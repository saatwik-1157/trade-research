#!/usr/bin/env python
"""Regime conditioning, and the one-bar error that manufactured 41% a year.

This file exists because of a defect, and the defect is worth more than the
result. Conditioning a rule's P&L on a regime variable is only meaningful if
the variable is known BEFORE the bar it groups. The first version used the
contemporaneous value, so the efficiency ratio at bar t contained the very
return the trend rule earned at bar t -- and a trend position profits exactly
when price travels far in one direction, which is exactly what raises ER.

Bucketing an outcome by a variable containing that outcome produced:

    top bucket +41.6% annualised, t = +21.63, Spearman +1.000 (perfect),
    permutation null p = 0.000

Lagged one bar, the top-minus-bottom spread falls from +28.44bp to -1.29bp.
The whole thing was the timing.

**The permutation null could not have caught it**, which is why it is not the
check that matters here. Shifting the conditioner destroys exactly the
contemporaneous alignment the artefact needs, so it passes with the bug
present and with the bug absent. Only a lag test finds it, so there is one
below that reproduces the tautology in miniature and then removes it.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from regime_search import (  # noqa: E402
    ER_N, bucket, efficiency_ratio, profile, realised_vol, spearman, tstat,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


print()
print("The efficiency ratio is a ratio, so it cannot exceed one")
line = np.arange(100, 200, dtype=float)
saw = 100 + np.tile([0.0, 1.0], 50)
check("a perfectly straight move scores exactly 1",
      round(float(np.nanmean(efficiency_ratio(line))), 6), 1.0)
check("a pure sawtooth scores 0",
      round(float(np.nanmean(efficiency_ratio(saw))), 6), 0.0)
rng = np.random.default_rng(3)
walk = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 3000)))
er = efficiency_ratio(walk)
# The original slice was [n-1 : len+n-1], which shifts the rolling denominator
# FORWARD and pulls future bars into it. Measured, that put ER at 1.65 on a
# straight line and as high as 10.6 on a walk -- impossible for a ratio.
check("and a random walk never exceeds one either",
      bool(np.nanmax(er) <= 1.0 + 1e-12), True)
check("nor falls below zero", bool(np.nanmin(er) >= 0.0), True)
check("the first n bars have no value rather than a wrong one",
      bool(np.all(~np.isfinite(er[:ER_N]))), True)

print()
print("Both conditioners use only the past, checked by truncation")
check("truncating the future leaves earlier ER unchanged",
      bool(np.allclose(er[:2000], efficiency_ratio(walk[:2000]), equal_nan=True)),
      True)
rv = realised_vol(walk)
check("and earlier realised vol unchanged",
      bool(np.allclose(rv[:2000], realised_vol(walk[:2000]), equal_nan=True)),
      True)

print()
print("Buckets are quantiles, so they fill evenly and ignore blanks")
x = np.concatenate((rng.normal(0, 1, 5000), np.full(500, np.nan)))
b = bucket(x, 5)
sizes = [int((b == j).sum()) for j in range(5)]
check("five buckets of near-equal size",
      bool(max(sizes) - min(sizes) <= 2), True)
check("blank values land in no bucket", int((b[5000:] != -1).sum()), 0)
check("too little data returns no buckets at all",
      int((bucket(np.arange(10.0), 5) != -1).sum()), 0)

print()
print("The profiler recovers a monotone relationship and reports none when absent")
cond = rng.random(5000)
pnl = (cond - 0.5) * 0.001 + rng.normal(0, 0.0005, 5000)
_m, _n, rho = profile(pnl, cond, 5)
check("a planted monotone relation gives Spearman +1", round(rho, 3), 1.0)
check("spearman is symmetric under reversal",
      round(spearman(np.arange(5.0), -np.arange(5.0)), 3), -1.0)

print()
print("THE LAG: a conditioner containing the outcome invents a perfect trend")
# The tautology in miniature. `noise` is the bar's own return; the conditioner
# is built from it, exactly as ER is built from the closes whose move the
# trend rule just captured.
n = 20000
ret = rng.normal(0, 0.01, n)
pnl_t = ret.copy()                       # the rule earned the bar's return
contemp = np.abs(ret)                    # a conditioner containing that return
_m0, _n0, rho0 = profile(np.abs(pnl_t), contemp, 5)
check("unlagged, the relation is perfect and entirely circular",
      round(rho0, 3), 1.0)
lagged = np.concatenate(([np.nan], contemp[:-1]))
_m1, _n1, rho1 = profile(np.abs(pnl_t), lagged, 5)
check("lagged one bar, it collapses", bool(abs(rho1) < 0.9), True)
m0, _, _ = profile(np.abs(pnl_t), contemp, 5)
m1, _, _ = profile(np.abs(pnl_t), lagged, 5)
spread0 = m0[-1] - m0[0]
spread1 = m1[-1] - m1[0]
check("and the top-minus-bottom spread loses almost all of its size",
      bool(abs(spread1) < abs(spread0) / 20), True)
# And the reason the permutation null is not the safeguard here.
shifted = np.roll(contemp, 7777)
_m2, _n2, rho2 = profile(np.abs(pnl_t), shifted, 5)
check("a SHIFTED conditioner also collapses, so the null cannot tell the "
      "two apart", bool(abs(rho2) < 0.9), True)

print()
print("The t-statistic refuses samples too small to carry one")
check("a handful of observations gives nan",
      tstat(np.zeros(5)) != tstat(np.zeros(5)), True)
check("a zero-mean sample gives a small t",
      bool(abs(tstat(rng.normal(0, 1, 5000))) < 3.0), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall regime checks passed")
