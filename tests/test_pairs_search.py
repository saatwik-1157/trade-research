#!/usr/bin/env python
"""The pairs search, and whether its null was earned.

A spread trade is the one family in this repository where the NULL is the
thing that looks like a finding. Two independent random walks regress on each
other with a significant slope most of the time -- Granger and Newbold's
spurious regression -- so a fitted spread that looks tradable is what the null
DOES here, not evidence against it. A search that could not tell those apart
would have reported a discovery.

So these checks ask the four questions that decide whether the verdict means
anything:

  * **No lookahead.** Fitting beta over the whole sample makes the spread
    mean-zero across exactly the period being traded, so it reverts by
    construction. This is checked by TRUNCATION -- the signal at bar t must be
    identical whether or not the series after t exists -- which is stronger
    than eyeballing the slice arithmetic.
  * **Power.** A genuinely cointegrated pair must be found, or a null is
    blindness rather than evidence.
  * **Restraint.** Independent random walks must NOT be found, which is the
    spurious-regression case and the one that would quietly become a result.
  * **Cost.** A pair crosses the spread FOUR times a round trip, both legs in
    and both out. Charging two would halve the hurdle and flatter every row.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from pairs_search import hedge_ratio, pair_pnl, sharpe_t, spread_z  # noqa: E402

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


rng = np.random.default_rng(11)

print()
print("The hedge ratio is fitted, not assumed")
b = np.cumsum(rng.normal(0, 1, 5000))
a = 2.40 * b + rng.normal(0, 0.01, 5000)
check("a planted ratio is recovered", round(hedge_ratio(a, b), 2), 2.40)
check("a flat series has no ratio rather than a divide by zero",
      hedge_ratio(a, np.zeros(5000)) != hedge_ratio(a, np.zeros(5000)), True)

print()
print("The signal at bar t does not depend on anything after bar t")
n = 4000
pa = np.cumsum(rng.normal(0, 0.001, n))
pb = np.cumsum(rng.normal(0, 0.001, n))
full, _ = spread_z(pa, pb, fit=300, look=120, refit=60)
# TRUNCATION is the decisive form of this check. If beta or the z-score
# reached forward by even one bar, cutting the series would move the value.
# A slice-arithmetic eyeball would not catch a refit that peeked.
cut = 3000
trunc, _ = spread_z(pa[:cut], pb[:cut], fit=300, look=120, refit=60)
check("truncating the future leaves every earlier z unchanged",
      bool(np.allclose(full[:cut], trunc, equal_nan=True)), True)
# And it must not be trivially satisfied by emitting nothing.
check("the truncated run still produced a live signal",
      bool(np.isfinite(trunc).sum() > 1000), True)

print()
print("Cost is charged on both legs, entering and exiting")
z = np.full(60, np.nan)
z[10], z[30] = 3.0, 0.0
betas = np.full(60, 1.0)
flat = np.zeros(60)
free, _ = pair_pnl(flat, flat, z, betas, 0.0, 0.0, 2.0, 0.5)
paid, trips = pair_pnl(flat, flat, z, betas, 0.001, 0.002, 2.0, 0.5)
check("a round trip is counted once", trips, 1)
# in: |1/2| * 0.001 + |1/2| * 0.002 ; out: the same again -> 0.003
check("four crossings are charged, not two",
      round(float(free.sum() - paid.sum()), 6), 0.003)
z2 = np.full(60, 3.0)
held, trips2 = pair_pnl(flat, flat, z2, betas, 0.001, 0.002, 2.0, 0.5)
check("a position held is not re-charged every bar",
      round(float(-held.sum()), 6), 0.0015)
check("and holding is one trip, not sixty", trips2, 1)

print()
print("It finds a real cointegrated pair and not an imaginary one")


def _walk(planted: bool, n: int = 20000, seed: int = 5):
    r = np.random.default_rng(seed)
    pb = np.cumsum(r.normal(0, 0.001, n))
    if planted:
        err = np.zeros(n)
        for i in range(1, n):
            err[i] = 0.90 * err[i - 1] + r.normal(0, 0.002)
        pa = 1.30 * pb + err
    else:
        pa = np.cumsum(r.normal(0, 0.001, n))
    ra, rb = np.diff(pa, prepend=pa[0]), np.diff(pb, prepend=pb[0])
    zz, bb = spread_z(pa, pb, 1000, 200, 250)
    pnl, _ = pair_pnl(ra, rb, zz, bb, 0.0, 0.0, 2.0, 0.5)
    return sharpe_t(pnl)[1]


t_real, t_noise = _walk(True), _walk(False)
print(f"        planted t={t_real:+.2f}   independent walks t={t_noise:+.2f}")
check("a planted cointegrated pair is found", bool(t_real > 5.0), True)
check("independent random walks are not", bool(abs(t_noise) < 3.0), True)
check("and the planted pair scores far above the walks",
      bool(t_real > abs(t_noise) * 3), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall pairs-search checks passed")
