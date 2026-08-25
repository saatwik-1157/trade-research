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
  * Inference is clustered by entry date. Seven USD majors share a leg, so one
    dollar move opens correlated trades in all of them and a pooled t-statistic
    counts that move seven times. A by-symbol figure is reported alongside, but
    as a consistency check only - it rises when the pairs agree, which is what
    a shared move looks like rather than evidence against one.
  * History is cut into equal-duration eras and the search is walked forward
    across them, re-ranking candidates on the eras before each test era. One
    sequential split cannot show that a result predates the current regime,
    because its holdout is always the present - so several splits agreeing is
    the same observation counted several times, not several observations.

A candidate has to clear all of these to be worth a second look. Expect none to.
The repository has measured its composite at IC 0.002, found 0 of 105 patterns
surviving correction, and shown that at these spreads roughly 6,600 trades are
needed to demonstrate an edge the size of the scatter between rules - so a null
result here is the predicted outcome, not a failed run.

    python tools/rule_search.py --out reports/rule_search.json
    python tools/rule_search.py --timeframe D1 --years 10 --blocks 5
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swap import swap_points_per_night
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


def collect_trades(market, sig_by_symbol, sl_atr, tp_atr):
    """Every trade from every symbol, in points, tagged with symbol and time.

    Tagging rather than pooling immediately is what lets the same trades be
    read three ways - by split, by symbol and by era - without simulating
    three times.
    """
    rows = []
    for sym, m in market.items():
        tr = simulate(m["o"], m["h"], m["l"], m["c"], sig_by_symbol[sym], m["atr"],
                      m["spread"], sl_atr, tp_atr, times=m.get("time"),
                      swap=m.get("swap"), triple_dow=m.get("triple_dow"))
        for t in tr:
            rows.append({"symbol": sym, "entry_idx": t["entry_idx"],
                         "entry_time": int(m["time"][t["entry_idx"]]),
                         "net": t["net"] / m["point"], "gross": t["gross"] / m["point"],
                         "bars": t["bars"], "reason": t["reason"]})
    return rows


# Two-sided 95% critical values. With seven symbols the clustered statistic has
# six degrees of freedom and 1.96 is the wrong threshold by a wide margin.
_T95 = {2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
        9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086, 30: 2.042}


def t_crit_95(df):
    if df < 2:
        return None
    usable = [k for k in sorted(_T95) if k <= df]
    return _T95[usable[-1]] if usable else 1.96


def _cluster(groups):
    """Mean and t across group means, not across raw observations."""
    means = np.array([float(np.mean(v)) for v in groups], dtype=float)
    k = len(means)
    if k < 3 or means.std(ddof=1) == 0:
        return k, None, None
    t = float(means.mean() / (means.std(ddof=1) / math.sqrt(k)))
    return k, round(t, 2), t_crit_95(k - 1)


def clustered_t(rows):
    """t across symbols rather than across trades.

    This asks whether the average PAIR made money, so it cannot be carried by
    one pair with a lot of trades. It is a consistency check, not the
    significance test: with seven groups it has six degrees of freedom, and
    when every pair agrees the between-pair spread is small and the statistic
    comes out LARGER than the pooled one. That is not extra evidence - seven
    pairs agreeing is what a single shared dollar move looks like. For that,
    see clustered_by_date.
    """
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r["net"])
    k, t, crit = _cluster(list(by_sym.values()))
    means = [float(np.mean(v)) for v in by_sym.values()]
    out = {"symbols": k, "symbols_positive": int(sum(1 for m in means if m > 0))}
    out["t_stat_clustered_by_symbol"] = t
    if t is None:
        out["symbol_cluster_note"] = "too few symbols for clustered inference"
        return out
    out["symbol_cluster_threshold_95"] = crit
    out["symbol_cluster_significant"] = bool(crit and abs(t) > crit)
    return out


def clustered_by_date(rows):
    """t across entry dates - the correction that actually applies here.

    Every pair on the book has USD on one side, so one dollar move opens
    correlated trades in all seven at once. Pooling treats those as seven
    independent observations and inflates t by roughly the square root of how
    many fire together. Collapsing to one observation per entry date and
    testing across dates removes that, and it is the same correction
    patterns.py applies for the same reason.

    It does not fix serial overlap: a rule holding six bars still has
    positions from neighbouring dates open together. So this is an upper bound
    on the evidence, not a clean one.
    """
    by_date = {}
    for r in rows:
        d = str(np.datetime64(int(r["entry_time"]), "s").astype("datetime64[D]"))
        by_date.setdefault(d, []).append(r["net"])
    groups = list(by_date.values())
    k, t, crit = _cluster(groups)
    out = {"entry_dates": k,
           "obs_per_date": round(len(rows) / k, 2) if k else None,
           "t_stat_clustered_by_date": t}
    if t is None:
        out["date_cluster_note"] = "too few entry dates for clustered inference"
        return out

    # Two means that can disagree in sign. The pooled mean weights every trade
    # equally, so dates firing many pairs at once dominate it; the per-date
    # mean weights every date equally. When they disagree the result lives in
    # a handful of crowded dates, and neither figure can be quoted alone - so
    # the divergence is reported rather than resolved.
    pooled_mean = float(np.mean([r["net"] for r in rows]))
    per_date_mean = float(np.mean([float(np.mean(g)) for g in groups]))
    out["mean_per_date"] = round(per_date_mean, 2)
    out["signs_disagree"] = bool(pooled_mean * per_date_mean < 0)
    out["date_cluster_threshold_95"] = crit
    out["date_cluster_significant"] = bool(crit and abs(t) > crit)
    return out


def per_symbol_views(rows):
    out = {}
    for sym in sorted({r["symbol"] for r in rows}):
        s = stats([r for r in rows if r["symbol"] == sym], 1.0)
        out[sym] = {"trades": s.get("trades", 0), "win_rate": s.get("win_rate"),
                    "expectancy_points_net": s.get("expectancy_points_net"),
                    "t_stat": s.get("t_stat")}
    return out


def block_edges(market, n_blocks):
    """Equal-duration era boundaries over the window every symbol covers."""
    lo = max(int(m["time"][0]) for m in market.values())
    hi = min(int(m["time"][-1]) for m in market.values())
    step = (hi - lo) / n_blocks
    return [lo + step * i for i in range(n_blocks + 1)]


def _in_block(r, edges, i):
    lo, hi = edges[i], edges[i + 1]
    last = i == len(edges) - 2
    return lo <= r["entry_time"] <= hi if last else lo <= r["entry_time"] < hi


def block_views(rows, edges):
    out = []
    for i in range(len(edges) - 1):
        s = stats([r for r in rows if _in_block(r, edges, i)], 1.0)
        out.append({"block": i + 1,
                    "from": str(np.datetime64(int(edges[i]), "s")),
                    "to": str(np.datetime64(int(edges[i + 1]), "s")),
                    "trades": s.get("trades", 0),
                    "win_rate": s.get("win_rate"),
                    "expectancy_points_net": s.get("expectancy_points_net"),
                    "t_stat": s.get("t_stat")})
    return out


def block_summary(blocks):
    graded = [b for b in blocks if b["trades"]]
    exps = [b["expectancy_points_net"] for b in graded]
    return {"blocks_graded": len(graded),
            "blocks_positive": sum(1 for e in exps if e > 0),
            "expectancy_by_block": exps,
            "min_block_expectancy": min(exps) if exps else None,
            "max_block_expectancy": max(exps) if exps else None}


def walk_forward(trades_by_name, edges, min_trades):
    """Rank on the eras before the test era, then trade only the winner in it.

    A named candidate's per-block record answers "did this rule work in other
    eras". It does not answer "would this search have found a rule that
    worked", because the name was chosen with the whole history in view. Here
    the selection is remade at each fold from the blocks before the test block
    only, so what is being graded is the search rather than a survivor of it.
    """
    folds = []
    for k in range(1, len(edges) - 1):
        cut, hi = edges[k], edges[k + 1]
        ranked = []
        for name, rows in trades_by_name.items():
            s = stats([r for r in rows if r["entry_time"] < cut], 1.0)
            if s.get("trades", 0) >= min_trades:
                ranked.append((s["t_stat"], name, s.get("trades")))
        if not ranked:
            folds.append({"test_block": k + 1,
                          "note": "no candidate had enough training trades"})
            continue
        ranked.sort(reverse=True)
        t_train, name, n_train = ranked[0]
        last = k == len(edges) - 2
        sel = [r for r in trades_by_name[name]
               if (cut <= r["entry_time"] <= hi if last else cut <= r["entry_time"] < hi)]
        s_test = stats(sel, 1.0)
        folds.append({"test_block": k + 1, "trained_on_blocks": f"1-{k}",
                      "selected": name, "train_t": t_train, "train_trades": n_train,
                      "test_trades": s_test.get("trades", 0),
                      "test_expectancy_points_net": s_test.get("expectancy_points_net"),
                      "test_t": s_test.get("t_stat")})
    graded = [f for f in folds if f.get("test_trades")]
    exps = [f["test_expectancy_points_net"] for f in graded]
    return {"folds": folds, "folds_graded": len(graded),
            "folds_profitable": sum(1 for e in exps if e > 0),
            "mean_test_expectancy_points_net": (
                round(float(np.mean(exps)), 2) if exps else None),
            "note": ("Each fold ranks every candidate on the blocks before the "
                     "test block and trades only the winner through it, so the "
                     "selection never sees the era it is judged on.")}


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
    ap.add_argument("--skip-hours", default="",
                    help="server hours to refuse entries in, comma separated. "
                         "cost_profile.py measures the rollover hour at ~4x the "
                         "normal spread, and cost is the one lever with a "
                         "measured sign")
    ap.add_argument("--cost-swap", action="store_true",
                    help="charge overnight financing as well as the spread. Off "
                         "by default so existing results are not silently "
                         "restated; every D1 figure in this repo was measured "
                         "without it and moves DOWN when it is on. Symbols whose "
                         "swap unit swap.py cannot convert are dropped with a "
                         "data_gaps entry rather than charged zero")
    ap.add_argument("--blocks", type=int, default=4,
                    help="split history into this many equal-duration eras and "
                         "walk the search forward across them; 0 or 1 disables")
    ap.add_argument("--years", type=float, default=3.2,
                    help="trim history to this many years, so timeframes are "
                         "comparable and backfilled bars are excluded")
    ap.add_argument("--out")
    ap.add_argument("--path")
    args = ap.parse_args()

    mt5 = connect(args.path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    deposit_currency = getattr(mt5.account_info(), "currency", "USD")
    skip_hours = [int(h) for h in args.skip_hours.split(",") if h.strip()]
    result = {"timeframe": args.timeframe, "years": args.years,
              "sl_atr": args.sl_atr, "tp_atr": args.tp_atr,
              "spread_source": args.spread_source, "split": args.split,
              "null_rounds": args.null_rounds, "blocks": args.blocks,
              "skip_hours": skip_hours, "data_gaps": []}

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
        times = rates["time"].astype("int64")
        blocked = np.zeros(len(c), dtype=bool)
        if skip_hours:
            bad = np.isin((times // 3600) % 24, skip_hours)
            blocked[:-1] = bad[1:]
        swap_px, triple = None, None
        if args.cost_swap:
            lng, sht, note = swap_points_per_night(info, getattr(tick, "bid", 0.0) or 0.0,
                                                   deposit_currency)
            if lng is None:
                result["data_gaps"].append(
                    f"{note} - {sym} is excluded from this run rather than "
                    "charged zero financing")
                continue
            swap_px, triple = (lng * info.point, sht * info.point), info.swap_rollover3days

        market[sym] = {"o": o, "h": h, "l": l, "c": c, "atr": atr_series(h, l, c),
                       "time": times, "entry_blocked": blocked,
                       "swap": swap_px, "triple_dow": triple,
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

    # Pooled figures are in "points" - price movement divided by the symbol's
    # own point size. That is roughly comparable across the FX majors and not
    # comparable at all across, say, silver and platinum, where a typical bar
    # spans different orders of magnitude in points. Pooling those adds numbers
    # in different units and produces a headline expectancy that means nothing;
    # per_symbol is the honest read there.
    atr_scale = {s: float(np.nanmedian(m["atr"]) / m["point"]) for s, m in market.items()}
    finite = [v for v in atr_scale.values() if np.isfinite(v) and v > 0]
    if finite and max(finite) / min(finite) > 5:
        result["data_gaps"].append(
            "symbols differ in typical bar size by "
            f"{max(finite) / min(finite):.0f}x in points "
            f"({', '.join(f'{s}={v:.0f}' for s, v in sorted(atr_scale.items()))}); "
            "pooled point figures add different units and are not meaningful - "
            "read per_symbol instead")
    result["median_atr_points_by_symbol"] = {s: round(v, 1) for s, v in atr_scale.items()}

    if len({m["n"] for m in market.values()}) > 1:
        result["data_gaps"].append(
            "symbols returned different bar counts; the in/out split is indexed "
            "off the first symbol, while eras are cut by time")

    candidates = build_candidates()
    result["candidates_tested"] = len(candidates)

    edges = block_edges(market, args.blocks) if args.blocks > 1 else None
    if edges:
        result["eras"] = {
            "count": args.blocks,
            "from": str(np.datetime64(int(edges[0]), "s")),
            "to": str(np.datetime64(int(edges[-1]), "s")),
            "note": ("Equal-duration eras over the window every symbol covers. "
                     "A rule positive only in the last era is a regime, not an "
                     "edge, and every sequential split puts that same era in the "
                     "holdout - so several splits agreeing is one observation, "
                     "not several."),
        }

    rows, null_rounds = [], [[] for _ in range(args.null_rounds)]
    trades_by_name = {}
    for idx, (name, family, fn) in enumerate(candidates):
        sigs = {s: np.where(m["entry_blocked"], 0,
                            fn(m["o"], m["h"], m["l"], m["c"]))
                for s, m in market.items()}
        tr = collect_trades(market, sigs, args.sl_atr, args.tp_atr)
        trades_by_name[name] = tr
        ins_rows = [r for r in tr if r["entry_idx"] < split_idx]
        oos_rows = [r for r in tr if r["entry_idx"] >= split_idx]
        ins, oos = stats(ins_rows, 1.0), stats(oos_rows, 1.0)
        for view, rws in ((ins, ins_rows), (oos, oos_rows)):
            view.update(clustered_t(rws))
            view.update(clustered_by_date(rws))
        row = {"name": name, "family": family, "in_sample": ins,
               "out_of_sample": oos, "per_symbol": per_symbol_views(tr)}
        if edges:
            row["blocks"] = block_views(tr, edges)
            row["block_summary"] = block_summary(row["blocks"])
        rows.append(row)
        # Matched null: identical signal counts, timing shuffled.
        for r in range(args.null_rounds):
            psigs = {s: permute(sig, seed=idx * 1000 + r * 7 + 1)
                     for s, sig in sigs.items()}
            ntr = collect_trades(market, psigs, args.sl_atr, args.tp_atr)
            nins = stats([x for x in ntr if x["entry_idx"] < split_idx], 1.0)
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
        wf = walk_forward(trades_by_name, edges, args.min_trades) if edges else None
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
            "best_out_of_sample_t_clustered_by_date":
                best["out_of_sample"].get("t_stat_clustered_by_date"),
            # Significant, positive, and agreeing with the pooled mean. A
            # clustered t can be large and positive while the rule lost money
            # over the same trades, which is a crowded-date artefact rather
            # than a survivor.
            "best_survives_date_clustering": bool(
                best["out_of_sample"].get("date_cluster_significant")
                and (best["out_of_sample"].get("t_stat_clustered_by_date") or 0) > 0
                and not best["out_of_sample"].get("signs_disagree")
                and (best["out_of_sample"].get("expectancy_points_net") or 0) > 0),
            "best_out_of_sample_signs_disagree":
                best["out_of_sample"].get("signs_disagree"),
            "best_out_of_sample_obs_per_date":
                best["out_of_sample"].get("obs_per_date"),
            "best_out_of_sample_t_clustered_by_symbol":
                best["out_of_sample"].get("t_stat_clustered_by_symbol"),
            "best_symbols_positive_out_of_sample":
                best["out_of_sample"].get("symbols_positive"),
            "best_block_summary": best.get("block_summary"),
            "walk_forward": wf,
            "reading": (
                "A candidate counts as evidence only if it clears the Bonferroni "
                "threshold, exceeds the best the permutation null reached over the "
                "same number of candidates, AND holds out of sample. The permutation "
                "null trades exactly as often as the real candidate and pays the same "
                "spread, so it isolates timing skill. Candidates passing 1.96 out of "
                "sample should be compared against expected_by_chance before being "
                "read as survivors. Two further checks sit alongside those. "
                "Clustering by entry date collapses the trades one shared dollar "
                "move opens across seven pairs into a single observation, which is "
                "the correction that applies here; the by-symbol figure is a "
                "consistency check and goes UP when every pair agrees, so it is not "
                "evidence. The era blocks and the walk-forward ask whether a result "
                "predates the most recent regime: a single sequential split cannot, "
                "because its holdout is always the present."
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
