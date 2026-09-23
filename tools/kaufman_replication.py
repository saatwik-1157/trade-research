#!/usr/bin/env python3
"""Kaufman's trend claim, tested on his own terms at his own periods.

CLAUDE.md records fifteen null searches. One published claim contradicts them
with numbers attached, and it deserves a run rather than a dismissal.

Kaufman's `Trading Systems and Methods` reports a 17-market study of five
trend methods over 40 calculation periods, and ranks **EURUSD the single most
robust trending market: 87% of tests profitable**, with linear-regression
slope at 93% and N-day breakout at 90%. USDJPY is second-equal at 81%.

**The previous search here did not test that claim, and saying why matters
more than the result.** `book_rules_search` ran regression slope at H1 with
n=50 and n=200, where 200 bars is about eight days. Kaufman's range is
**28-80 DAYS**. His hypothesis was never in the grid. That is a gap in what
was run, not a disagreement about what was found.

**Three things must match his protocol or this measures something else.**

  * **Always in the market, reversing, NO STOPS.** `rule_search` brackets
    every trade at 1.5xATR, and his systems hold until the signal flips.
    Running his entries with this repository's exit would test a third
    strategy belonging to neither of us, and the exit search already showed
    that a bracket changes a trend result's sign. So the simulator here is
    his: one position, always on, reversed when the signal reverses.
  * **A COMMON START DATE.** His own rule, and this repository was not
    following it: every test must begin where the LONGEST wind-up ends, or a
    28-day rule silently trades 52 bars the 80-day rule spent warming up and
    the two are no longer judged on the same sample. Measured at H1 the spread
    was 1.00% and harmless; at D1 an 80-bar wind-up is ~3% of the history, so
    it is fixed here rather than noted.
  * **GEOMETRIC period spacing.** Also his: 28, 35, 44, 55, 69, 80 rather than
    every fifth day, because a 75-day and an 80-day average are nearly the
    same hypothesis and arithmetic spacing fills the correction's denominator
    with near-duplicates.

**Where this deliberately does NOT follow him, and why.** He charges a flat
$40 round turn, which is below his own stated FX slippage estimate; this
charges the measured per-bar spread, as every other search here does. He
computes the profit factor on CLOSED trades only, so open drawdown is
invisible; this marks to market. And he has no permutation null and no
correction across 40 periods x 5 methods x 17 markets -- 3,400 tests -- so
both are supplied. His headline statistic, **the share of the parameter grid
that is profitable**, is computed exactly as he defines it, because that is
the number being checked.

    python tools/kaufman_replication.py
    python tools/kaufman_replication.py --timeframe D1 --years 10
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_backtest import choose_spread, connect, fetch_rates
from rule_search import z_for

# Geometric, inside Kaufman's stated 28-80 day range. Six points at a ratio of
# about 1.23, which spans the range without filling it with near-duplicates.
PERIODS = (28, 35, 44, 55, 69, 80)
SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD")
MAX_WARMUP = max(PERIODS)


def sma_signal(c, n):
    out = np.full(len(c), np.nan)
    if len(c) >= n:
        out[n - 1:] = np.convolve(c, np.ones(n) / n, mode="valid")
    return np.sign(c - out)


def ema_signal(c, n):
    a = 2.0 / (n + 1.0)
    e = np.full(len(c), np.nan)
    e[n - 1] = c[:n].mean()
    for t in range(n, len(c)):
        e[t] = a * c[t] + (1 - a) * e[t - 1]
    return np.sign(c - e)


def lwma_signal(c, n):
    w = np.arange(1, n + 1, dtype=float)
    w /= w.sum()
    out = np.full(len(c), np.nan)
    if len(c) >= n:
        out[n - 1:] = np.convolve(c, w[::-1], mode="valid")
    return np.sign(c - out)


def slope_signal(c, n):
    """Least-squares slope of close on bar index over the trailing n bars."""
    out = np.full(len(c), np.nan)
    if len(c) < n:
        return np.sign(out)
    x = np.arange(n, dtype=float)
    xc = x - x.mean()
    win = np.lib.stride_tricks.sliding_window_view(c, n)
    out[n - 1:] = (win - win.mean(axis=1, keepdims=True)) @ xc / float(np.dot(xc, xc))
    return np.sign(out)


def breakout_signal(c, n):
    """Long on an n-bar closing high, short on an n-bar closing low, else hold.

    Kaufman's N-day breakout is a REVERSAL system: it stays with the last
    signal until the opposite extreme is made, so the carry-forward below is
    the rule rather than a convenience.
    """
    out = np.full(len(c), np.nan)
    if len(c) < n:
        return out
    win = np.lib.stride_tricks.sliding_window_view(c, n)
    hi = win.max(axis=1)
    lo = win.min(axis=1)
    sig = np.full(len(c), np.nan)
    body = np.where(c[n - 1:] >= hi, 1.0, np.where(c[n - 1:] <= lo, -1.0, np.nan))
    sig[n - 1:] = body
    # Carry the last signal forward; that is what "reversal system" means.
    idx = np.where(~np.isnan(sig), np.arange(len(sig)), 0)
    np.maximum.accumulate(idx, out=idx)
    return sig[idx]


METHODS = {
    "sma": sma_signal,
    "ema": ema_signal,
    "lwma": lwma_signal,
    "slope": slope_signal,
    "breakout": breakout_signal,
}


def always_in_pnl(sig, c, cost):
    """Kaufman's simulator: one position, always on, reversed on a flip.

    Returns per-bar net log returns. A reversal crosses the spread TWICE --
    out of one side and into the other -- which is charged at the bar the
    position changes. No stop, no target, marked to market every bar, because
    a profit factor on closed trades alone hides open drawdown.
    """
    r = np.diff(np.log(c), prepend=np.log(c[0]))
    pos = np.nan_to_num(sig, nan=0.0)
    held = np.concatenate(([0.0], pos[:-1]))          # yesterday's position
    turn = np.abs(pos - held)                          # 0, 1 or 2
    return held * r - turn * cost


def t_of(x):
    live = x[np.isfinite(x)]
    if live.size < 30 or live.std() == 0:
        return float("nan")
    return float(live.mean() / (live.std() / math.sqrt(live.size)))


def run_grid(closes, cost, shift=0, rng=None):
    """Every method x period on one symbol. `shift` circularly rotates the
    signal, which is the null: identical trade count, identical holding
    structure, alignment with returns destroyed."""
    rows = []
    for mname, fn in METHODS.items():
        for n in PERIODS:
            sig = fn(closes, n)
            sig[:MAX_WARMUP] = np.nan            # the COMMON start date
            if shift:
                sig = np.roll(sig, shift)
                sig[:MAX_WARMUP] = np.nan
            pnl = always_in_pnl(sig, closes, cost)
            pnl[:MAX_WARMUP] = np.nan
            rows.append({"method": mname, "period": n,
                         "total": float(np.nansum(pnl)), "t": t_of(pnl),
                         "pnl": pnl})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeframe", default="D1")
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--null-rounds", type=int, default=20)
    ap.add_argument("--out", default="reports/kaufman_replication.json")
    args = ap.parse_args()

    mt5 = connect()
    data = {}
    for sym in SYMBOLS:
        rates = fetch_rates(mt5, sym, args.bars, timeframe=args.timeframe)
        if rates is None or len(rates) < 500:
            print(f"  {sym}: too few bars, dropped")
            continue
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        spread, _note = choose_spread(rates, info, tick, source="median")
        # Spread as a RETURN, since the book is in log returns.
        data[sym] = (rates["close"].astype(float),
                     float(spread) / float(np.median(rates["close"])))
    mt5.shutdown()
    if not data:
        print("  no symbols")
        return 1

    n_bars = min(len(c) for c, _ in data.values())
    print(f"\n  Kaufman's five trend methods x {len(PERIODS)} geometric periods "
          f"({PERIODS[0]}-{PERIODS[-1]}), {args.timeframe}")
    print(f"  {len(data)} majors, {n_bars:,} bars each, always in the market, "
          f"reversing, no stops")
    print(f"  common start date at bar {MAX_WARMUP}; cost is the measured "
          f"spread on each crossing")
    print("  " + "-" * 72)
    print(f"  {'symbol':9}{'% tests profitable':>20}{'best t':>9}{'best method':>14}"
          f"{'best n':>8}")

    rng = np.random.default_rng(20260923)
    summary, all_rows = {}, []
    for sym, (closes, cost) in data.items():
        rows = run_grid(closes, cost)
        good = [r for r in rows if r["t"] == r["t"]]
        share = sum(1 for r in good if r["total"] > 0) / max(1, len(good))
        best = max(good, key=lambda r: r["t"])
        summary[sym] = {"pct_profitable": share * 100,
                        "best_t": best["t"], "best_method": best["method"],
                        "best_period": best["period"],
                        "tests": len(good)}
        all_rows += [{k: v for k, v in r.items() if k != "pnl"} | {"symbol": sym}
                     for r in rows]
        print(f"  {sym:9}{share * 100:>19.1f}%{best['t']:>+9.2f}"
              f"{best['method']:>14}{best['period']:>8}")

    # Kaufman's own headline is the share of the grid that is profitable, so
    # that is what the null is built against.
    pooled = [r for sym in data for r in run_grid(data[sym][0], data[sym][1])]
    pooled_good = [r for r in pooled if r["t"] == r["t"]]
    real_share = sum(1 for r in pooled_good if r["total"] > 0) / len(pooled_good)
    real_best_t = max(r["t"] for r in pooled_good)

    null_shares, null_best = [], []
    for _ in range(args.null_rounds):
        rows = []
        for sym, (closes, cost) in data.items():
            shift = int(rng.integers(MAX_WARMUP + 50, len(closes) - MAX_WARMUP - 50))
            rows += run_grid(closes, cost, shift=shift, rng=rng)
        g = [r for r in rows if r["t"] == r["t"]]
        null_shares.append(sum(1 for r in g if r["total"] > 0) / len(g))
        null_best.append(max(r["t"] for r in g))

    thresh = z_for(0.05 / len(pooled_good))
    print("  " + "-" * 72)
    print(f"  pooled: {real_share * 100:.1f}% of {len(pooled_good)} tests "
          f"profitable, best t {real_best_t:+.2f}")
    print(f"  null  : {float(np.mean(null_shares)) * 100:.1f}% profitable "
          f"(max {max(null_shares) * 100:.1f}%), best t {float(np.mean(null_best)):+.2f} "
          f"(max {max(null_best):+.2f})")
    print(f"  Bonferroni threshold over {len(pooled_good)} tests: {thresh:.3f}")
    eur = summary.get("EURUSD", {})
    print(f"\n  Kaufman reports EURUSD 87% of tests profitable, slope 93%, "
          f"breakout 90%.")
    print(f"  Measured here: EURUSD {eur.get('pct_profitable', float('nan')):.1f}%, "
          f"best t {eur.get('best_t', float('nan')):+.2f} "
          f"({eur.get('best_method', '-')}, n={eur.get('best_period', '-')})")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"by_symbol": summary, "rows": all_rows,
                   "pooled": {"pct_profitable": real_share * 100,
                              "best_t": real_best_t,
                              "tests": len(pooled_good),
                              "bonferroni": thresh},
                   "null": {"pct_profitable_mean": float(np.mean(null_shares)) * 100,
                            "pct_profitable_max": max(null_shares) * 100,
                            "best_t_mean": float(np.mean(null_best)),
                            "best_t_max": max(null_best),
                            "rounds": args.null_rounds},
                   "config": {"timeframe": args.timeframe, "periods": list(PERIODS),
                              "bars": n_bars, "common_start": MAX_WARMUP,
                              "always_in_market": True, "stops": None}},
                  fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
