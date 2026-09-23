#!/usr/bin/env python
"""The calendar search, and the weekend hole a day-of-week study falls into.

The first version of `calendar_search.py` reported a Friday effect of
+0.0306% against Tuesday's +0.0123%, cleared its permutation null at
p = 0.0002 and was stable in all four eras. It was mostly an artefact, and
both causes are pinned here because neither is visible in the output:

  * **The Friday-to-Monday return spans THREE calendar days** while every
    other return spans one. Three times the exposure produces a larger mean
    by arithmetic. Per day that bucket is +0.0101% against Tuesday's
    +0.0123% -- lower, not higher.
  * **It was labelled with the day the return STARTS from.** The convention,
    and the only labelling that makes buckets comparable, names a return for
    the bar it ENDS on, so the Friday-to-Monday move is the Monday
    observation. The first version had relabelled the classic weekend effect
    as a Friday effect.

A test that only checked "does it find the planted effect" would have passed
throughout. These check the alignment itself.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from calendar_search import (buckets_for, clustered_t, days_spanned,  # noqa: E402
                             permutation_p, returns_after)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def epoch(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


# Three trading weeks of daily stamps, weekends absent as MT5 delivers them.
DAYS = []
for week in range(3):
    for dow in range(5):                       # Mon..Fri
        DAYS.append(epoch(2026, 9, 7 + week * 7 + dow))
TIMES = np.array(DAYS, dtype="int64")

print()
print("The weekend is three days and the tool has to know it")
spans = days_spanned(TIMES)
check("a mid-week return spans one day", float(spans[0]), 1.0)
# Index 3 is Thu->Fri; index 4 is Fri->Mon.
check("the Friday-to-Monday return spans three", float(spans[4]), 3.0)
check("and only the weekend ones do",
      sorted(set(round(float(x)) for x in spans)), [1, 3])
check("no span is ever zero, which would divide by zero",
      bool(np.all(spans > 0)), True)

print()
print("A return is labelled by the bar it ENDS on")
lab, names = buckets_for(TIMES, "dow")
check("the first stamp is a Monday", names[lab[0]], "Mon")
check("the fifth is a Friday", names[lab[4]], "Fri")
# The return from index 4 (Fri) to index 5 (Mon) must be a MONDAY
# observation. labels[1:] is the alignment the tool uses.
ending = lab[1:]
check("the Friday-to-Monday return is labelled Monday",
      names[ending[4]], "Mon")
check("and the Thursday-to-Friday return is labelled Friday",
      names[ending[3]], "Fri")
# The bug being guarded: labelling by the opening bar would call it Friday.
check("labelling by the opening bar WOULD have called it Friday",
      names[lab[:-1][4]], "Fri")

print()
print("Returns and per-day normalisation")
closes = np.array([100.0, 101.0, 100.0, 100.0, 100.0, 103.0], dtype=float)
r = returns_after(closes)
check("the return is close-to-close over price", round(float(r[0]), 6), 0.01)
check("one return per gap", len(r), len(closes) - 1)
# A 3% move over three days is 1% a day, and must not read as 3%.
sp = np.array([1.0, 1.0, 1.0, 1.0, 3.0])
check("a three-day move normalises to a third",
      round(float((r / sp)[4]), 6), round(0.03 / 3, 6))

print()
print("Clustering counts dates, not symbol-days")
# Seven correlated symbols on ten dates: the clustered n must be 10, not 70.
dates = np.repeat(np.arange(40), 7)
vals = np.repeat(np.linspace(-0.01, 0.01, 40), 7)
n_dates, _t = clustered_t(vals, dates)
check("forty dates seen seven times each count as forty", n_dates, 40)
# A series with no between-date variance has no t, rather than an infinite one.
flat = np.zeros(280)
check("a flat series returns None rather than dividing by zero",
      clustered_t(flat, dates)[1], None)

print()
print("The null can both find an effect and refuse to")
rng = np.random.default_rng(1)
n = 4000
labels = np.array([i % 5 for i in range(n)])
noise = rng.normal(0, 0.005, n)
check("pure noise is not called an effect",
      permutation_p(noise, labels, 400, seed=2)["p_value"] > 0.05, True)
planted = noise.copy()
planted[labels == 4] += 0.004          # one bucket genuinely different
check("a planted bucket effect IS found",
      permutation_p(planted, labels, 400, seed=2)["p_value"] < 0.01, True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall calendar-search checks passed")
