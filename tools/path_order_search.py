#!/usr/bin/env python3
"""Did the high come before the low? The one thing OHLC does not record.

Eighteenth search, and the only one here whose feature no book on the shelf
could have computed. Two bars with **identical** open, high, low and close
can have taken opposite paths: one rose to its high and then sold off, the
other fell to its low and then rallied. OHLC cannot tell them apart. A finer
series can, and this project has H1 bars underneath every D1 bar, so the
ordering is recoverable for free.

**Measured first, before any hypothesis: the ambiguity is 0.57%.** Only 12 of
2,089 EURUSD D1 bars put their high and low inside the SAME H1 bar, so H1
resolves a D1 path cleanly. H4 would not -- four sub-bars is too coarse -- so
this runs at D1 only, and that is a measurement rather than a preference.

**The confound is the whole problem, and it is severe.** Where the bar closes
in its own range almost determines the ordering: high-first runs 96.9% in the
bottom close decile and 3.7% in the top, monotonically, and it is 89.3% on
down bars against 12.2% on up bars. So the RAW feature is very nearly a
restatement of candle shape, and `shape_search` already refuted candle shape
across 28 candidates. An unconditional test here would rediscover that and
call it a new finding.

**So the question is narrowed to the only part that is genuinely new: does the
ordering carry anything BEYOND what the close-in-range already says?** Bars
are stratified into close-in-range deciles, and the comparison is made only
WITHIN a stratum, between bars that closed in the same place but travelled
there differently. The extreme deciles are reported and not judged -- at 96.9%
and 3.7% there is almost no minority class to compare against, and a t-statistic
on eight observations is not evidence.

**The null is permutation WITHIN strata**, which is the design that makes the
result mean anything. Shuffling the ordering label across the whole sample
would destroy the confound as well as the signal and would therefore test
whether close-in-range predicts returns -- a question already answered.
Shuffling inside each decile holds the confound EXACTLY fixed and destroys
only the path information, so whatever survives is attributable to the
ordering and to nothing else.

    python tools/path_order_search.py
    python tools/path_order_search.py --horizon 2
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_backtest import choose_spread, connect, fetch_rates

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD")
N_DECILES = 10
# A stratum needs enough of BOTH classes to compare. Below this the decile is
# reported and not judged, because the confound has left nothing to test.
MIN_PER_CLASS = 40


def reconstruct(times, o, h, l, c):
    """Aggregate H1 into D1 and recover which extreme came first.

    Returns one row per day: high_first, close-in-range, the day's own return
    and the day's index. A day whose high and low fall in the SAME H1 bar is
    ambiguous at this resolution and is dropped rather than guessed -- the
    count is reported, because a silent drop is how a sample quietly changes.
    """
    days = np.array([dt.datetime.fromtimestamp(int(x), dt.UTC).date() for x in times])
    by_day = collections.defaultdict(list)
    for i, d in enumerate(days):
        by_day[d].append(i)

    out, ambiguous, thin = [], 0, 0
    for d, ii in sorted(by_day.items()):
        if len(ii) < 6:            # a holiday stub is not a day
            thin += 1
            continue
        ii = np.array(ii)
        i_hi = int(ii[np.argmax(h[ii])])
        i_lo = int(ii[np.argmin(l[ii])])
        if i_hi == i_lo:
            ambiguous += 1
            continue
        hh, ll = float(h[ii].max()), float(l[ii].min())
        cc, oo = float(c[ii[-1]]), float(o[ii[0]])
        if hh <= ll:
            continue
        out.append({"date": d, "high_first": i_hi < i_lo,
                    "cir": (cc - ll) / (hh - ll), "close": cc, "open": oo})
    return out, ambiguous, thin


def welch(a, b):
    if len(a) < 5 or len(b) < 5:
        return float("nan"), float("nan")
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    if va + vb <= 0:
        return float("nan"), float("nan")
    return float(a.mean() - b.mean()), float((a.mean() - b.mean()) / math.sqrt(va + vb))


def stratified_diff(fwd, hf, dec):
    """Mean forward-return difference, pooled across deciles by inverse variance.

    Only deciles carrying at least `MIN_PER_CLASS` of BOTH classes contribute.
    Pooling the extremes would let a decile that is 96.9% one class dominate
    on noise.
    """
    num = den = 0.0
    used = []
    for k in range(N_DECILES):
        m = dec == k
        a, b = fwd[m & hf], fwd[m & ~hf]
        if len(a) < MIN_PER_CLASS or len(b) < MIN_PER_CLASS:
            continue
        va = a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)
        if va <= 0:
            continue
        num += (a.mean() - b.mean()) / va
        den += 1.0 / va
        used.append(k)
    if den <= 0:
        return float("nan"), float("nan"), used
    est = num / den
    return est, est * math.sqrt(den), used


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", type=int, default=50000)
    ap.add_argument("--horizon", type=int, default=1, help="days ahead to score")
    ap.add_argument("--null-rounds", type=int, default=2000)
    ap.add_argument("--out", default="reports/path_order_search.json")
    args = ap.parse_args()

    mt5 = connect()
    rows_all, costs, amb_tot, day_tot = [], {}, 0, 0
    for sym in SYMBOLS:
        r = fetch_rates(mt5, sym, args.bars, timeframe="H1")
        if r is None or len(r) < 5000:
            print(f"  {sym}: too few bars")
            continue
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        spread, _n = choose_spread(r, info, tick, source="median")
        costs[sym] = float(spread) / float(np.median(r["close"]))
        rows, amb, _thin = reconstruct(r["time"], r["open"].astype(float),
                                       r["high"].astype(float),
                                       r["low"].astype(float),
                                       r["close"].astype(float))
        amb_tot += amb
        day_tot += len(rows) + amb
        closes = np.array([x["close"] for x in rows])
        fwd = np.full(len(rows), np.nan)
        k = args.horizon
        if len(rows) > k:
            fwd[:-k] = np.log(closes[k:] / closes[:-k])
        for x, f in zip(rows, fwd):
            if np.isfinite(f):
                rows_all.append({**x, "symbol": sym, "fwd": float(f)})
    mt5.shutdown()
    if not rows_all:
        print("  nothing reconstructed")
        return 1

    hf = np.array([x["high_first"] for x in rows_all])
    cir = np.array([x["cir"] for x in rows_all])
    fwd = np.array([x["fwd"] for x in rows_all])
    dec = np.clip((cir * N_DECILES).astype(int), 0, N_DECILES - 1)
    mean_cost = float(np.mean(list(costs.values())))

    print(f"\n  intrabar path ordering, D1 rebuilt from H1, {len(SYMBOLS)} majors")
    print(f"  {len(rows_all):,} days scored at horizon {args.horizon}; "
          f"{amb_tot:,} of {day_tot:,} ({amb_tot / max(1, day_tot) * 100:.2f}%) "
          f"ambiguous and dropped")
    print(f"  high-first overall {hf.mean() * 100:.1f}%; round trip costs "
          f"{mean_cost * 2 * 100:.4f}% of price")

    # 1. The UNCONDITIONAL test, which is mostly the confound.
    d_raw, t_raw = welch(fwd[hf], fwd[~hf])
    print("  " + "-" * 74)
    print(f"  unconditional: high-first {fwd[hf].mean() * 100:+.4f}% vs "
          f"low-first {fwd[~hf].mean() * 100:+.4f}%, "
          f"diff {d_raw * 100:+.4f}%, t {t_raw:+.2f}")
    print("  (this is largely close-in-range restated, which shape_search "
          "already refuted)")

    # 2. The test that isolates the ordering.
    print("  " + "-" * 74)
    print(f"  {'decile':8}{'n high-first':>14}{'n low-first':>13}"
          f"{'hf fwd %':>11}{'lf fwd %':>11}{'diff %':>9}{'t':>7}")
    per_dec = []
    for k in range(N_DECILES):
        m = dec == k
        a, b = fwd[m & hf], fwd[m & ~hf]
        d, t = welch(a, b)
        judged = len(a) >= MIN_PER_CLASS and len(b) >= MIN_PER_CLASS
        mark = "" if judged else "   not judged (confound leaves no contrast)"
        per_dec.append({"decile": k, "n_high_first": int(len(a)),
                        "n_low_first": int(len(b)),
                        "diff_pct": d * 100 if d == d else None,
                        "t": t if t == t else None, "judged": judged})
        print(f"  {k:<8}{len(a):>14,}{len(b):>13,}"
              f"{a.mean() * 100 if len(a) else float('nan'):>+11.4f}"
              f"{b.mean() * 100 if len(b) else float('nan'):>+11.4f}"
              f"{d * 100 if d == d else float('nan'):>+9.4f}"
              f"{t if t == t else float('nan'):>+7.2f}{mark}")

    est, t_strat, used = stratified_diff(fwd, hf, dec)
    print("  " + "-" * 74)
    print(f"  stratified over deciles {used}: "
          f"diff {est * 100:+.4f}%, t {t_strat:+.2f}")

    # 3. The null: permute the label WITHIN each decile, so the confound is
    #    held exactly and only the ordering is destroyed.
    rng = np.random.default_rng(20260923)
    null_t = []
    for _ in range(args.null_rounds):
        shuffled = hf.copy()
        for k in used:
            m = np.flatnonzero(dec == k)
            shuffled[m] = rng.permutation(shuffled[m])
        _e, tt, _u = stratified_diff(fwd, shuffled, dec)
        if tt == tt:
            null_t.append(tt)
    null_t = np.array(null_t)
    p_two = float((np.abs(null_t) >= abs(t_strat)).mean()) if len(null_t) else float("nan")
    print(f"  within-decile permutation over {len(null_t):,} rounds: "
          f"|t| >= {abs(t_strat):.2f} in {p_two * 100:.1f}%")
    print(f"  null |t| reaches {np.abs(null_t).max():.2f}, 95th pct "
          f"{np.percentile(np.abs(null_t), 95):.2f}")
    print(f"\n  the stratified difference is {est * 100:+.4f}% against a "
          f"round-trip cost of {mean_cost * 2 * 100:.4f}%")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"n_days": len(rows_all), "horizon": args.horizon,
                   "ambiguous": amb_tot, "days_total": day_tot,
                   "high_first_share": float(hf.mean()),
                   "unconditional": {"diff_pct": d_raw * 100, "t": t_raw},
                   "by_decile": per_dec,
                   "stratified": {"diff_pct": est * 100, "t": t_strat,
                                  "deciles_used": used},
                   "null": {"rounds": len(null_t), "p_two_sided": p_two,
                            "max_abs_t": float(np.abs(null_t).max()),
                            "p95_abs_t": float(np.percentile(np.abs(null_t), 95))},
                   "cost_round_trip_pct": mean_cost * 2 * 100}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
