#!/usr/bin/env python
"""Round numbers, and the rounding bug that invented a finding.

The first version of this measurement converted prices to whole pips with
`np.rint`. That is numpy's banker's rounding: a value exactly at half a pip
goes to the nearest EVEN pip. About a tenth of this broker's quotes sit
exactly on a half pip, so every one of them was pushed onto an even digit.

The result looked like a discovery. The last whole-pip digit came out 1.07x
expected on even digits and 0.93x on odd, **identically in all seven majors**,
and the digit profiles correlated +0.607 across pairs -- which is exactly what
a real shared microstructure effect would look like. It was the rounding mode.
Through integer tenths of a pip the split collapses to 1.0014 / 0.9986 and the
cross-pair correlation falls to +0.084.

So the first check here is that `to_pips` has no tie, asserted on the exact
values that triggered it. The rest establish that the pierce test detects what
it claims and that the offset control really moves the grid -- because the
whole verdict rests on the round level being compared against arbitrary ones.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from round_number_search import (  # noqa: E402
    digit_profile, neighbourhood, pierce_test, to_pips,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


print()
print("Converting to pips must not round half to even")
pip = 1e-4
# Prices sitting EXACTLY on half a pip, straddling an even and an odd pip.
half_to_even = 1.23445      # 12344.5 pips -> rint sends it to 12344 (even)
half_to_odd = 1.23435       # 12343.5 pips -> rint sends it to 12344 (even too)
check("a half-pip above an EVEN pip does not jump to it",
      int(to_pips([half_to_even], pip)[0]), 12344)
check("and a half-pip above an ODD pip also floors, not rounds to even",
      int(to_pips([half_to_odd], pip)[0]), 12343)
# The bug: np.rint sends BOTH of those to 12344, erasing the odd pip.
check("np.rint would have collapsed both onto the same even pip",
      (int(np.rint(half_to_even / pip)), int(np.rint(half_to_odd / pip))),
      (12344, 12344))
check("to_pips keeps them distinct",
      int(to_pips([half_to_even], pip)[0]) != int(to_pips([half_to_odd], pip)[0]),
      True)
# And on a uniform sweep of sub-pip values the digits must stay balanced.
sweep = 1.20000 + np.arange(20000) * (pip / 10)
d = to_pips(sweep, pip) % 10
counts = np.bincount(d, minlength=10)
check("a uniform sub-pip sweep gives a flat last digit",
      bool(counts.max() - counts.min() <= 1), True)
# The mechanism itself, shown on EXACT half-integers so no float
# representation is involved. Building a sweep by repeated addition does not
# land on exact halves, which is why that version of this check passed
# vacuously; real broker quotes do reach the tie, and the even/odd skew it
# produced on 350,000 bars was 1.07 against 0.93.
halves = np.arange(12000, 12100) + 0.5
rounded = np.rint(halves).astype(np.int64)
check("np.rint sends EVERY exact half to an even integer",
      int((rounded % 2 != 0).sum()), 0)
# 100 halves collapse onto the 51 even integers from 12000 to 12100
# inclusive, so rint can never produce an odd pip at all.
check("so odd pips are unreachable and the range compresses by half",
      (len(set(rounded.tolist())), int((rounded % 2).sum())), (51, 0))


print()
print("The digit profile bins extremes by position in the cycle")
highs = np.array([1.2000, 1.2050, 1.2099, 1.2000])
lows = np.array([1.1900, 1.1950, 1.1901, 1.1900])
prof = digit_profile(highs, lows, pip, 100)
check("the profile has one slot per pip in the cycle", len(prof), 100)
check("every extreme is counted exactly once", int(prof.sum()), 8)
check("two highs exactly on the round level land on slot 0", int(prof[0]), 4)
flat = np.ones(100)
check("a flat profile has a neighbourhood of exactly 1",
      round(neighbourhood(flat, 100, 3), 9), 1.0)


print()
print("A pierce is detected, and its sign is reversal-positive")
# Close below the level, then a bar whose HIGH crosses it, then a fall.
closes = np.array([1.1990, 1.1995, 1.1980])
highs = np.array([1.1992, 1.2005, 1.1996])
lows = np.array([1.1988, 1.1990, 1.1975])
s = pierce_test(None, highs, lows, closes, pip, 100, 0, 1)
check("an upward pierce followed by a fall scores POSITIVE",
      bool(len(s) == 1 and s[0] > 0), True)
closes_up = np.array([1.1990, 1.1995, 1.2010])
s2 = pierce_test(None, highs, lows, closes_up, pip, 100, 0, 1)
check("an upward pierce followed by a rise scores negative",
      bool(len(s2) == 1 and s2[0] < 0), True)
# A downward pierce mirrors.
c_dn = np.array([1.2010, 1.2005, 1.2020])
h_dn = np.array([1.2012, 1.2008, 1.2025])
l_dn = np.array([1.2008, 1.1995, 1.2015])
s3 = pierce_test(None, h_dn, l_dn, c_dn, pip, 100, 0, 1)
check("a downward pierce followed by a rise scores POSITIVE",
      bool(len(s3) == 1 and s3[0] > 0), True)
# A bar that never reaches the level is not a pierce.
s4 = pierce_test(None, np.array([1.1992, 1.1994, 1.1996]),
                 np.array([1.1988, 1.1990, 1.1992]),
                 np.array([1.1990, 1.1993, 1.1995]), pip, 100, 0, 1)
check("a bar that never reaches a level is not a pierce", len(s4), 0)


print()
print("The offset control really moves the grid off the round number")
rng = np.random.default_rng(5)
px = 1.2 * np.exp(np.cumsum(rng.normal(0, 0.0004, 20000)))
hi = px + np.abs(rng.normal(0, 0.0002, 20000))
lo = px - np.abs(rng.normal(0, 0.0002, 20000))
n0 = len(pierce_test(None, hi, lo, px, pip, 100, 0, 1))
n37 = len(pierce_test(None, hi, lo, px, pip, 100, 37, 1))
check("an offset grid still produces a comparable number of pierces",
      bool(0.5 < n37 / max(1, n0) < 2.0), True)
same = pierce_test(None, hi, lo, px, pip, 100, 0, 1)
moved = pierce_test(None, hi, lo, px, pip, 100, 37, 1)
check("but they are DIFFERENT pierces, not the same set relabelled",
      bool(len(same) != len(moved) or not np.allclose(same, moved)), True)


print()
print("It would find a round-number effect if one were there")
# Plant one: whenever a bar pierces a round level, force the next bar back.
px2 = px.copy()
hp = to_pips(px2, pip)
lvl = (hp % 100 == 0)
planted = px2.copy()
planted[1:][lvl[:-1]] *= 0.9985          # snap back after touching a level
s_real = pierce_test(None, hi, lo, planted, pip, 100, 0, 1)
s_ctrl = pierce_test(None, hi, lo, planted, pip, 100, 37, 1)
check("a planted round-level reversal beats its offset control",
      bool(s_real.mean() > s_ctrl.mean()), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall round-number checks passed")
