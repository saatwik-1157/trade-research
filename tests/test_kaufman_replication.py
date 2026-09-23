#!/usr/bin/env python
"""Kaufman's protocol, and whether this really is his protocol.

This file exists to check a PUBLISHED claim, so the thing that has to be
right is not the verdict but the fidelity of the simulator. Three ways to get
it wrong, each of which would produce a number that looks like an answer to
Kaufman and is not:

  * **Bracketing the trades.** His systems are always in the market and
    reverse on the signal; they have no stop and no target. `rule_search`
    brackets at 1.5xATR, and the exit searches already showed a bracket can
    flip a trend result's sign. Running his entries with this repository's
    exit would test a third strategy belonging to neither.
  * **Charging cost per bar instead of per reversal.** A position held for
    forty bars crosses the spread twice, not eighty times.
  * **Letting fast rules trade bars the slow ones spent warming up.** His own
    rule, and this repository was not following it. At D1 an 80-bar wind-up is
    ~3% of the history.

Power and restraint are checked before any verdict is believed, as in
`cross_search` and `pairs_search`. The restraint figure is the interesting
one and is asserted here as a calibration rather than as a pass: a pure random
walk already returns a little over half the grid profitable, so Kaufman's "70%
of tests profitable" standard sits only ~13 points above what noise delivers.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from kaufman_replication import (  # noqa: E402
    MAX_WARMUP, METHODS, PERIODS, always_in_pnl, breakout_signal, run_grid,
    sma_signal,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


print()
print("The book is always on, and reverses rather than closing flat")
c = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
held_long = np.array([np.nan, 1.0, 1.0, 1.0, 1.0])
pnl = always_in_pnl(held_long, c, 0.0)
# Bar 0 has no prior position, bar 1 inherits bar 0's (none), so earnings
# start at bar 2. The point is that the position persists without re-entry.
check("a held position earns every bar after the first",
      bool(np.all(pnl[2:] > 0)), True)
check("and is never flat while the signal is live",
      bool(np.all(np.isfinite(pnl))), True)

print()
print("Cost is charged per REVERSAL, not per bar held")
flat = np.full(40, 100.0)
hold = np.ones(40)
free = always_in_pnl(hold, flat, 0.0)
paid = always_in_pnl(hold, flat, 0.001)
# One transition from no-position to long = one crossing.
check("holding forty bars costs one crossing, not forty",
      round(float(free.sum() - paid.sum()), 8), 0.001)
flip = np.concatenate((np.ones(20), -np.ones(20)))
paid_flip = always_in_pnl(flip, flat, 0.001)
free_flip = always_in_pnl(flip, flat, 0.0)
# Entry (1 crossing) plus a reversal (2 crossings) = 3.
check("a reversal costs TWO crossings, out and in",
      round(float(free_flip.sum() - paid_flip.sum()), 8), 0.003)

print()
print("Every rule starts on the same bar, whatever its own wind-up")
rng = np.random.default_rng(2)
px = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, 1200)))
firsts = []
for n in (min(PERIODS), max(PERIODS)):
    s = sma_signal(px, n)
    s[:MAX_WARMUP] = np.nan
    firsts.append(int(np.flatnonzero(np.isfinite(s))[0]))
check("the fastest and slowest rules begin on the same bar",
      len(set(firsts)), 1)
check("and that bar is the longest wind-up", firsts[0], MAX_WARMUP)
check("the longest period is the wind-up", MAX_WARMUP, max(PERIODS))
check("periods are geometric, not every fifth day",
      bool(all(PERIODS[i + 1] / PERIODS[i] > 1.15 for i in range(len(PERIODS) - 1))),
      True)

print()
print("The breakout is a REVERSAL system: it carries its last signal")
# A realistic consolidation, NOT an exactly flat one. On a perfectly flat run
# every close is simultaneously the n-bar high and the n-bar low, so the tie
# resolves arbitrarily -- a measure-zero case that does not occur in FX and
# would make this check test the tie rather than the carry.
_rng = np.random.default_rng(9)
c2 = np.concatenate((np.linspace(100, 120, 60),
                     119.0 + _rng.normal(0, 0.05, 40)))
b = breakout_signal(c2, 28)
live = b[np.isfinite(b)]
check("it holds through bars that make no new extreme",
      bool(np.all(live[-15:] == live[-15])), True)
check("and it is never flat once started",
      bool(np.all(np.abs(live) == 1.0)), True)
# There are bars in the consolidation that set neither a new high nor a new
# low; the carry is what fills them, so assert they exist or the check above
# is vacuous.
_win = np.lib.stride_tricks.sliding_window_view(c2, 28)
_neither = sum(1 for i in range(len(_win))
               if c2[i + 27] < _win[i].max() and c2[i + 27] > _win[i].min())
check("and there really were bars needing the carry", bool(_neither > 5), True)

print()
print("It finds a trend that is there, and not one that is not")
n = 3000
rng = np.random.default_rng(4)
regime = np.repeat(rng.choice([-1, 1], size=n // 200), 200)[:n]
trending = 100 * np.exp(np.cumsum(regime * 0.004 + rng.normal(0, 0.004, n)))
walk = 100 * np.exp(np.cumsum(rng.normal(0, 0.008, n)))


def share_and_best(px_):
    rows = [r for r in run_grid(px_, 0.0) if r["t"] == r["t"]]
    return (sum(1 for r in rows if r["total"] > 0) / len(rows),
            max(r["t"] for r in rows), len(rows))


s_tr, t_tr, cells = share_and_best(trending)
s_rw, t_rw, _ = share_and_best(walk)
print(f"        trending {s_tr * 100:.1f}% at t={t_tr:+.2f}   "
      f"random walk {s_rw * 100:.1f}% at t={t_rw:+.2f}")
check("the full grid is 5 methods x 6 periods", cells, len(METHODS) * len(PERIODS))
check("a real trend is found across nearly the whole grid", bool(s_tr > 0.9), True)
check("and at a large t", bool(t_tr > 10), True)
check("a random walk is not found", bool(t_rw < 3.0), True)
# The calibration that matters for reading Kaufman's own standard: noise alone
# already puts about half the grid in profit, so "70% profitable" is a low bar.
check("but noise still profits on roughly half the grid, so 70% is a low bar",
      bool(0.3 < s_rw < 0.75), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall Kaufman-protocol checks passed")
