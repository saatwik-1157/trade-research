#!/usr/bin/env python3
"""Measure whether the mt5_paper.py trading rules separate forward returns.

Replicates the live rules bar-for-bar against MT5 history: same signal
functions, same 1.5xATR bracket, same "closed bars only" discipline. The
`random` rule is run alongside as the benchmark - a rule that cannot beat a
coin flip over a few hundred trades has not demonstrated anything.

    python tools/rule_backtest.py --rule rsi_reversion
    python tools/rule_backtest.py --rule all --out reports/rule_backtest.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random

import numpy as np

# nights_between lives in swap.py with the unit conversion it belongs to;
# simulate() only needs the count.
from swap import nights_between

DEFAULT_TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SYMBOLS = "EURUSD,GBPUSD,USDJPY,USDCAD,AUDUSD,USDCHF,NZDUSD"


def connect(path=None):
    import MetaTrader5 as mt5
    if not mt5.initialize(path=path or DEFAULT_TERMINAL, timeout=60000):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5


def choose_spread(rates, info, tick, source="median"):
    """Spread in price units, and a note on where it came from.

    `live` reads one quote from the moment the tool happens to run, which on a
    quiet morning understates this broker by 3-8x and silently flatters every
    result. The recorded per-bar spread over the whole history is the honest
    default; bars carrying 0 are unrecorded rather than free, so they are
    dropped instead of averaged in.
    """
    recorded = None
    if rates is not None and "spread" in (rates.dtype.names or ()):
        sp = rates["spread"].astype(float)
        sp = sp[sp > 0]
        if len(sp):
            recorded = sp

    if source in ("median", "p90") and recorded is not None:
        pts = float(np.median(recorded)) if source == "median" else float(
            np.percentile(recorded, 90))
        return pts * info.point, f"{source} of {len(recorded)} bars recording a spread"

    live = (tick.ask - tick.bid) if tick and tick.ask > tick.bid else 0.0
    if live <= 0:
        live = info.spread * info.point
    if live <= 0:
        return 0.0, "no spread available - costs understated, treat as an upper bound"
    return live, "single live quote at run time"


def timeframe_const(mt5, name):
    """Map a timeframe name to MT5's constant.

    M1 is here because this repository analyses M1 bars in two places -- the
    rollover-window study and `intrabar_search` -- and both had to reach past
    this helper to `copy_rates_from_pos`, which loses the stepping-down that
    `fetch_rates` does when the terminal refuses an over-large request. An
    unknown name raises rather than defaulting: a silent fallback to H1 would
    return real bars of the wrong size.
    """
    known = ("M1", "M5", "M15", "H1", "H4", "D1")
    if name not in known:
        raise KeyError(f"unknown timeframe {name!r}; known: {' '.join(known)}")
    # getattr, not a dict literal: a literal evaluates EVERY entry, so it
    # demands attributes the caller never asked for -- which broke the test
    # suite's mock terminal, defining only the three timeframes it uses.
    attr = f"TIMEFRAME_{name}"
    if not hasattr(mt5, attr):
        raise KeyError(f"this terminal has no {attr}")
    return getattr(mt5, attr)


def fetch_rates(mt5, symbol, want, timeframe="H1", min_bars=500):
    """Ask for `want` bars, stepping down until the terminal agrees.

    The terminal rejects an over-large request with "Invalid params" rather
    than returning what it has, so an unconditional big ask silently yields
    nothing for every symbol.
    """
    tf = timeframe_const(mt5, timeframe)
    for count in (want, 50000, 20000, 10000, 5000, 2000, 1000):
        if count > want:
            continue
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is not None and len(rates) >= min_bars:
            return rates
    return None


def trim_to_years(rates, years):
    """Drop everything older than `years` before the last bar.

    The D1 series on this feed reaches back to 1971 - decades before EURUSD
    existed. Those backfilled bars carry synthetic prices and a placeholder
    spread (50 points against 3 for a real quote), so leaving them in makes a
    daily test both fictional and, through the spread, wrong in the expensive
    direction. Trimming also keeps timeframes comparable to one another.
    """
    if rates is None or not years:
        return rates
    t = rates["time"].astype("int64")
    return rates[t >= int(t[-1]) - int(years * 365.25 * 86400)]


# ---------------------------------------------------------------- indicators
def wilder_rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder RSI as a full series.

    mt5_paper reseeds from the first n bars of its 300-bar window on every
    call; after ~286 smoothing steps the seed's weight is (1-1/n)^286 ~ 1e-9,
    so a continuous series reproduces the live values well past display
    precision.
    """
    d = np.diff(close)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    out = np.full(close.shape, np.nan)
    if len(d) < n:
        return out
    ag, al = gain[:n].mean(), loss[:n].mean()
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + gain[i]) / n
        al = (al * (n - 1) + loss[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_series(h, l, c, n: int = 14) -> np.ndarray:
    """Simple mean of true range over n bars - matches atr_from() in mt5_paper."""
    prev = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    out = np.full(c.shape, np.nan)
    csum = np.cumsum(np.insert(tr, 0, 0.0))
    for i in range(n, len(tr) + 1):
        out[i] = (csum[i] - csum[i - n]) / n
    return out


def sma(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full(a.shape, np.nan)
    if len(a) < n:
        return out
    csum = np.cumsum(np.insert(a, 0, 0.0))
    out[n - 1:] = (csum[n:] - csum[:-n]) / n
    return out


# --------------------------------------------------------------------- rules
def signals_rsi_reversion(o, h, l, c):
    r = wilder_rsi(c)
    sig = np.where(r < 30, 1, np.where(r > 70, -1, 0))
    sig[np.isnan(r)] = 0
    return sig


def signals_sma_cross(o, h, l, c):
    f, s = sma(c, 20), sma(c, 50)
    sig = np.zeros(c.shape, dtype=int)
    up = (f[:-1] <= s[:-1]) & (f[1:] > s[1:])
    dn = (f[:-1] >= s[:-1]) & (f[1:] < s[1:])
    sig[1:][up] = 1
    sig[1:][dn] = -1
    sig[np.isnan(f) | np.isnan(s)] = 0
    return sig


def signals_random(o, h, l, c, seed=0):
    rng = random.Random(seed)
    # same distribution as rule_random: buy, sell, None, None
    return np.array([{0: 1, 1: -1}.get(rng.choice([0, 1, 2, 3]), 0) for _ in c], dtype=int)


SIGNALS = {
    "rsi_reversion": signals_rsi_reversion,
    "sma_cross": signals_sma_cross,
    "random": signals_random,
}


# ----------------------------------------------------------------- simulation
def simulate(o, h, l, c, sig, atr, spread, sl_atr=1.5, tp_atr=1.5, max_hold=240,
             times=None, swap=None, triple_dow=None):
    """Walk bars, open on a signal from CLOSED bars, exit at SL or TP.

    Entry is the next bar's open: the signal reads bars up to i-1, so it is
    only actionable from bar i. When one bar's range covers both SL and TP the
    loss is booked - intrabar order is unknown, and assuming the win is the
    classic way a backtest flatters itself.

    `swap` is (long_per_night, short_per_night) in PRICE units, signed, so a
    charge is negative and a carry credit is positive - AUDUSD pays to be long
    on this broker and charges to be short, and flattening that to a cost would
    be as wrong as ignoring it. Passing None keeps the spread-only behaviour
    every existing result was measured under, so the default cannot silently
    restate history; callers opt in.

    Nights are counted between the entry bar's open and the exit bar's open,
    which for D1 is exactly the number of daily boundaries the position slept
    through. A position that never sleeps is charged nothing.
    """
    trades = []
    i = 60
    n = len(c)
    while i < n - 1:
        s = sig[i - 1]
        if s == 0 or not np.isfinite(atr[i - 1]) or atr[i - 1] <= 0:
            i += 1
            continue
        entry = o[i]
        a = atr[i - 1]
        if s > 0:
            sl, tp = entry - sl_atr * a, entry + tp_atr * a
        else:
            sl, tp = entry + sl_atr * a, entry - tp_atr * a

        exit_px, exit_reason, held, j = None, None, 0, i
        for j in range(i, min(i + max_hold, n)):
            held = j - i + 1
            if s > 0:
                hit_sl, hit_tp = l[j] <= sl, h[j] >= tp
            else:
                hit_sl, hit_tp = h[j] >= sl, l[j] <= tp
            if hit_sl:                      # loss wins ties, deliberately
                exit_px, exit_reason = sl, "sl"
                break
            if hit_tp:
                exit_px, exit_reason = tp, "tp"
                break
        if exit_px is None:
            exit_px, exit_reason = c[j], "timeout"

        gross = (exit_px - entry) if s > 0 else (entry - exit_px)
        nights, financing = 0, 0.0
        if swap is not None and times is not None:
            nights = nights_between(times[i], times[j], triple_dow)
            financing = nights * (swap[0] if s > 0 else swap[1])
        trades.append({"dir": int(s), "gross": float(gross),
                       "net": float(gross - spread + financing), "bars": held,
                       "nights": nights, "financing": float(financing),
                       "reason": exit_reason, "entry_idx": i})
        i = j + 1                            # flat before the next entry
    return trades


def stats(trades, point):
    if not trades:
        return {"trades": 0, "note": "no trades generated"}
    net = np.array([t["net"] for t in trades]) / point
    gross = np.array([t["gross"] for t in trades]) / point
    wins, losses = net[net > 0], net[net < 0]
    gp, gl = wins.sum(), -losses.sum()
    sd = net.std(ddof=1) if len(net) > 1 else 0.0
    t_stat = (net.mean() / (sd / math.sqrt(len(net)))) if sd > 0 else 0.0
    return {
        "trades": len(trades),
        "win_rate": round(float(len(wins) / len(net)), 4),
        "expectancy_points_net": round(float(net.mean()), 2),
        "expectancy_points_gross": round(float(gross.mean()), 2),
        "profit_factor": round(float(gp / gl), 3) if gl > 0 else None,
        "total_points_net": round(float(net.sum()), 1),
        "avg_win_points": round(float(wins.mean()), 1) if len(wins) else None,
        "avg_loss_points": round(float(losses.mean()), 1) if len(losses) else None,
        "t_stat": round(float(t_stat), 2),
        "significant_at_95": bool(abs(t_stat) > 1.96),
        "avg_bars_held": round(float(np.mean([t["bars"] for t in trades])), 1),
        "exit_mix": {k: sum(1 for t in trades if t["reason"] == k)
                     for k in ("tp", "sl", "timeout")},
    }


def main():
    ap = argparse.ArgumentParser(description="Backtest the mt5_paper trading rules.")
    ap.add_argument("--rule", default="rsi_reversion",
                    choices=["rsi_reversion", "sma_cross", "random", "all"])
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--bars", type=int, default=100000, help="H1 bars per symbol")
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--tp-atr", type=float, default=1.5)
    ap.add_argument("--spread-source", default="median",
                    choices=["median", "p90", "live"],
                    help="median of recorded bar spreads (default), the 90th "
                         "percentile, or a single live quote")
    ap.add_argument("--out")
    ap.add_argument("--path")
    args = ap.parse_args()

    mt5 = connect(args.path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    rules = ["rsi_reversion", "sma_cross", "random"] if args.rule == "all" else [args.rule]

    result = {"sl_atr": args.sl_atr, "tp_atr": args.tp_atr,
              "timeframe": "H1", "spread_source": args.spread_source,
              "rules": {}, "data_gaps": []}

    market = {}
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            result["data_gaps"].append(f"{sym}: could not select")
            continue
        rates = fetch_rates(mt5, sym, args.bars)
        info = mt5.symbol_info(sym)
        tick = mt5.symbol_info_tick(sym)
        if rates is None or len(rates) < 500 or info is None:
            result["data_gaps"].append(f"{sym}: insufficient history")
            continue
        spread, spread_note = choose_spread(rates, info, tick, args.spread_source)
        if spread <= 0:
            result["data_gaps"].append(f"{sym}: {spread_note}")
        market[sym] = {
            "o": rates["open"].astype(float), "h": rates["high"].astype(float),
            "l": rates["low"].astype(float), "c": rates["close"].astype(float),
            "point": info.point, "spread": spread, "bars": len(rates),
            "spread_note": spread_note,
            "from": str(np.datetime64(int(rates["time"][0]), "s")),
            "to": str(np.datetime64(int(rates["time"][-1]), "s")),
        }

    for rule in rules:
        per_symbol, pooled = {}, []
        for sym, m in market.items():
            sig = SIGNALS[rule](m["o"], m["h"], m["l"], m["c"])
            atr = atr_series(m["h"], m["l"], m["c"])
            tr = simulate(m["o"], m["h"], m["l"], m["c"], sig, atr, m["spread"],
                          args.sl_atr, args.tp_atr)
            per_symbol[sym] = {**stats(tr, m["point"]),
                               "spread_points": round(m["spread"] / m["point"], 1),
                               "spread_source": m["spread_note"],
                               "bars": m["bars"], "from": m["from"], "to": m["to"]}
            pooled += [{"net": t["net"] / m["point"], "gross": t["gross"] / m["point"],
                        "bars": t["bars"], "reason": t["reason"]} for t in tr]
        result["rules"][rule] = {"pooled": stats(pooled, 1.0), "per_symbol": per_symbol}

    mt5.shutdown()
    text = json.dumps(result, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
