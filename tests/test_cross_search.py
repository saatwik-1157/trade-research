#!/usr/bin/env python
"""The cross-sectional engine, and the four ways it could lie.

A null result is only worth anything if the thing producing it could have
found something. Three of these tests exist to show it could, and one to show
it is not finding things that are not there:

  * **Lookahead.** The signal is a cumulative past return sliced out of a
    cumsum, which is exactly the shape that goes one column wrong and reads
    tomorrow. A lookahead bug does not break anything visibly - it manufactures
    an edge, which is the direction this project cares about. Pure noise must
    not produce a large t.
  * **Power.** If the engine cannot detect a cross-sectional effect that IS
    there, its null says nothing at all. A planted persistent currency must be
    found, and found with the right sign.
  * **Direction.** Four majors quote the foreign currency as base and three
    quote the dollar as base, so three returns must be negated before ranking.
    Skipping that inverts three of seven and produces a result that is half
    signal and half sign error.
  * **Dollar neutrality.** The entire reason this construction exists is that
    a pure dollar move cancels. If it does not cancel exactly, the portfolio
    still carries the shared leg and the whole argument for the file collapses.

Cost is checked too, because a turnover charge that silently does nothing
makes every fast variant look as good as the slow ones, and frequency is the
only lever this repository has ever shown to have a sign.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from cross_search import (FOREIGN_IS_BASE, align, foreign_returns,
                          momentum_signal, permuted_signal, portfolio, tstat)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def t_of(series):
    s = series[np.isfinite(series)]
    return float(s.mean() / (s.std(ddof=1) / math.sqrt(len(s))))


# ------------------------------------------------------- direction convention

check("a base-quoted major is not negated",
      list(foreign_returns(np.array([1.0, math.e]), True).round(6)), [1.0])
check("a dollar-quoted major IS negated",
      list(foreign_returns(np.array([1.0, math.e]), False).round(6)), [-1.0])
check("all four XXXUSD majors are marked foreign-as-base",
      [FOREIGN_IS_BASE[s] for s in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD")],
      [True, True, True, True])
check("all three USDXXX majors are marked foreign-as-quote",
      [FOREIGN_IS_BASE[s] for s in ("USDJPY", "USDCAD", "USDCHF")],
      [False, False, False])

# ------------------------------------------------------------------ alignment

# Three symbols on overlapping but unequal timestamps. A cross-section that
# indexed off the first symbol would rank different bars against each other.
series = {
    "A": (np.array([10, 20, 30, 40]), np.array([1.0, 2.0, 3.0, 4.0])),
    "B": (np.array([20, 30, 40, 50]), np.array([5.0, 6.0, 7.0, 8.0])),
    "C": (np.array([20, 30, 40]), np.array([9.0, 10.0, 11.0])),
}
syms, times, closes = align(series)
check("alignment keeps only the common timestamps", list(times), [20, 30, 40])
check("alignment returns symbols in a stable order", syms, ["A", "B", "C"])
check("and each row is that symbol's bars at those stamps",
      [list(r) for r in closes], [[2.0, 3.0, 4.0], [5.0, 6.0, 7.0], [9.0, 10.0, 11.0]])

# --------------------------------------------------------- dollar neutrality

# The claim the whole file rests on. A move shared by every currency is a pure
# dollar move and must cancel EXACTLY, not approximately.
rng = np.random.default_rng(7)
n_sym, n_bar = 7, 1500
common = np.tile(rng.normal(0, 0.01, n_bar), (n_sym, 1))
sig = momentum_signal(rng.normal(0, 0.005, (n_sym, n_bar)), 20)
p = portfolio(common, sig, np.zeros(n_sym), n_legs=2, hold=5, sign=1)
check("a pure common dollar move cancels exactly",
      float(np.abs(p).max()), 0.0)

# ---------------------------------------------------------------- lookahead

# Pure noise. Nothing is predictable, so a large t here is the engine reading
# a bar it should not be able to see.
noise = rng.normal(0, 0.005, (n_sym, n_bar * 3))
p = portfolio(noise, momentum_signal(noise, 20), np.zeros(n_sym), 2, 5, 1)
check("pure noise does not produce a significant result", abs(t_of(p)) < 2.0, True)

# The sharper form: shift the signal one column the WRONG way so it reads the
# current bar's own return, and confirm the test above would have caught it.
cheat = np.full_like(noise, np.nan)
cheat[:, 1:] = noise[:, 1:]                     # today's return, known today
p_cheat = portfolio(noise, cheat, np.zeros(n_sym), 2, 5, 1)
check("and a deliberate one-bar lookahead IS caught", abs(t_of(p_cheat)) > 5.0, True)

# Direct, and the reason momentum_signal is a function at all. The statistical
# checks above are necessary but blunt: an off-by-one that contaminates only
# 1/k of the signal shows up as an in-sample t of 2.47 - enough to CLEAR
# Bonferroni and the permutation null, caught only by the out-of-sample gate.
# This asserts the algebra instead of hoping a t-statistic notices.
probe = np.zeros((2, 40))
probe[0, 25] = 1.0          # a single return, at bar 25, in symbol 0
sig_probe = momentum_signal(probe, 10)
check("the signal is blind to the bar it is acted on",
      float(sig_probe[0, 25]), 0.0)
check("and picks that return up on the very next bar",
      float(sig_probe[0, 26]), 1.0)
check("and drops it once it falls out of the lookback",
      float(sig_probe[0, 36]), 0.0)
check("while the bar before it saw nothing either",
      float(sig_probe[0, 24]), 0.0)
# Everything before the window can be filled is NaN, never a silent zero: a
# zero would rank as a middling currency and quietly trade the warm-up.
check("the warm-up is NaN rather than zero",
      bool(np.all(np.isnan(sig_probe[:, :11]))), True)

# ------------------------------------------------------------------- power

# A planted persistent currency. If momentum cannot find this, the null the
# tool reports on real data means nothing.
drift = rng.normal(0, 0.005, (n_sym, n_bar * 3))
drift[0] += 0.002
sig_d = momentum_signal(drift, 20)
t_mom = t_of(portfolio(drift, sig_d, np.zeros(n_sym), 2, 5, 1))
t_rev = t_of(portfolio(drift, sig_d, np.zeros(n_sym), 2, 5, -1))
check("a planted strong currency is found by momentum", t_mom > 5.0, True)
check("and reversal loses by the same amount on it",
      round(t_mom + t_rev, 6), 0.0)

# --------------------------------------------------------------------- cost

# Turnover is the charge, so a leg held through a rebalance is free and a
# faster rebalance must cost strictly more.
#
# The comparison has to be made at a FIXED hold, between the same portfolio
# with and without a spread. Comparing net means across different holds - as
# an earlier version of this test did - compares different portfolios, and on
# noise their gross returns differ by far more than the cost does, so it
# failed while the charge was working correctly.
spreads = np.full(n_sym, 0.0001)
sig_n = momentum_signal(noise, 20)


def cost_at(h):
    free = portfolio(noise, sig_n, np.zeros(n_sym), 2, h, 1).mean()
    paid = portfolio(noise, sig_n, spreads, 2, h, 1).mean()
    return free - paid


costs = [cost_at(h) for h in (1, 5, 20)]
check("every hold is charged something", all(c > 0 for c in costs), True)
check("and a faster rebalance is charged strictly more",
      costs[0] > costs[1] > costs[2], True)
# The ratio is a real check rather than just an ordering, but it is NOT
# linear in the rebalance count and an earlier version of this test wrongly
# demanded that it was. Five times the rebalances costs 2.3x, not 5x, because
# a 20-bar momentum signal rarely changes its top two between adjacent bars -
# most rebalances re-select the same legs and |w_new - w_old| is zero. That
# the charge is sublinear in frequency IS the turnover model working; a cost
# scaling exactly with rebalance count would mean it was charging per
# rebalance rather than per trade.
check("five times the rebalances costs meaningfully more, but not five times",
      2.0 < costs[0] / costs[1] < 5.0, True)

# ------------------------------------------------------------------- the null

# The cross-sectional shuffle must destroy WHICH currency is ranked where
# while leaving the set of values at each rebalance intact - otherwise it is
# not holding leg count and exposure fixed.
base = np.arange(28, dtype=float).reshape(7, 4)
sh = permuted_signal(base, hold=1, seed=3)
check("the shuffle preserves each rebalance's set of values",
      [sorted(sh[:, t]) == sorted(base[:, t]) for t in range(4)],
      [True, True, True, True])
check("and it does change the ordering somewhere",
      bool(np.any(sh != base)), True)
# Columns that are not rebalance dates must pass through untouched, or the
# null would be turning over more often than the thing it is a null for.
sh2 = permuted_signal(base, hold=4, seed=3)
check("columns between rebalances are left alone",
      [list(sh2[:, t]) == list(base[:, t]) for t in (1, 2, 3)],
      [True, True, True])

# ------------------------------------------------------------------- tstat

check("a series too short to judge returns None", tstat(np.zeros(5))[0], None)
check("a flat series returns None rather than a divide by zero",
      tstat(np.zeros(500))[0], None)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall cross-section checks passed")
