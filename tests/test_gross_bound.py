#!/usr/bin/env python
"""The tie rule, and why the gross bound is a bound rather than a number.

A symmetric bracket entered at random on a martingale must give EXACTLY zero
gross. It does not, and the whole departure is one rule: when a bar's range
covers both the stop and the target, the intrabar order is unknown. Resolving
that as a loss gives -0.0119R and as a win +0.0070R, on under 1% of trades --
a swing larger than the effect the account's case is arguing about.

So what is tested here is not a verdict but the mechanics: that a tie is
detected at all, that the two resolutions really differ, and that the walk is
otherwise the same as the tested simulator's.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from gross_bound import bracket_r, summarise  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


N = 80
atr = np.full(N, 1.0)
sig = np.ones(N)

print()
print("A bar covering BOTH levels is a tie, and the resolution is a choice")
o = np.full(N, 100.0)
c = np.full(N, 100.0)
h = np.full(N, 100.2)
low = np.full(N, 99.8)
# bar 61 straddles: entry 100, sl 98.5, tp 101.5 -> reach both
h[61], low[61] = 102.0, 98.0
r_loss, t_loss = bracket_r(o, h, low, c, sig, atr, tie_to_loss=True)
r_win, t_win = bracket_r(o, h, low, c, sig, atr, tie_to_loss=False)
check("the straddling bar is counted as a tie", (t_loss, t_win), (1, 1))
check("resolving it as a loss books -1R", round(r_loss[0], 6), -1.0)
check("resolving it as a win books +1R", round(r_win[0], 6), 1.0)
check("and the two resolutions differ by exactly 2R",
      round(r_win[0] - r_loss[0], 6), 2.0)

print()
print("A bar reaching only one level is not a tie")
h2 = np.full(N, 100.2)
l2 = np.full(N, 99.8)
h2[61] = 102.0                      # target only
rr, tt = bracket_r(o, h2, l2, c, sig, atr, tie_to_loss=True)
check("a target-only bar books +1R with no tie", (round(rr[0], 6), tt), (1.0, 0))
h3 = np.full(N, 100.2)
l3 = np.full(N, 99.8)
l3[61] = 98.0                       # stop only
rr2, tt2 = bracket_r(o, h3, l3, c, sig, atr, tie_to_loss=True)
check("a stop-only bar books -1R with no tie", (round(rr2[0], 6), tt2), (-1.0, 0))
check("and the tie choice cannot change a one-sided bar",
      bracket_r(o, h3, l3, c, sig, atr, tie_to_loss=False)[0][0], rr2[0])

print()
print("The walk matches the tested simulator's conventions")
check("nothing is entered before bar 60", len(bracket_r(
    o[:60], h[:60], low[:60], c[:60], sig[:60], atr[:60])[0]), 0)
flat_h = np.full(N, 100.05)
flat_l = np.full(N, 99.95)
rr3, _ = bracket_r(o, flat_h, flat_l, c, sig, atr, max_hold=5)
check("a position that never resolves times out at the close rather than hanging",
      bool(len(rr3) > 0 and abs(rr3[0]) < 0.01), True)
zero = np.zeros(N)
check("a zero ATR is skipped rather than dividing by zero",
      len(bracket_r(o, h, low, c, sig, zero)[0]), 0)
check("a zero signal opens nothing",
      len(bracket_r(o, h, low, c, np.zeros(N), atr)[0]), 0)

print()
print("The summary reports a bound, not a point")
s = summarise([1.0, -1.0] * 500)
check("a balanced sample averages zero", round(s["mean_r"], 9), 0.0)
check("and its win rate is a half", round(s["win_rate"], 6), 0.5)
check("n is the sample size", s["n"], 1000)
check("the interval brackets the mean",
      bool(s["ci"][0] < s["mean_r"] < s["ci"][1]), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall gross-bound checks passed")
