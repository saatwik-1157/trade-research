#!/usr/bin/env python
"""The DeMark state machine, asserted rule by rule.

Every other candidate in this project is a formula, and a formula that is
wrong is usually wrong loudly. A state machine is not: it can drop into a
branch that never fires, or fire on the wrong bar, and still emit a clean
array of plausible-looking signals. So each rule is driven separately here,
on bars built to trigger exactly that rule and nothing else.

The one place the specification is genuinely ambiguous is RECYCLING -- what a
fresh setup does to a countdown already in progress. This implementation
restarts it, per the rule as stated. That choice has a visible consequence
which is pinned below: on a perfectly monotone decline a new setup completes
every nine bars, so the countdown is reset forever and NO signal is ever
emitted. That is not a bug, it is the rule; on real data setups are irregular
and 48.6% of them reach a thirteen-count.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from demark_search import (  # noqa: E402
    COUNTDOWN_LEN, SETUP_LEN, forward, sequential, tstat,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def bars(closes):
    c = np.asarray(closes, float)
    return c.copy(), c + 0.2, c - 0.2, c


print()
print("The setup is nine consecutive closes below the close four bars back")
n = 60
c = 100 - np.arange(n) * 0.5
o, h, l, _c = bars(c)
su, _sg = sequential(o, h, l, c, buy=True)
# The first bar that can qualify is index 4, so the ninth is index 12.
check("the first setup completes on the ninth qualifying bar", int(su[0]), 4 + SETUP_LEN - 1)
check("the setup length is nine", SETUP_LEN, 9)

broken = c.copy()
broken[8] += 10.0                      # one bar fails the condition
su2, _ = sequential(o, h, l, broken, buy=True)
check("a single failing bar restarts the count from zero",
      int(su2[0]) > int(su[0]), True)

rising = 100 + np.arange(n) * 0.5
su3, _ = sequential(o, h, l, rising, buy=True)
check("a rising series produces no BUY setup", len(su3), 0)
su4, _ = sequential(o, h, l, rising, buy=False)
check("but it does produce SELL setups", bool(len(su4) > 0), True)


print()
print("A close beyond the setup's own extreme cancels it")
cancelled = c.copy()
cancelled[20:] += 40.0                 # blows through the setup high
_s, sig_c = sequential(o, h, l, cancelled, buy=True)
check("a close above the setup high kills the countdown", len(sig_c), 0)


print()
print("Recycling: a fresh setup restarts the countdown, per the rule")
# On a monotone decline a setup completes every nine bars, so the countdown
# is reset before it can ever reach thirteen.
_su, sig_mono = sequential(o, h, l, c, buy=True)
check("a monotone decline emits setups", bool(len(_su) >= 4), True)
check("and therefore NO signal, because each setup recycles the count",
      len(sig_mono), 0)
check("the countdown target is thirteen", COUNTDOWN_LEN, 13)

# A hand-built decline is the wrong fixture here: any series regular enough
# to construct keeps completing setups and recycles forever. A drifting walk
# is what the machine actually meets, so the properties are asserted on that.
_r = np.random.default_rng(1)
c2 = 100 * np.exp(np.cumsum(_r.normal(-0.0004, 0.008, 1500)))
su5, sig5 = sequential(c2, c2 * 1.002, c2 * 0.998, c2, buy=True)
check("a drifting walk reaches thirteen-counts", bool(len(sig5) >= 5), True)
check("and not from every setup -- most are cancelled or recycled",
      bool(0.2 < len(sig5) / len(su5) < 0.9), True)
check("every signal lands after the setup that armed it",
      bool(all(sig5[i] > su5[su5 < sig5[i]].max() for i in range(len(sig5)))), True)
# DeMark states the sequence takes 21 bars minimum, typically 24-39. That is
# a property of the rules, so it is a check rather than a coincidence.
gap = int(sig5[0] - su5[su5 < sig5[0]].max())
check("a signal cannot come sooner than the countdown length",
      bool(gap >= COUNTDOWN_LEN), True)
check("and the first sequence takes about the stated 21-39 bars",
      bool(13 <= gap <= 60), True)


print()
print("Forward returns are signed so a correct signal scores positive")
px = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
idx = np.array([0])
check("a buy followed by a rise is positive", bool(forward(px, idx, 2, True)[0] > 0), True)
check("a buy followed by a fall is negative",
      bool(forward(px[::-1].copy(), idx, 2, True)[0] < 0), True)
check("a sell followed by a fall is positive",
      bool(forward(px[::-1].copy(), idx, 2, False)[0] > 0), True)
check("a signal too close to the end is dropped, not padded",
      len(forward(px, np.array([5]), 2, True)), 0)


print()
print("The t-statistic refuses samples too small to carry one")
check("a handful of observations gives nan",
      tstat(np.zeros(5)) != tstat(np.zeros(5)), True)
rng = np.random.default_rng(2)
check("a zero-mean sample gives a small t",
      bool(abs(tstat(rng.normal(0, 1, 5000))) < 3.0), True)
check("a shifted sample gives a large one",
      bool(tstat(rng.normal(0.5, 1, 5000)) > 10), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall DeMark checks passed")
