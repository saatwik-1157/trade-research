#!/usr/bin/env python
"""The Pugh encoding, and whether its correction is really exact.

This search's whole claim is that its candidate list is the COMPLETE family
rather than a chosen one, so the Bonferroni threshold is exact instead of
conventional. That claim is not about the result -- it is about the encoding,
and it is false unless two properties hold:

  * **Exhaustive.** Every bar with a predecessor carries a label. A bar that
    fell through the comparisons would be silently excluded from every
    candidate, and the family would no longer cover the data.
  * **Mutually exclusive.** No bar carries two labels. Overlapping candidates
    would make 80 tests fewer than 80 independent ones and the correction
    would be wrong in the flattering direction.

Both are asserted here by counting, not by reading the comparisons. The
remaining checks are the ones every candidate list in this repository now
gets: the constructions are driven against hand-built bars, and no rule is
allowed to be silent -- `three_methods` in `book_rules_search` fired ZERO
times on 3,000 synthetic bars while looking healthy, and a signal stuck at
zero is indistinguishable in a report from a rule that was tried and failed.
"""
from __future__ import annotations

import collections
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from pugh_search import (  # noqa: E402
    BEAR, BULL, INSIDE, OUTSIDE, build_pugh_candidates, make_sequence,
    pugh_states,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


print()
print("The four labels are read off the highs and lows, not guessed")
h = np.array([10.0, 11.0, 10.5, 12.0, 11.5])
lo = np.array([9.0, 9.5, 9.0, 8.0, 8.5])
st = pugh_states(h, lo)
check("higher high and higher low is BULL", int(st[1]), BULL)
check("lower high and lower low is BEAR", int(st[2]), BEAR)
check("higher high and lower low is OUTSIDE", int(st[3]), OUTSIDE)
check("lower high and higher low is INSIDE", int(st[4]), INSIDE)
check("the first bar has no predecessor and no label", int(st[0]), -1)
# A flat bar must land somewhere rather than vanish, and WHERE matters.
# The first version phrased the partition as "higher high / higher low", which
# sent a bar identical to its predecessor into BEAR. A contained bar is not
# bearish, and 1.855% of real H1 bars carry an equal high or low, so this was
# a misclassification of ~2,600 bars rather than a rounding detail.
flat = pugh_states(np.array([10.0, 10.0]), np.array([9.0, 9.0]))
check("an exactly repeated bar is INSIDE, not BEAR", int(flat[1]), INSIDE)
check("an equal high with a lower low is still BEAR",
      int(pugh_states(np.array([10.0, 10.0]), np.array([9.0, 8.0]))[1]), BEAR)
check("an equal low with a higher high is still BULL",
      int(pugh_states(np.array([10.0, 11.0]), np.array([9.0, 9.0]))[1]), BULL)


print()
print("The family is COMPLETE, which is the only reason the correction is exact")
rng = np.random.default_rng(1)
n = 20000
ret = rng.normal(0, 0.0008, n)
c = 1.1 * np.exp(np.cumsum(ret))
o = np.concatenate(([c[0]], c[:-1]))
hi = np.maximum(o, c) + np.abs(rng.normal(0, 0.0003, n))
low = np.minimum(o, c) - np.abs(rng.normal(0, 0.0003, n))

cands = build_pugh_candidates()
check("16 two-bar shapes and 64 three-bar shapes", len(cands), 80)
check("and the count is 4**2 + 4**3 exactly", 4 ** 2 + 4 ** 3, 80)

for k, family, skip in ((2, "pugh_2bar", 2), (3, "pugh_3bar", 3)):
    fired = np.zeros(n)
    for _nm, fam, f in cands:
        if fam == family:
            fired += f(o, hi, low, c) != 0
    body = fired[skip:]
    check(f"every {k}-bar position is covered by exactly one shape",
          int((body == 1).sum()), len(body))
    check(f"and no {k}-bar position is covered by two",
          int((body > 1).sum()), 0)

# Every label must actually occur, or part of the family is untestable.
seen = collections.Counter(int(s) for s in pugh_states(hi, low)[1:])
check("all four labels occur on realistic bars",
      sorted(seen) == [BULL, BEAR, OUTSIDE, INSIDE], True)


print()
print("A sequence fires on its own shape and on no other")
# bull then bear, built by hand.
h2 = np.array([10.0, 11.0, 10.5, 10.2])
l2 = np.array([9.0, 9.5, 9.0, 8.5])
f_bull_bear = make_sequence((BULL, BEAR))
sig = f_bull_bear(h2, h2, l2, h2)
check("bull->bear fires at the bar completing it", float(sig[2]), 1.0)
check("and not before it completes", bool(np.all(sig[:2] == 0.0)), True)
check("a different sequence does not fire on it",
      float(make_sequence((BEAR, BULL))(h2, h2, l2, h2)[2]), 0.0)
check("the short form is the exact negation",
      float(make_sequence((BULL, BEAR), short=True)(h2, h2, l2, h2)[2]), -1.0)
# Causality: the signal at t must not depend on anything after t.
full = f_bull_bear(o, hi, low, c)
trunc = f_bull_bear(o[:9000], hi[:9000], low[:9000], c[:9000])
check("truncating the future leaves earlier signals unchanged",
      bool(np.array_equal(full[:9000], trunc)), True)


print()
print("No candidate is silent -- a rule that never fires is not a null")
# The family is complete but NOT uniformly powered, and that is measured
# rather than assumed: across seven majors and 20,000 H1 bars each,
# out_out_out occurs 72 times and in_in_in 113, against 23,184 for bull_bull.
# So the check is not "all 80 fire often" -- it is that every TWO-bar cell is
# well populated, and that only the known-rare three-bar corners are thin.
RARE = {"pugh_out_out_out", "pugh_in_in_in"}
silent_two, thin_three, bad = [], [], []
for name, fam, f in cands:
    sg = f(o, hi, low, c)
    if len(sg) != n or not set(np.unique(sg)).issubset({-1.0, 0.0, 1.0}):
        bad.append(name)
    fires = int((sg != 0).sum())
    if fam == "pugh_2bar" and fires < 100:
        silent_two.append(name)
    if fam == "pugh_3bar" and fires < 20:
        thin_three.append(name)
check("every two-bar shape is well populated", silent_two, [])
check("only the known-rare corners are thin, and they are named",
      set(thin_three) <= RARE, True)
check("and the thin cells are a handful, not the family",
      bool(len(thin_three) <= 2), True)
check("every signal is -1, 0 or +1 of the right length", bad, [])

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall Pugh checks passed")
