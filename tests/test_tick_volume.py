#!/usr/bin/env python
"""Tick volume, the clock hiding inside it, and a duplicate in the source.

Three defects are pinned here and all three were live at some point in this
search:

  * **A binding that reached the wrong symbol.** `rule_search` hands a
    candidate o/h/l/c and nothing else, so the volume must be looked up. The
    first version bound one module-level array at fetch time -- but
    `rule_search` fetches EVERY symbol into a dict before running any
    candidate, so the binding held only the last, and `trim_to_years` then
    changed its length. The guard refused the mismatch and every candidate
    went silent: **0 of 16 judged.** Failing safe is right; going unnoticed is
    not, which is why the trade count is checked and not just the verdict.
  * **A clock masquerading as information.** Tick volume by server hour runs
    0.27x to 2.13x -- a 7.9x range -- and the hourly profile correlates +0.959
    across pairs. A rule comparing a bar to a rolling baseline spanning hours
    measures time of day. The normaliser must remove that and must stay
    causal.
  * **A duplicate candidate.** Kaufman lists Chaikin's Volume Accumulator and
    Intraday Intensity as two indicators. They are the same formula, and
    carrying both would have inflated the Bonferroni denominator with a
    hypothesis already counted.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import tick_volume_search as T  # noqa: E402

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


HOURLY = np.array([0.3, 0.4, 0.5, 0.8, 0.7, 0.6, 0.5, 0.5, 0.6, 1.2, 1.5, 1.3,
                   1.1, 1.1, 1.2, 1.8, 2.0, 2.1, 1.6, 1.1, 1.0, 1.0, 0.8, 0.4])

print()
print("The normaliser removes the hour-of-day shape, and stays causal")
rng = np.random.default_rng(3)
n = 6000
hour = np.arange(n) % 24
vol = HOURLY[hour] * np.exp(rng.normal(0, 0.4, n)) * 1000.0
raw_prof = np.array([vol[hour == k].mean() for k in range(24)])
raw_prof /= raw_prof.mean()
nv = T.normalise(vol)
nrm_prof = np.array([np.nanmean(nv[hour == k]) for k in range(24)])
nrm_prof /= np.nanmean(nrm_prof)
check("the raw series carries a large hour-of-day swing",
      bool(raw_prof.max() / raw_prof.min() > 5.0), True)
check("and the normalised one does not",
      bool(nrm_prof.max() / nrm_prof.min() < 2.0), True)
check("truncating the future leaves earlier values unchanged",
      bool(np.allclose(nv[:4000], T.normalise(vol[:4000]), equal_nan=True)), True)
check("a bar with too little same-hour history is nan, not a number",
      bool(np.all(~np.isfinite(nv[:24]))), True)


print()
print("Volume is keyed PER SYMBOL, not held in one global")
T.VOL_BY_KEY.clear()
c_a = np.linspace(1.10, 1.20, 500)
c_b = np.linspace(150.0, 160.0, 400)          # different length AND level
v_a = np.full(500, 111.0)
v_b = np.full(400, 222.0)
T.VOL_BY_KEY[T._key(c_a)] = v_a
T.VOL_BY_KEY[T._key(c_b)] = v_b
check("each close series finds its own volume",
      (float(T._vol(c_a, c_a, c_a, c_a)[0]), float(T._vol(c_b, c_b, c_b, c_b)[0])),
      (111.0, 222.0))
check("two symbols do not collide on one key",
      len(T.VOL_BY_KEY), 2)
# The failure the length guard is for.
unknown = np.linspace(0.7, 0.8, 300)
check("an unbound series returns nan rather than another symbol's volume",
      bool(np.all(~np.isfinite(T._vol(unknown, unknown, unknown, unknown)))), True)
# And that silence must be VISIBLE, not quietly substituted.
T.VOL_BY_KEY.clear()
o = h = l = c = np.linspace(1.1, 1.2, 600)
sig = T.make_spike(2.0)(o, h, l, c)
check("with no volume bound at all, a candidate goes silent",
      int((sig != 0).sum()), 0)


print()
print("Chaikin and Intraday Intensity are the SAME formula")
rng2 = np.random.default_rng(1)
hi = 100 + np.abs(rng2.normal(0, 1, 5000))
lo = 100 - np.abs(rng2.normal(0, 1, 5000))
cl = lo + rng2.random(5000) * (hi - lo)
rngv = hi - lo
chaikin = (((cl - lo) / rngv) - 0.5) * 2.0
intensity = ((cl - lo) - (hi - cl)) / rngv
check("they agree to floating-point precision",
      bool(np.allclose(chaikin, intensity)), True)
check("and both reduce to (2C - H - L)/(H - L)",
      bool(np.allclose(chaikin, (2 * cl - hi - lo) / rngv)), True)
names = [nm for nm, _f, _g in T.build_volume_candidates()]
check("so only one of them is a candidate",
      sum(1 for nm in names if "intensity" in nm), 0)
check("and the list is sixteen, each with its inverse", len(names), 16)
# That comparison was written as sorted(x) == sorted(x), which is true of any
# list and could never fail. The pairing is asserted properly: every stem must
# appear exactly twice, once ridden and once faded.
import collections as _c
stems = _c.Counter(nm.replace("_ride", "").replace("_fade", "") for nm in names)
check("every candidate has exactly one fade twin",
      sorted(set(stems.values())), [2])
# Position-agnostic: the direction token sits mid-name on the parameterised
# families (vspike_ride_2.0) and at the end on the rest (ad_ride), so a
# suffix test would fail on half the list for a naming reason rather than a
# pairing one.
_rides = {nm.replace("_ride", "") for nm in names if "_ride" in nm}
_fades = {nm.replace("_fade", "") for nm in names if "_fade" in nm}
check("every ridden candidate has a faded counterpart", _rides == _fades, True)
check("and the two halves are equal in size",
      (len(_rides), len(_fades)), (8, 8))


print()
print("With volume bound, every candidate fires and emits -1/0/+1 only")
T.VOL_BY_KEY.clear()
c2 = 1.1 * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
o2 = np.concatenate(([c2[0]], c2[:-1]))
h2 = np.maximum(o2, c2) + np.abs(rng.normal(0, 0.0003, n))
l2 = np.minimum(o2, c2) - np.abs(rng.normal(0, 0.0003, n))
T.VOL_BY_KEY[T._key(c2)] = vol
silent, bad = [], []
for name, _fam, f in T.build_volume_candidates():
    s = f(o2, h2, l2, c2)
    if len(s) != n or not set(np.unique(s)).issubset({-1.0, 0.0, 1.0}):
        bad.append(name)
    if int((np.diff(s) != 0).sum()) < 20:
        silent.append(name)
check("no candidate is silent once volume is bound", silent, [])
check("and every signal is -1, 0 or +1 of the right length", bad, [])

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall tick-volume checks passed")
