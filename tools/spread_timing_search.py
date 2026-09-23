#!/usr/bin/env python3
"""Can the cost drag be timed? The only lever this project has ever measured.

Twenty-second search, and the first aimed at cost rather than direction.

Twenty-one searches say direction is unforecastable here. The record says
something else about cost, and it is quantified: the live ledger is **-0.0235R
a trade against 0.0299R of measured spread drag**, so the implied GROSS
expectancy is **+0.0064R** and the cost is what makes it negative. Choosing
the three cheapest pairs already took the drag from 0.0299R to 0.0146R. **If
timing could take it below 0.0064R the sign would flip**, so the target is a
number rather than a hope.

**Read that +0.0064R with the caution it deserves.** `trade_autopsy` puts the
residual after cost at t = 0.43, so a positive gross expectancy is well inside
noise and is NOT a demonstrated edge. What follows tests whether the DRAG can
be reduced, which is a separate and much better-evidenced question.

**Three measurements decide the design, and all three were made first.**

  * **51.1% of bars carry no recorded spread at all**, and the missingness
    trends: 44.9% of the first half of the sample against 57.0% of the second.
    `choose_spread` already treats a zero as unrecorded rather than free, and
    that convention is kept here. Every figure below is conditional on the
    recorded half, which is a real limitation and not a footnote.
  * **67.7% of spread variance is BETWEEN days**, not within them.
  * **Within-day autocorrelation is -0.120 at lag 1**, +0.035 at lag 2 and
    +0.007 at lag 6 -- nothing. The +0.6 autocorrelation visible at every lag
    in the raw series is entirely that slow between-day component.

So timing entries INSIDE a session is refuted before it is tried: knowing the
last bar's spread tells you nothing about the next bar's. What remains is
whether an expensive DAY can be recognised from the day before, which is where
two thirds of the variance lives.

**The control is the whole test, and it is the same shape as the close-in-range
confound.** Cheap-spread days are plausibly also quiet days, and a quiet day
offers less to win as well as less to pay. A filter that halves the cost and
halves the gross move has bought nothing. So the run reports the drag saved
AND the gross move forgone, and the verdict is the difference rather than the
saving.

    python tools/spread_timing_search.py
    python tools/spread_timing_search.py --buckets 3
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
from rule_backtest import connect, fetch_rates

SYMBOLS = ("USDJPY", "EURUSD", "GBPUSD", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD")
CHEAP_THREE = ("USDJPY", "EURUSD", "GBPUSD")


def daily_spread(times, spreads):
    """Mean RECORDED spread per day, and the count behind each mean.

    A zero is unrecorded rather than free -- `choose_spread` established that
    and averaging zeros in halves the apparent cost of trading. A day with no
    recorded bar at all yields no value rather than a zero.
    """
    days = np.array([dt.datetime.fromtimestamp(int(x), dt.UTC).date() for x in times])
    acc = collections.defaultdict(list)
    for d, s in zip(days, spreads):
        if s > 0:
            acc[d].append(float(s))
    out = {d: (float(np.mean(v)), len(v)) for d, v in acc.items()}
    return days, out


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
    ap.add_argument("--buckets", type=int, default=3)
    ap.add_argument("--cheap-only", action="store_true",
                    help="restrict to the three pairs the harness actually trades")
    ap.add_argument("--out", default="reports/spread_timing_search.json")
    args = ap.parse_args()
    k = args.buckets
    syms = CHEAP_THREE if args.cheap_only else SYMBOLS

    mt5 = connect()
    rows = []
    missing_tot = bars_tot = 0
    for sym in syms:
        r = fetch_rates(mt5, sym, args.bars, timeframe="H1")
        if r is None or len(r) < 10000:
            continue
        sp = r["spread"].astype(float)
        c = r["close"].astype(float)
        missing_tot += int((sp == 0).sum())
        bars_tot += len(sp)
        days, dmean = daily_spread(r["time"], sp)
        point = 1e-2 if sym.endswith("JPY") else 1e-4
        # Per-day realised move and the day's own mean spread, in RETURN units
        # so pairs pool: a spread in points is the instrument's point size,
        # which is the pooling error this repository documents.
        by = collections.defaultdict(list)
        for i, d in enumerate(days):
            by[d].append(i)
        order = sorted(by)
        prev = None
        for d in order:
            if d not in dmean:
                prev = d
                continue
            ii = np.array(by[d])
            px = c[ii]
            spread_ret = dmean[d][0] * point / float(np.median(px))
            move = float(np.abs(np.log(px[-1] / px[0])))          # gross available
            vol = float(np.std(np.diff(np.log(px)))) if len(px) > 2 else np.nan
            prior = dmean[prev][0] * point / float(np.median(px)) if (
                prev is not None and prev in dmean) else np.nan
            rows.append({"sym": sym, "date": d, "spread": spread_ret,
                         "prior_spread": prior, "move": move, "vol": vol,
                         "n_recorded": dmean[d][1]})
            prev = d
    mt5.shutdown()
    if not rows:
        return 1

    sprd = np.array([r["spread"] for r in rows])
    prior = np.array([r["prior_spread"] for r in rows])
    move = np.array([r["move"] for r in rows])
    ok = np.isfinite(prior) & np.isfinite(sprd) & np.isfinite(move)

    print(f"\n  spread timing, {len(syms)} majors, {len(rows):,} days")
    print(f"  {missing_tot:,} of {bars_tot:,} bars ({missing_tot / bars_tot * 100:.1f}%) "
          f"carry NO recorded spread and are excluded")
    rho = float(np.corrcoef(prior[ok], sprd[ok])[0, 1])
    print(f"  day-to-day spread persistence: corr(yesterday, today) = {rho:+.3f}")

    # Bucket TODAY by YESTERDAY's spread -- the only information available
    # before the day starts.
    edges = np.quantile(prior[ok], np.linspace(0, 1, k + 1)[1:-1])
    b = np.digitize(prior[ok], edges)
    s_ok, m_ok = sprd[ok], move[ok]

    print("  " + "-" * 72)
    print(f"  {'bucket':22}{'days':>8}{'spread today':>15}{'gross move':>13}"
          f"{'move/spread':>13}")
    out = []
    for j in range(k):
        m = b == j
        sm, mm = float(s_ok[m].mean()), float(m_ok[m].mean())
        ratio = mm / sm if sm > 0 else float("nan")
        tag = ("cheapest prior day" if j == 0 else
               "dearest prior day" if j == k - 1 else f"bucket {j}")
        print(f"  {tag:22}{int(m.sum()):>8,}{sm * 100:>14.4f}%{mm * 100:>12.4f}%"
              f"{ratio:>13.2f}")
        out.append({"bucket": j, "days": int(m.sum()),
                    "spread_pct": sm * 100, "move_pct": mm * 100,
                    "move_over_spread": ratio})

    cheap, dear = out[0], out[-1]
    saved = (dear["spread_pct"] - cheap["spread_pct"]) / dear["spread_pct"] * 100
    forgone = (dear["move_pct"] - cheap["move_pct"]) / dear["move_pct"] * 100
    print("  " + "-" * 72)
    print(f"  trading only after a cheap day saves {saved:.1f}% of the spread")
    print(f"  but forgoes {forgone:.1f}% of the gross move available")
    print(f"  move-per-unit-spread: {cheap['move_over_spread']:.2f} cheap vs "
          f"{dear['move_over_spread']:.2f} dear")

    # The number that decides it: expectancy after the filter, using the
    # repository's own measured figures.
    GROSS_R, DRAG_R = 0.0064, 0.0146
    scale = cheap["spread_pct"] / float(np.average(
        [o["spread_pct"] for o in out], weights=[o["days"] for o in out]))
    new_drag = DRAG_R * scale
    gross_scale = cheap["move_pct"] / float(np.average(
        [o["move_pct"] for o in out], weights=[o["days"] for o in out]))
    new_gross = GROSS_R * gross_scale
    print("  " + "-" * 72)
    print(f"  applying that to the live record ({GROSS_R:+.4f}R gross, "
          f"{DRAG_R:.4f}R drag on the cheap three):")
    print(f"    drag  {DRAG_R:.4f}R -> {new_drag:.4f}R   (x{scale:.3f})")
    print(f"    gross {GROSS_R:+.4f}R -> {new_gross:+.4f}R   (x{gross_scale:.3f})")
    print(f"    net   {GROSS_R - DRAG_R:+.4f}R -> {new_gross - new_drag:+.4f}R")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"symbols": list(syms), "days": len(rows),
                   "missing_bar_share": missing_tot / bars_tot,
                   "day_persistence": rho, "buckets": out,
                   "spread_saved_pct": saved, "move_forgone_pct": forgone,
                   "applied": {"gross_r": GROSS_R, "drag_r": DRAG_R,
                               "new_drag_r": new_drag, "new_gross_r": new_gross,
                               "net_before": GROSS_R - DRAG_R,
                               "net_after": new_gross - new_drag}}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
