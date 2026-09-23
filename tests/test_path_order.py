#!/usr/bin/env python
"""Intrabar path ordering, and whether the confound was really held fixed.

This search's feature is very nearly a restatement of something already
refuted. Where a bar closes in its own range almost determines which extreme
came first -- 96.9% high-first in the bottom close decile against 3.7% in the
top -- and `shape_search` refuted candle shape across 28 candidates. So an
unconditional test here would rediscover that result and report it as new.

Everything therefore rests on two properties, and neither is about the
verdict:

  * **The reconstruction is right.** Which H1 bar holds the day's high, which
    holds its low, and the honest handling of the case where one bar holds
    both. A wrong answer here is invisible: the output still looks like a
    clean binary feature.
  * **The null holds the confound EXACTLY.** Shuffling the ordering label
    across the whole sample would destroy the close-in-range relationship as
    well as the path information, and would therefore test whether
    close-in-range predicts returns -- a question already answered. Shuffling
    inside each decile must leave P(high-first | decile) untouched, and that
    is asserted by counting rather than assumed from the code.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import path_order_search as pos  # noqa: E402
from path_order_search import (  # noqa: E402
    N_DECILES, reconstruct, stratified_diff, welch,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def _day(highs, lows, opens=None, closes=None):
    base = dt.datetime(2024, 5, 8, tzinfo=dt.UTC)
    n = len(highs)
    t = np.array([int((base + dt.timedelta(hours=i)).timestamp()) for i in range(n)])
    o = np.full(n, 100.0) if opens is None else np.asarray(opens, float)
    c = np.full(n, 100.0) if closes is None else np.asarray(closes, float)
    return t, o, np.asarray(highs, float), np.asarray(lows, float), c


print()
print("Which extreme came first is read off the H1 bars, not inferred")
flat_h = [100.0] * 8
flat_l = [100.0] * 8
h = list(flat_h)
low = list(flat_l)
h[2] = 105.0
low[5] = 95.0
rows, amb, _t = reconstruct(*_day(h, low))
check("high in an earlier bar than the low is high-first",
      bool(rows[0]["high_first"]), True)
check("and nothing is ambiguous", amb, 0)

h = list(flat_h)
low = list(flat_l)
h[5] = 105.0
low[2] = 95.0
rows2, _a, _t = reconstruct(*_day(h, low))
check("low in an earlier bar than the high is low-first",
      bool(rows2[0]["high_first"]), False)

# The one case the feature genuinely cannot resolve.
h = list(flat_h)
low = list(flat_l)
h[3] = 105.0
low[3] = 95.0
rows3, amb3, _t = reconstruct(*_day(h, low))
check("both extremes inside ONE H1 bar is ambiguous", amb3, 1)
check("and an ambiguous day is dropped rather than guessed", len(rows3), 0)

# A holiday stub is not a day.
short = _day([100.0] * 3, [99.0] * 3)
rows4, _a, thin4 = reconstruct(*short)
check("a day with too few hours is rejected", (len(rows4), thin4), (0, 1))

# close-in-range is computed from the reconstructed extremes, not the H1 close.
h = list(flat_h)
low = list(flat_l)
h[1] = 110.0
low[6] = 90.0
closes = [100.0] * 8
closes[-1] = 105.0
rows5, _a, _t = reconstruct(*_day(h, low, closes=closes))
check("close-in-range uses the day's own high and low",
      round(rows5[0]["cir"], 6), round((105.0 - 90.0) / (110.0 - 90.0), 6))


print()
print("The stratified estimate ignores strata the confound has emptied")
rng = np.random.default_rng(7)
n = 14000
dec = rng.integers(0, N_DECILES, n)
hf = rng.random(n) < 0.5
# Empty one decile of its minority class, as the real extremes are.
starved = dec == 3
hf[starved] = True
hf[np.flatnonzero(starved)[:5]] = False
fwd = rng.normal(0, 0.006, n)
_e, _t, used = stratified_diff(fwd, hf, dec)
check("a decile with almost no minority class is not judged",
      3 in used, False)
check("and the populated deciles all are", len(used), N_DECILES - 1)


print()
print("It finds an ordering effect planted INSIDE the strata")
dec = rng.integers(0, N_DECILES, n)
hf = rng.random(n) < 0.5
noise = rng.normal(0, 0.006, n)
e_base, t_base, _u = stratified_diff(noise, hf, dec)
check("the same noise with no effect gives a small t", bool(abs(t_base) < 2.5), True)
planted = 0.0004
e1, t1, _u = stratified_diff(noise + hf * planted, hf, dec)
check("a planted 0.04% effect is found", bool(t1 > 3.0), True)
# The recovered figure is the planted effect PLUS whatever difference this
# particular noise draw already had between the classes. Requiring it to equal
# the planted value alone asks the estimator to remove sampling error, which
# is not its job -- so the identity is asserted against the SAME draw.
check("and it recovers planted + that draw's own baseline, exactly",
      bool(abs(e1 - (planted + e_base)) < 1e-12), True)
check("the effect moves t well clear of the same draw's baseline",
      bool(t1 - t_base > 3.0), True)


print()
print("The null holds the confound EXACTLY and destroys only the ordering")
# Build a sample where close-in-range strongly determines the ordering, as
# the real data does, then shuffle within deciles and require the
# relationship to survive untouched.
dec = rng.integers(0, N_DECILES, n)
p_hf = 0.97 - 0.94 * (dec / (N_DECILES - 1))        # 97% down to 3%
hf = rng.random(n) < p_hf
before = np.array([hf[dec == k].mean() for k in range(N_DECILES)])
shuffled = hf.copy()
for k in range(N_DECILES):
    m = np.flatnonzero(dec == k)
    shuffled[m] = rng.permutation(shuffled[m])
after = np.array([shuffled[dec == k].mean() for k in range(N_DECILES)])
check("P(high-first | decile) is identical after the shuffle",
      bool(np.allclose(before, after)), True)
check("the confound really is strong in this fixture",
      bool(before[0] - before[-1] > 0.8), True)
check("but the labels genuinely moved",
      bool((shuffled != hf).sum() > n // 10), True)
# A GLOBAL shuffle must break it, which is why it is the wrong null here.
globalled = rng.permutation(hf)
after_g = np.array([globalled[dec == k].mean() for k in range(N_DECILES)])
check("a global shuffle would flatten the confound, so it is the wrong null",
      bool(abs(after_g[0] - after_g[-1]) < 0.2), True)


print()
print("Welch, not Student, since the two path classes need not match in variance")
check("unequal variances still return a finite t",
      bool(np.isfinite(welch(rng.normal(0, 0.01, 400),
                             rng.normal(0, 0.0001, 400))[1])), True)
check("too few observations give nan rather than a number",
      welch(np.zeros(3), np.zeros(3))[1] != welch(np.zeros(3), np.zeros(3))[1],
      True)
check("the thin-stratum floor is a stated constant, not a magic number",
      pos.MIN_PER_CLASS >= 40, True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall path-ordering checks passed")
