#!/usr/bin/env python3
"""Search many candidate rule families for one that survives correction.

Searching N rules and reporting the best is how noise gets published, so the
comparison here is not against 1.96. It is against what the same search finds
when there is definitionally nothing to find:

  * Every candidate is paired with a PERMUTATION null - its own signal array
    shuffled. Signal count, buy/sell mix and trade frequency are preserved
    exactly; only the timing is destroyed. So the null candidate trades as
    often as the real one and pays the same spread, and any difference between
    them is timing skill rather than a difference in exposure. Several rounds
    of permutation give a distribution for "best of N under no skill".
  * Candidates are ranked in-sample; the winner's out-of-sample result is what
    counts.
  * A Bonferroni threshold is computed over the true number of candidates.

A candidate has to clear all three to be worth a second look. Expect none to.
The repository has measured its composite at IC 0.002, found 0 of 105 patterns
surviving correction, and shown that at these spreads roughly 6,600 trades are
needed to demonstrate an edge the size of the scatter between rules - so a null
result here is the predicted outcome, not a failed run.

    python tools/rule_search.py --out reports/rule_search.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rule_backtest import (SYMBOLS, atr_series, choose_spread, connect,
                           fetch_rates, simulate, sma, stats, trim_to_years,
                           wilder_rsi)


# ------------------------------------------------------------- indicators
def ema(a, n):
    out = np.full(a.shape, np.nan)
    if len(a) < n:
        return out
    k = 2.0 / (n + 1)
    acc = a[:n].mean()
    out[n - 1] = acc
    for i in range(n, len(a)):
        acc = a[i] * k + acc * (1 - k)
        out[i] = acc
    return out


def rolling_max(a, n):
    out = np.full(a.shape, np.nan)
    for i in range(n, len(a)):
        out[i] = a[i - n:i].max()
    return out


def rolling_min(a, n):
    out = np.full(a.shape, np.nan)
    for i in range(n, len(a)):
        out[i] = a[i - n:i].min()
    return out


def rolling_std(a, n):
    out = np.full(a.shape, np.nan)
    for i in range(n, len(a)):
        out[i] = a[i - n:i].std(ddof=0)
    return out


def _blank(c):
    return np.zeros(c.shape, dtype=int)


# -------------------------------------------------------------- candidates
# Every signal reads closed bars only; simulate() enters on the following bar.
def make_rsi(n, lo, hi, invert=False):
    def f(o, h, l, c):
        r = wilder_rsi(c, n)
        buy, sell = (r > hi, r < lo) if invert else (r < lo, r > hi)
        sig = np.where(buy, 1, np.where(sell, -1, 0))
        sig[np.isnan(r)] = 0
        return sig
    return f


def make_ma_cross(fast, slow, kind="sma", invert=False):
    def f(o, h, l, c):
        fn = sma if kind == "sma" else ema
        a, b = fn(c, fast), fn(c, slow)
        sig = _blank(c)
        up = (a[:-1] <= b[:-1]) & (a[1:] > b[1:])
        dn = (a[:-1] >= b[:-1]) & (a[1:] < b[1:])
        sig[1:][up] = -1 if invert else 1
        sig[1:][dn] = 1 if invert else -1
        sig[np.isnan(a) | np.isnan(b)] = 0
        return sig
    return f


def make_donchian(n, fade=False):
    def f(o, h, l, c):
        hi, lo = rolling_max(h, n), rolling_min(l, n)
        brk_up, brk_dn = c > hi, c < lo
        sig = np.where(brk_up, -1 if fade else 1, np.where(brk_dn, 1 if fade else -1, 0))
        sig[np.isnan(hi) | np.isnan(lo)] = 0
        return sig
    return f


def make_bollinger(n, k, fade=True):
    def f(o, h, l, c):
        m, s = sma(c, n), rolling_std(c, n)
        upper, lower = m + k * s, m - k * s
        above, below = c > upper, c < lower
        sig = np.where(above, -1 if fade else 1, np.where(below, 1 if fade else -1, 0))
        sig[np.isnan(m) | np.isnan(s)] = 0
        return sig
    return f


def make_momentum(n, invert=False):
    def f(o, h, l, c):
        sig = _blank(c)
        r = np.full(c.shape, np.nan)
        r[n:] = c[n:] / c[:-n] - 1.0
        up, dn = r > 0, r < 0
        sig[up] = -1 if invert else 1
        sig[dn] = 1 if invert else -1
        sig[np.isnan(r)] = 0
        # Only act on a change of state, or this fires on every single bar.
        change = np.zeros(c.shape, dtype=bool)
        change[1:] = sig[1:] != sig[:-1]
        sig[~change] = 0
        return sig
    return f


def build_candidates():
    c = []
    for n in (7, 14, 21):
        for lo, hi in ((20, 80), (25, 75), (30, 70)):
            c.append((f"rsi_rev_{n}_{lo}_{hi}", "rsi_reversion", make_rsi(n, lo, hi)))
    for n in (14,):
        for lo, hi in ((30, 70), (40, 60)):
            c.append((f"rsi_mom_{n}_{lo}_{hi}", "rsi_momentum", make_rsi(n, lo, hi, invert=True)))
    for fast, slow in ((5, 20), (10, 50), (20, 50), (20, 100), (50, 200)):
        c.append((f"sma_{fast}_{slow}", "ma_cross", make_ma_cross(fast, slow, "sma")))
        c.append((f"ema_{fast}_{slow}", "ma_cross", make_ma_cross(fast, slow, "ema")))
    for fast, slow in ((10, 50), (20, 100)):
        c.append((f"sma_{fast}_{slow}_inv", "ma_cross_inv",
                  make_ma_cross(fast, slow, "sma", invert=True)))
    for n in (20, 55, 100):
        c.append((f"donchian_brk_{n}", "breakout", make_donchian(n)))
        c.append((f"donchian_fade_{n}", "breakout_fade", make_donchian(n, fade=True)))
    for n, k in ((20, 2.0), (20, 2.5), (50, 2.0)):
        c.append((f"boll_fade_{n}_{k}", "bollinger_fade", make_bollinger(n, k)))
        c.append((f"boll_ride_{n}_{k}", "bollinger_ride", make_bollinger(n, k, fade=False)))
    for n in (24, 72, 168):
        c.append((f"mom_{n}", "momentum", make_momentum(n)))
        c.append((f"mom_{n}_inv", "momentum_inv", make_momentum(n, invert=True)))
    return c


def permute(sig, seed):
    """Shuffle a signal array: same count and mix, timing destroyed."""
    rng = np.random.default_rng(seed)
    out = sig.copy()
    rng.shuffle(out)
    return out


def run_candidate(market, sig_by_symbol, split_idx, sl_atr, tp_atr):
    ins, oos = [], []
    for sym, m in market.items():
        tr = simulate(m["o"], m["h"], m["l"], m["c"], sig_by_symbol[sym], m["atr"],
                      m["spread"], sl_atr, tp_atr)
        for t in tr:
            row = {"net": t["net"] / m["point"], "gross": t["gross"] / m["point"],
                   "bars": t["bars"], "reason": t["reason"]}
            (ins if t["entry_idx"] < split_idx else oos).append(row)
    return stats(ins, 1.0), stats(oos, 1.0)


def z_for(alpha):
    lo, hi = 0.0, 8.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if math.erfc(mid / math.sqrt(2)) > alpha:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


def main():
    ap = argparse.ArgumentParser(description="Search rule families with a permutation null.")
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--split", type=float, default=0.70)
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--tp-atr", type=float, default=1.5)
    ap.add_argument("--null-rounds", type=int, default=3,
                    help="permutation draws of the whole candidate set")
    ap.add_argument("--spread-source", default="median", choices=["median", "p90", "live"])
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--timeframe", default="H1", choices=["H1", "H4", "D1"])
    ap.add_argument("--years", type=float, default=3.2,
                    help="trim history to this many years, so timeframes are "
                         "comparable and backfilled bars are excluded")
    ap.add_argument("--out")
    ap.add_argument("--path")
    args = ap.parse_args()

    mt5 = connect(args.path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    result = {"timeframe": args.timeframe, "years": args.years,
              "sl_atr": args.sl_atr, "tp_atr": args.tp_atr,
              "spread_source": args.spread_source, "split": args.split,
              "null_rounds": args.null_rounds, "data_gaps": []}

    market = {}
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            result["data_gaps"].append(f"{sym}: could not select")
            continue
        rates = fetch_rates(mt5, sym, args.bars, args.timeframe, min_bars=300)
        rates = trim_to_years(rates, args.years)
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        if rates is None or info is None:
            result["data_gaps"].append(f"{sym}: insufficient history")
            continue
        spread, note = choose_spread(rates, info, tick, args.spread_source)
        if spread <= 0:
            result["data_gaps"].append(f"{sym}: {note}")
        o, h, l, c = (rates["open"].astype(float), rates["high"].astype(float),
                      rates["low"].astype(float), rates["close"].astype(float))
        market[sym] = {"o": o, "h": h, "l": l, "c": c, "atr": atr_series(h, l, c),
                       "point": info.point, "spread": spread, "n": len(c),
                       "from": str(np.datetime64(int(rates["time"][0]), "s")),
                       "to": str(np.datetime64(int(rates["time"][-1]), "s"))}
    mt5.shutdown()
    if not market:
        raise SystemExit("no usable symbols: " + "; ".join(result["data_gaps"]))

    any_sym = next(iter(market.values()))
    split_idx = int(any_sym["n"] * args.split)
    result["window"] = {"from": any_sym["from"], "to": any_sym["to"],
                        "bars_per_symbol": any_sym["n"], "in_sample_bars": split_idx,
                        "out_of_sample_bars": any_sym["n"] - split_idx}

    candidates = build_candidates()
    result["candidates_tested"] = len(candidates)

    rows, null_rounds = [], [[] for _ in range(args.null_rounds)]
    for idx, (name, family, fn) in enumerate(candidates):
        sigs = {s: fn(m["o"], m["h"], m["l"], m["c"]) for s, m in market.items()}
        ins, oos = run_candidate(market, sigs, split_idx, args.sl_atr, args.tp_atr)
        rows.append({"name": name, "family": family,
                     "in_sample": ins, "out_of_sample": oos})
        # Matched null: identical signal counts, timing shuffled.
        for r in range(args.null_rounds):
            psigs = {s: permute(sig, seed=idx * 1000 + r * 7 + 1)
                     for s, sig in sigs.items()}
            nins, _ = run_candidate(market, psigs, split_idx, args.sl_atr, args.tp_atr)
            null_rounds[r].append(nins.get("t_stat", 0.0)
                                  if nins.get("trades", 0) >= args.min_trades else None)

    valid = [r for r in rows if r["in_sample"].get("trades", 0) >= args.min_trades]
    result["candidates_with_enough_trades"] = len(valid)
    result["candidates"] = rows

    if not valid:
        result["verdict"] = {"note": "no candidate produced enough trades to judge"}
    else:
        best = max(valid, key=lambda r: r["in_sample"]["t_stat"])
        best_real_t = best["in_sample"]["t_stat"]
        null_best = [max([t for t in rnd if t is not None], default=0.0)
                     for rnd in null_rounds]
        z_crit = z_for(0.05 / len(valid))
        oos_survivors = [r["name"] for r in valid
                         if (r["out_of_sample"].get("t_stat") or 0) > 1.96]
        result["verdict"] = {
            "candidates_judged": len(valid),
            "bonferroni_z_threshold": z_crit,
            "best_candidate": best["name"],
            "best_in_sample_t": best_real_t,
            "best_out_of_sample_t": best["out_of_sample"].get("t_stat"),
            "best_out_of_sample_expectancy": best["out_of_sample"].get("expectancy_points_net"),
            "permutation_null_best_t_per_round": null_best,
            "permutation_null_best_t_mean": round(float(np.mean(null_best)), 2),
            "beats_bonferroni": bool(best_real_t > z_crit),
            "beats_permutation_null": bool(best_real_t > max(null_best)) if null_best else None,
            "holds_out_of_sample": bool((best["out_of_sample"].get("t_stat") or 0) > 1.96),
            "candidates_positive_out_of_sample_at_1_96": oos_survivors,
            "expected_by_chance_at_1_96": round(0.025 * len(valid), 1),
            "reading": (
                "A candidate counts as evidence only if it clears the Bonferroni "
                "threshold, exceeds the best the permutation null reached over the "
                "same number of candidates, AND holds out of sample. The permutation "
                "null trades exactly as often as the real candidate and pays the same "
                "spread, so it isolates timing skill. Candidates passing 1.96 out of "
                "sample should be compared against expected_by_chance before being "
                "read as survivors."
            ),
        }

    text = json.dumps(result, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    else:
        print(json.dumps(result["verdict"], indent=2))


if __name__ == "__main__":
    main()
