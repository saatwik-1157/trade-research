#!/usr/bin/env python3
"""What the bar did INSIDE itself: the one feature space OHLC cannot express.

Twenty-fifth search. The input catalogue is closed at H1 -- every field MT5
supplies has been searched -- but that is a statement about the RESOLUTION,
not about the data. Two bars with identical open, high, low and close can have
travelled completely different distances getting there, and nothing tested
above can see it: `shape_search` measured body and wick from OHLC, and
`path_order_search` asked only which extreme came first.

M1 bars expose the path. 50,000 of them is 48 days, about 833 H1 bars a pair
and roughly 5,800 pooled, so the power is stated before the result rather than
after: one standard error of a win rate at that size is **0.66 points**, so the
1.50-point spread hurdle would show at about **t = 2.3**. Adequate for an
effect of the size that matters, and far too small for a subtle one.

**The premise was checked before the search, and it demoted the feature this
file would otherwise have led with.** Each candidate was regressed on the
OHLC-visible properties (body, close-in-range, range) to ask whether it is
genuinely new:

    path efficiency   R2 = 0.824 from OHLC   <- mostly RECOVERABLE, dropped
    path / range      R2 = 0.235
    open crossings    R2 = 0.151
    fraction above mid R2 = 0.023

Path efficiency -- net move over total travel -- is 82% explained by the bar's
own body and range, which makes it close to a restatement of ground
`shape_search` already refuted. It is excluded rather than reported as a
fourth family. The other three are kept.

**The control is RESIDUALISATION, and it is the whole test.** Each feature is
regressed on the OHLC properties and only the residual is used, so what is
measured is strictly the part of the path that OHLC cannot express. Testing
the raw feature would rediscover candle shape and call it new -- the same
confound `path_order_search` had to strip out with strata.

    python tools/intrabar_search.py
    python tools/intrabar_search.py --horizon 2
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_backtest import choose_spread, connect

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD")
MIN_SUB_BARS = 50          # an H1 bar needs most of its minutes to count
#: R2 above which a path feature is judged recoverable from OHLC and dropped.
RECOVERABLE = 0.50


def _crossings(closes, level):
    """How many times the series moves from strictly above to strictly below.

    Ticks sitting exactly ON the level are dropped rather than counted: a
    close that equals the open has not crossed it, and counting the sign
    passing through zero would score a touch as two crossings.
    """
    side = np.sign(closes - level)
    side = side[side != 0]
    if len(side) < 2:
        return 0
    return int((np.diff(side) != 0).sum())


def build_bars(t, o, h, l, c):
    """Group M1 bars into H1 bars and measure what happened inside each.

    A partial hour -- a session edge or a gap -- is DROPPED rather than
    measured, because a path statistic over twelve minutes is not the same
    quantity as one over sixty and pooling them would be the units error this
    repository documents in every other form.
    """
    hb = t // 3600
    group = collections.defaultdict(list)
    for i, k in enumerate(hb):
        group[int(k)].append(i)

    out = []
    for k in sorted(group):
        ii = np.array(group[k])
        if len(ii) < MIN_SUB_BARS:
            continue
        O, C = float(o[ii[0]]), float(c[ii[-1]])
        H, L = float(h[ii].max()), float(l[ii].min())
        if H <= L or C <= 0:
            continue
        closes = c[ii].astype(float)
        path = float(np.abs(np.diff(closes)).sum())
        if path <= 0:
            continue
        mid = (H + L) / 2.0
        out.append({
            "hour": k, "open": O, "close": C, "high": H, "low": L,
            # --- the path features, none of them visible in OHLC ---
            "ppr": path / (H - L),
            "above": float((closes > mid).mean()),
            # Count only genuine above<->below transitions. Using
            # `diff(sign(closes - O)) != 0` counts a close sitting EXACTLY on
            # the open as a crossing, because the sign passes through zero on
            # the way in and again on the way out -- so a bar that merely
            # touched its open scored two crossings it never made.
            "cross": float(_crossings(closes, O)),
            # --- what OHLC already says, used only to residualise ---
            "body": abs(C - O) / (H - L),
            "cir": (C - L) / (H - L),
            "rng": (H - L) / C,
        })
    return out


def residualise(feature, ohlc):
    """Strip out everything the OHLC properties can explain.

    Returns the residual and the R2 removed. A feature whose R2 is high was
    never new information, and reporting it as a finding would rediscover
    candle shape under another name.
    """
    X = np.column_stack([ohlc, np.ones(len(feature))])
    coef, *_ = np.linalg.lstsq(X, feature, rcond=None)
    pred = X @ coef
    ss = float(((feature - feature.mean()) ** 2).sum())
    r2 = 1.0 - float(((feature - pred) ** 2).sum()) / ss if ss > 0 else 0.0
    return feature - pred, r2


def tstat(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 30 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", type=int, default=50000)
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--null-rounds", type=int, default=2000)
    ap.add_argument("--out", default="reports/intrabar_search.json")
    args = ap.parse_args()

    mt5 = connect()
    rows, costs = [], []
    for sym in SYMBOLS:
        r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, args.bars)
        if r is None or len(r) < 10000:
            print(f"  {sym}: too few M1 bars")
            continue
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        spread, _n = choose_spread(r, info, tick, source="median")
        costs.append(float(spread) / float(np.median(r["close"])))
        bars = build_bars(r["time"].astype(np.int64), r["open"], r["high"],
                          r["low"], r["close"])
        cl = np.array([b["close"] for b in bars])
        k = args.horizon
        for i, b in enumerate(bars[:-k]):
            b["fwd"] = float(np.log(cl[i + k] / cl[i]))
            b["sym"] = sym
            rows.append(b)
    mt5.shutdown()
    if not rows:
        return 1

    cost_rt = float(np.mean(costs)) * 2 * 100
    fwd = np.array([r["fwd"] for r in rows])
    ohlc = np.column_stack([[r["body"] for r in rows],
                            [r["cir"] for r in rows],
                            [r["rng"] for r in rows]])

    print(f"\n  intrabar path, {len(SYMBOLS)} majors, {len(rows):,} H1 bars "
          f"rebuilt from M1, horizon {args.horizon}")
    se_pts = math.sqrt(0.25 / len(rows)) * 100
    print(f"  one standard error of a win rate here is {se_pts:.2f} points, so "
          f"the 1.50 hurdle shows at t={1.50 / se_pts:.1f}")
    print(f"  round trip costs {cost_rt:.4f}% of price")
    print("  " + "-" * 74)
    print(f"  {'feature':22}{'R2 from OHLC':>14}{'kept':>7}"
          f"{'resid t':>10}{'top-bot %':>12}{'null p':>9}")

    rng = np.random.default_rng(20260923)
    out = []
    for name, key in (("path / range", "ppr"),
                      ("fraction above mid", "above"),
                      ("open crossings", "cross"),
                      ("path efficiency", None)):
        if key is None:
            # Kept in the table for honesty: it was a candidate, it was
            # measured as recoverable, and it is reported rather than quietly
            # dropped.
            eff = np.array([abs(r["close"] - r["open"]) /
                            max(1e-12, r["ppr"] * (r["high"] - r["low"]))
                            for r in rows])
            _res, r2 = residualise(eff, ohlc)
            print(f"  {name:22}{r2:>14.3f}{'no':>7}{'-':>10}{'-':>12}{'-':>9}")
            out.append({"feature": name, "r2_from_ohlc": r2, "kept": False})
            continue

        raw = np.array([r[key] for r in rows], dtype=float)
        resid, r2 = residualise(raw, ohlc)
        kept = r2 < RECOVERABLE
        if not kept:
            print(f"  {name:22}{r2:>14.3f}{'no':>7}{'-':>10}{'-':>12}{'-':>9}")
            out.append({"feature": name, "r2_from_ohlc": r2, "kept": False})
            continue

        # Sign the forward return by the residual's sign, so a positive mean
        # means the feature predicted direction.
        scored = np.sign(resid) * fwd
        t = tstat(scored)
        lo, hi = np.quantile(resid, [1 / 3, 2 / 3])
        top = fwd[resid >= hi].mean() * 100
        bot = fwd[resid <= lo].mean() * 100

        null = []
        for _ in range(args.null_rounds):
            tt = tstat(np.sign(rng.permutation(resid)) * fwd)
            if tt == tt:
                null.append(tt)
        null = np.array(null)
        p = float((np.abs(null) >= abs(t)).mean())

        print(f"  {name:22}{r2:>14.3f}{'yes':>7}{t:>+10.2f}"
              f"{top - bot:>+12.4f}{p * 100:>8.1f}%")
        out.append({"feature": name, "r2_from_ohlc": r2, "kept": True,
                    "resid_t": t, "top_minus_bottom_pct": top - bot,
                    "null_p": p, "null_max_abs_t": float(np.abs(null).max())})

    kept = [o for o in out if o.get("kept")]
    print("  " + "-" * 74)
    print(f"  {len(kept)} of {len(out)} candidates carried information OHLC "
          f"does not, and are judged")
    print(f"  Bonferroni over {len(kept)}: p must beat "
          f"{0.05 / max(1, len(kept)):.4f}")
    best = max((o for o in kept), key=lambda o: abs(o["resid_t"]), default=None)
    if best:
        print(f"  best |t| {abs(best['resid_t']):.2f} ({best['feature']}), "
              f"null p {best['null_p'] * 100:.1f}%")
        print(f"  its top-minus-bottom spread is "
              f"{best['top_minus_bottom_pct']:+.4f}% against a round trip of "
              f"{cost_rt:.4f}%")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"bars": len(rows), "horizon": args.horizon,
                   "se_win_rate_points": se_pts,
                   "cost_round_trip_pct": cost_rt,
                   "recoverable_threshold": RECOVERABLE,
                   "features": out}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
