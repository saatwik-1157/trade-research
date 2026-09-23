#!/usr/bin/env python
"""The four bookshelf rules, and whether they were actually tested.

The failure mode for a candidate list is not a wrong number, it is a rule
that never fires. A signal stuck at zero produces "no trades", reads as a
null, and is indistinguishable in the report from a rule that was tried and
failed. `three_methods` fired ZERO times in 3,000 synthetic bars while
looking perfectly healthy, which is how this check came to exist.

So these ask two separate questions of every candidate: is the construction
right, and does it fire.

One measured curiosity is pinned here too. `soldiers` fires once in 3,000
random-walk bars and 21,089 times across seven real pairs, because an FX bar
opens almost exactly on the previous close -- so "opens inside the previous
body" is nearly free, and in FX the pattern degenerates towards three
consecutive up-closes. That is momentum at n=3, which is refuted ground, and
it is the reason the result is not read as a candlestick finding.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from book_rules_search import (  # noqa: E402
    build_book_candidates,
    heikin_ashi,
    parabolic_sar,
    regression_slope,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


print()
print("Parabolic SAR accelerates on new extremes and flips")
n = 300
up = np.arange(n, dtype=float)
h, l = up + 1.0, up - 1.0
sar = parabolic_sar(h, l)
check("the band trails BELOW price in a pure uptrend",
      bool(np.all(sar[10:] < h[10:])), True)
# The acceleration is the mechanism: a longer run must close the gap.
gap_early = float(h[20] - sar[20])
gap_late = float(h[250] - sar[250])
check("and the gap tightens as the run extends", bool(gap_late < gap_early), True)
vee = np.concatenate([np.arange(150, dtype=float), np.arange(150, 0, -1, dtype=float)])
sar_v = parabolic_sar(vee + 1.0, vee - 1.0)
above = (vee + 1.0) > sar_v
check("a reversal flips the band to the other side of price",
      bool(above[100] and not above[-10]), True)

print()
print("The regression slope reads the interior of its window")
n = 500
rising = np.linspace(1.0, 2.0, n)
s = regression_slope(rising, 100)
check("a rising line gives a positive slope", bool(s[-1] > 0), True)
check("a falling line gives a negative slope",
      bool(regression_slope(rising[::-1].copy(), 100)[-1] < 0), True)
check("the first n-1 bars have no slope rather than a wrong one",
      bool(np.all(~np.isfinite(s[:99]))), True)
# Causality: the slope at t must not move when the future is deleted.
full = regression_slope(np.asarray(rising), 100)
trunc = regression_slope(np.asarray(rising[:400]), 100)
check("truncating the future leaves earlier slopes unchanged",
      bool(np.allclose(full[:400], trunc, equal_nan=True)), True)

print()
print("Heikin Ashi carries state, which is why it is not single-bar shape")
o = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
h = np.array([2.0, 2.0, 2.0, 2.0, 2.0])
lo = np.array([0.0, 0.0, 0.0, 0.0, 0.0])
c = np.array([1.5, 1.5, 1.5, 1.5, 1.5])
ha_o, ha_c = heikin_ashi(o, h, lo, c)
check("the HA close is the mean of the four prices",
      round(float(ha_c[0]), 4), round((1.0 + 2.0 + 0.0 + 1.5) / 4, 4))
check("the HA open is seeded from the first bar", round(float(ha_o[0]), 4), 1.25)
check("and every later open depends on the PREVIOUS bar, not this one",
      round(float(ha_o[1]), 4), round((1.25 + 1.125) / 2, 4))
# Identical raw bars produce DIFFERENT HA bars -- the definition of state.
check("identical raw bars still give a varying HA open",
      bool(len(set(np.round(ha_o, 6))) > 1), True)

print()
print("The multi-bar patterns fire on the pattern and not on noise")
CAND = dict((name, f) for name, _fam, f in build_book_candidates())
# Three White Soldiers, built to the definition: three white bodies, each
# closing beyond the last, each opening inside the previous body.
o = np.array([10.0, 10.5, 11.5, 12.5])
c = np.array([11.0, 12.0, 13.0, 14.0])
h = c + 0.1
lo = o - 0.1
sig = CAND["soldiers_ride"](o, h, lo, c)
check("three soldiers are detected at the third bar", float(sig[3]), 1.0)
check("and nothing fires before the pattern completes",
      bool(np.all(sig[:2] == 0.0)), True)
check("the fade variant is the exact inverse",
      float(CAND["soldiers_fade"](o, h, lo, c)[3]), -1.0)

# Rising Three Methods: a long white bar, three small bars inside its range,
# then a close above the first bar's close.
o = np.array([10.0, 12.0, 11.8, 12.1, 12.5])
c = np.array([13.0, 11.9, 12.2, 11.9, 13.5])
h = np.array([13.1, 12.1, 12.3, 12.2, 13.6])
lo = np.array([9.9, 11.7, 11.7, 11.8, 12.4])
check("rising three methods is detected at the fifth bar",
      float(CAND["three_methods_ride"](o, h, lo, c)[4]), 1.0)

print()
print("Every candidate actually fires -- a silent rule is not a null")
rng = np.random.default_rng(5)
n = 20000
# Realistic bars: the open sits on the PREVIOUS close, as an FX bar does.
ret = rng.normal(0, 0.0008, n)
cl = 1.1 * np.exp(np.cumsum(ret))
op = np.concatenate(([cl[0]], cl[:-1]))
hi = np.maximum(op, cl) + np.abs(rng.normal(0, 0.0003, n))
lowp = np.minimum(op, cl) - np.abs(rng.normal(0, 0.0003, n))
silent, bad_values = [], []
for name, _fam, f in build_book_candidates():
    s = f(op, hi, lowp, cl)
    if len(s) != n or not set(np.unique(s)).issubset({-1.0, 0.0, 1.0}):
        bad_values.append(name)
    if int((np.diff(s) != 0).sum()) < 20:
        silent.append(name)
check("no candidate is silent on 20,000 realistic bars", silent, [])
check("every signal is -1, 0 or +1 and the right length", bad_values, [])
check("sixteen candidates, as the Bonferroni count assumes",
      len(build_book_candidates()), 16)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall book-rule checks passed")
