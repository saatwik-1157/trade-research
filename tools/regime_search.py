#!/usr/bin/env python3
"""Were twenty nulls "no edge", or "edge masked by regime"? Kaufman says regime.

Twenty-first search, and the first that tests no new rule at all.

Every search in CLAUDE.md judged its candidates UNCONDITIONALLY: a rule was
scored over the whole history and reported dead. The strongest objection to
that record is not about any one family, it is structural -- a rule that works
in one regime and loses in another averages to nothing, and twenty
unconditional nulls cannot tell the two apart.

Kaufman makes that objection with numbers, which is why it is worth a run
rather than an argument. He measures a 20-day **efficiency ratio** across a
wide universe, applies a plain 40-day trend, and reports profit factor rising
from roughly **0.7 at ER 0.204 to 3.2 at ER 0.266**, concluding that *low
noise is good for trend following and high noise is not*. He separately claims
that trend entries improve when filtered to **low realised volatility**, and
reports every one of five markets improving. Those are two different
conditioning variables and both are tested here.

**This is confirmatory, not exploratory, and the distinction matters.** The
conditioner, the lookback and the direction of the predicted effect were all
published before this run, so finding them is a test rather than a search. If
the conditioners were chosen here, after seeing the returns, this would be
twenty-one searches worth of multiplicity wearing one search's correction.

  * **Efficiency ratio** `|P[t] - P[t-n]| / sum(|P[i] - P[i-1]|)`, n = 20.
    Kaufman's own definition, and he is explicit it is NOT a volatility
    measure. High ER means the move got somewhere; low ER means it thrashed.
  * **Realised volatility**, the 20-day standard deviation of returns.

**Both conditioners must be LAGGED, and the first version of this file was
not.** ER at bar t is computed from closes through c[t], so it contains the
very return r[t] that the trend rule earned on bar t -- and a trend position
profits precisely on bars where price travelled far in one direction, which is
exactly what pushes ER up. Bucketing the outcome by a variable containing the
outcome is a tautology, and it produced a spectacular one: **+41.6% annualised
in the top bucket, t = +21.63, PERFECT monotonicity, and a permutation null at
p = 0.000.** Lagged a single bar the entire effect vanishes -- the top-minus-
bottom spread goes from +28.44bp to -1.29bp.

The null could not have caught it. Shifting the conditioner destroys exactly
the contemporaneous alignment that creates the artefact, so the null passes
whether the bug is present or not. Only the lag check finds it, which is why
`--lag 0` is still available and is printed beside the real answer.

**The null is a circular shift of the CONDITIONER against the returns.** It
holds the conditioner's own distribution and autocorrelation exactly -- both
are strongly autocorrelated, and a naive shuffle would destroy that and test
a regime variable nobody has -- and destroys only its alignment with what the
trend rule then earned.

    python tools/regime_search.py
    python tools/regime_search.py --buckets 3
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
from kaufman_replication import (
    MAX_WARMUP, METHODS, PERIODS, SYMBOLS, always_in_pnl,
)
from rule_backtest import choose_spread, connect, fetch_rates

ER_N = 20
VOL_N = 20


def efficiency_ratio(c, n=ER_N):
    """Kaufman's ER: net movement over total movement, across n bars.

    One at a perfectly straight move, near zero at a thrash. Written out
    rather than imported because the denominator is the sum of ABSOLUTE bar
    changes, not the range, and confusing the two turns it into something
    close to the volatility-regime ratio this repository already refuted.
    """
    c = np.asarray(c, float)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    step = np.abs(np.diff(c, prepend=c[0]))
    # `np.convolve(..., "full")[i]` is already the sum of the n values ENDING
    # at i, so the series is taken from the front. Slicing at [n-1:] instead
    # shifts the window forward by n-1 bars and pulls the FUTURE into the
    # denominator: measured, that put ER at 1.65 on a straight line where the
    # ratio is bounded by 1, sent it as high as 10.6 on a random walk, and
    # made the whole series non-causal.
    tot = np.convolve(step, np.ones(n), mode="full")[:len(c)]
    net = np.full(len(c), np.nan)
    net[n:] = np.abs(c[n:] - c[:-n])
    with np.errstate(divide="ignore", invalid="ignore"):
        out = net / tot
    out[~np.isfinite(out)] = np.nan
    return out


def realised_vol(c, n=VOL_N):
    r = np.diff(np.log(np.asarray(c, float)), prepend=0.0)
    out = np.full(len(c), np.nan)
    for t in range(n, len(c)):
        out[t] = r[t - n + 1:t + 1].std(ddof=1)
    return out


def bucket(x, k):
    """Quantile buckets, computed on the live values only."""
    out = np.full(len(x), -1, dtype=int)
    live = np.isfinite(x)
    if live.sum() < k * 20:
        return out
    edges = np.quantile(x[live], np.linspace(0, 1, k + 1)[1:-1])
    out[live] = np.digitize(x[live], edges)
    return out


def grid_pnl(closes, cost):
    """Mean per-bar net P&L across the whole 5 x 6 trend grid.

    The average of the grid rather than a chosen cell, because Kaufman's claim
    is about trend following as a class and picking the best cell first would
    be the selection this project keeps refuting.
    """
    acc = np.zeros(len(closes))
    n = 0
    for fn in METHODS.values():
        for p in PERIODS:
            sig = fn(closes, p)
            sig[:MAX_WARMUP] = np.nan
            pnl = always_in_pnl(sig, closes, cost)
            pnl[:MAX_WARMUP] = np.nan
            acc += np.nan_to_num(pnl)
            n += 1
    acc[:MAX_WARMUP] = np.nan
    return acc / max(1, n)


def tstat(x):
    x = x[np.isfinite(x)]
    if len(x) < 30 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def spearman(a, b):
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    return float(np.corrcoef(ra, rb)[0, 1])


def profile(pnl, cond, k):
    """Mean P&L per conditioner bucket, plus the monotonicity Kaufman claims."""
    b = bucket(cond, k)
    means, ns = [], []
    for j in range(k):
        m = (b == j) & np.isfinite(pnl)
        means.append(float(pnl[m].mean()) if m.sum() > 20 else float("nan"))
        ns.append(int(m.sum()))
    good = [(j, v) for j, v in enumerate(means) if v == v]
    rho = spearman(np.array([j for j, _ in good], float),
                   np.array([v for _, v in good], float)) if len(good) > 2 else float("nan")
    return means, ns, rho


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeframe", default="D1")
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--buckets", type=int, default=5)
    ap.add_argument("--lag", type=int, default=1,
                    help="bars to lag the conditioner; 0 reproduces the tautology")
    ap.add_argument("--null-rounds", type=int, default=1000)
    ap.add_argument("--out", default="reports/regime_search.json")
    args = ap.parse_args()
    k = args.buckets

    mt5 = connect()
    data = {}
    for sym in SYMBOLS:
        r = fetch_rates(mt5, sym, args.bars, timeframe=args.timeframe)
        if r is None or len(r) < 500:
            continue
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        spread, _n = choose_spread(r, info, tick, source="median")
        c = r["close"].astype(float)
        data[sym] = (c, float(spread) / float(np.median(c)))
    mt5.shutdown()
    if not data:
        return 1

    print(f"\n  regime conditioning, {args.timeframe}, {len(data)} majors, "
          f"{k} buckets")
    print(f"  the whole {len(METHODS)}x{len(PERIODS)} trend grid, averaged, "
          f"always in the market, net of the measured spread")
    print("  Kaufman: profit factor 0.7 at ER 0.204 rising to 3.2 at ER 0.266")

    results = {}
    for cname, fn in (("efficiency_ratio", efficiency_ratio),
                      ("realised_vol", realised_vol)):
        pooled_p, pooled_c = [], []
        for sym, (c, cost) in data.items():
            pooled_p.append(grid_pnl(c, cost))
            raw = fn(c)
            # Lag it: the conditioner must be known BEFORE the bar whose P&L
            # it is grouping, or it contains that bar's own return.
            pooled_c.append(np.concatenate((np.full(args.lag, np.nan),
                                            raw[:-args.lag])) if args.lag else raw)
        pnl = np.concatenate(pooled_p)
        cond = np.concatenate(pooled_c)
        means, ns, rho = profile(pnl, cond, k)

        # The unlagged figure is printed beside it, because the gap between
        # the two IS the finding and hiding it would leave the next reader to
        # rediscover the tautology.
        raw_c = np.concatenate([fn(c) for c, _ in data.values()])
        m0, _n0, rho0 = profile(pnl, raw_c, k)
        contemp = (m0[-1] - m0[0]) * 1e4 if m0[-1] == m0[-1] and m0[0] == m0[0] else float("nan")

        print("  " + "-" * 70)
        print(f"  conditioned on {cname.replace('_', ' ').upper()}")
        print(f"  {'bucket':9}{'bars':>9}{'mean bp/bar':>14}{'annualised %':>15}")
        for j, (m, nj) in enumerate(zip(means, ns)):
            tag = "  lowest" if j == 0 else ("  highest" if j == k - 1 else "")
            ann = m * 252 * 100 if m == m else float("nan")
            print(f"  {j:<9}{nj:>9,}{m * 1e4 if m == m else float('nan'):>14.3f}"
                  f"{ann:>15.2f}{tag}")
        print(f"  Spearman(bucket, mean) = {rho:+.3f}   "
              f"(Kaufman predicts strongly POSITIVE for ER)")
        print(f"  UNLAGGED, for comparison only: spread {contemp:+.3f}bp, "
              f"rho {rho0:+.3f}  <- contains the bar's own return")

        # Null: circularly shift the CONDITIONER, keeping its distribution and
        # its autocorrelation, destroying only the alignment.
        rng = np.random.default_rng(20260923)
        null_rho, null_spread = [], []
        real_spread = (means[-1] - means[0]) if (means[-1] == means[-1]
                                                 and means[0] == means[0]) else float("nan")
        for _ in range(args.null_rounds):
            sh = []
            for arr in pooled_c:
                j = int(rng.integers(MAX_WARMUP + 20, len(arr) - MAX_WARMUP - 20))
                sh.append(np.roll(arr, j))
            m2, _n2, r2 = profile(pnl, np.concatenate(sh), k)
            if r2 == r2:
                null_rho.append(r2)
            if m2[-1] == m2[-1] and m2[0] == m2[0]:
                null_spread.append(m2[-1] - m2[0])
        null_rho = np.array(null_rho)
        null_spread = np.array(null_spread)
        p_rho = float((np.abs(null_rho) >= abs(rho)).mean()) if len(null_rho) else float("nan")
        p_spr = float((np.abs(null_spread) >= abs(real_spread)).mean()) if len(null_spread) else float("nan")
        print(f"  shifted-conditioner null: |rho| >= {abs(rho):.3f} in "
              f"{p_rho * 100:.1f}%;  |top-bottom| >= {abs(real_spread) * 1e4:.3f}bp "
              f"in {p_spr * 100:.1f}%")

        results[cname] = {"lag": args.lag,
                          "unlagged_spread_bp": contemp,
                          "unlagged_spearman": rho0,
                          "means_bp": [m * 1e4 if m == m else None for m in means],
                          "bars": ns, "spearman": rho,
                          "top_minus_bottom_bp": real_spread * 1e4,
                          "p_spearman": p_rho, "p_spread": p_spr,
                          "top_bucket_t": tstat(pnl[(bucket(cond, k) == k - 1)]),
                          "bottom_bucket_t": tstat(pnl[(bucket(cond, k) == 0)])}
        print(f"  top bucket t = {results[cname]['top_bucket_t']:+.2f},  "
              f"bottom bucket t = {results[cname]['bottom_bucket_t']:+.2f}")

    print("  " + "-" * 70)
    print("  a regime that rescued trend following would show a positive")
    print("  Spearman on ER, a top bucket clearing zero, and a null that does not.")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"timeframe": args.timeframe, "buckets": k,
                   "er_lookback": ER_N, "vol_lookback": VOL_N,
                   "grid": f"{len(METHODS)}x{len(PERIODS)}",
                   "results": results}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
