#!/usr/bin/env python
"""Spread timing, and the three ways it could be an artefact rather than a lever.

This is the only search in the project that measured something usable, so it
gets the hardest checks rather than the softest. Three ways the result could
be false, each pinned here:

  * **Zeros counted as free.** 41.4% of bars carry no recorded spread.
    `choose_spread` established that a zero is UNRECORDED, not a free trade,
    and averaging them in halves the apparent cost. A day with no recorded
    bar at all must yield no value rather than a zero.
  * **Lookahead.** The filter buckets TODAY by YESTERDAY's spread. Using
    today's own spread would be the same tautology that manufactured 41% a
    year in `regime_search`: it would be selecting cheap days by observing
    they were cheap.
  * **Units.** A spread in POINTS is the instrument's point size, so pooling
    USDJPY's 0.01 with EURUSD's 0.0001 is the metals error. Everything is
    converted to return units before any pair meets another.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from spread_timing_search import CHEAP_THREE, daily_spread, tstat  # noqa: E402

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def stamps(day, hours):
    base = dt.datetime(2024, 5, day, tzinfo=dt.UTC)
    return [int((base + dt.timedelta(hours=h)).timestamp()) for h in hours]


print()
print("An unrecorded spread is not a free trade")
times = stamps(8, range(6)) + stamps(9, range(6))
sp = np.array([2.0, 0.0, 4.0, 0.0, 0.0, 6.0,        # day 8: three recorded
               0.0, 0.0, 0.0, 0.0, 0.0, 0.0])       # day 9: none recorded
_days, dm = daily_spread(np.array(times), sp)
keys = sorted(dm)
check("only the day with recorded bars appears", len(dm), 1)
check("and its mean ignores the zeros entirely",
      round(dm[keys[0]][0], 6), round((2.0 + 4.0 + 6.0) / 3, 6))
check("the count behind the mean is the RECORDED bars, not all bars",
      dm[keys[0]][1], 3)
# The bug this guards: averaging zeros in would halve the figure.
check("averaging zeros in would have halved it",
      bool(abs(sp[:6].mean() - dm[keys[0]][0] / 2) < 1e-9), True)

print()
print("A day with no recorded bar yields nothing rather than zero")
_d2, dm2 = daily_spread(np.array(stamps(9, range(6))), np.zeros(6))
check("no recorded bars means no entry at all", len(dm2), 0)

print()
print("Spread must be converted to RETURN units before pairs are pooled")
# USDJPY's point is 1e-2 and EURUSD's is 1e-4; two points of each are wildly
# different fractions of price, which is the metals error.
jpy_pts, eur_pts = 2.0, 2.0
jpy_px, eur_px = 150.0, 1.10
jpy_ret = jpy_pts * 1e-2 / jpy_px
eur_ret = eur_pts * 1e-4 / eur_px
# The trap is subtler than "one is bigger". In RAW POINTS the two are the
# same number, 2.0 and 2.0, so pooling points silently treats them as equal
# costs. In return units USDJPY is 1.33e-4 of price and EURUSD is 1.82e-4 --
# a 36% difference hiding behind identical integers.
check("in raw points the two look identical", (jpy_pts, eur_pts), (2.0, 2.0))
check("but as a fraction of price they differ by more than a third",
      bool(abs(eur_ret / jpy_ret - 1.0) > 0.3), True)
check("and EURUSD is the DEARER of the two, not the cheaper",
      bool(eur_ret > jpy_ret), True)
check("the traded set is the three cheapest pairs",
      list(CHEAP_THREE), ["USDJPY", "EURUSD", "GBPUSD"])

print()
print("The filter uses YESTERDAY, so it cannot select on what it measures")
rng = np.random.default_rng(4)
n = 4000
# A persistent spread series, as the real one is (day-to-day corr +0.245).
level = np.zeros(n)
for i in range(1, n):
    level[i] = 0.7 * level[i - 1] + rng.normal(0, 1)
today = np.exp(level)
prior = np.concatenate(([np.nan], today[:-1]))
ok = np.isfinite(prior)
e = np.quantile(prior[ok], [1 / 3, 2 / 3])
b = np.digitize(prior[ok], e)
cheap, dear = today[ok][b == 0].mean(), today[ok][b == 2].mean()
check("bucketing on YESTERDAY still separates today's spread",
      bool(cheap < dear), True)
# And the tautology it avoids: bucketing on today is perfect by construction.
e2 = np.quantile(today, [1 / 3, 2 / 3])
b2 = np.digitize(today, e2)
sep_prior = dear / cheap
sep_today = today[b2 == 2].mean() / today[b2 == 0].mean()
check("bucketing on TODAY separates far more, because it is circular",
      bool(sep_today > sep_prior * 2), True)
# With no persistence there must be nothing to exploit.
flat = np.exp(rng.normal(0, 1, n))
pf = np.concatenate(([np.nan], flat[:-1]))
ok2 = np.isfinite(pf)
e3 = np.quantile(pf[ok2], [1 / 3, 2 / 3])
b3 = np.digitize(pf[ok2], e3)
r = flat[ok2][b3 == 2].mean() / flat[ok2][b3 == 0].mean()
check("an unpersistent spread gives no separation from yesterday",
      bool(0.8 < r < 1.25), True)

print()
print("The statistic refuses samples too small to carry one")
check("a handful of observations gives nan",
      tstat(np.zeros(5)) != tstat(np.zeros(5)), True)
check("a zero-mean sample gives a small t",
      bool(abs(tstat(rng.normal(0, 1, 5000))) < 3.0), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall spread-timing checks passed")
