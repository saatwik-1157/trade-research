#!/usr/bin/env python
"""Intrabar path, and whether any of it is actually new information.

The premise of this search is that M1 bars expose something OHLC cannot
express. That premise is checkable rather than assumable, and checking it
demoted the feature the search would otherwise have led with: path efficiency
is **82% explained** by the bar's own body and range, which makes it close to
a restatement of ground `shape_search` already refuted.

So the load-bearing machinery here is not the signal test, it is:

  * **The residualiser**, which strips out everything OHLC explains so that
    what remains is strictly the part of the path OHLC cannot see. Testing the
    raw feature would rediscover candle shape and report it as new.
  * **The bar builder**, which must DROP a partial hour rather than measure
    it. A path statistic over twelve minutes is not the same quantity as one
    over sixty, and pooling them is the units error this repository documents
    in every other form.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from intrabar_search import (  # noqa: E402
    MIN_SUB_BARS, RECOVERABLE, build_bars, residualise, tstat,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def minute_bars(hour, closes):
    """M1 bars inside one clock hour, as the builder expects them."""
    n = len(closes)
    t = np.array([hour * 3600 + i * 60 for i in range(n)], dtype=np.int64)
    c = np.asarray(closes, float)
    o = np.concatenate(([c[0]], c[:-1]))
    return t, o, np.maximum(o, c), np.minimum(o, c), c


print()
print("A partial hour is dropped, not measured")
short = minute_bars(100, np.linspace(1.10, 1.11, MIN_SUB_BARS - 1))
check("an hour with too few minutes yields no bar", len(build_bars(*short)), 0)
full = minute_bars(100, np.linspace(1.10, 1.11, 60))
check("a complete hour yields exactly one", len(build_bars(*full)), 1)
two = tuple(np.concatenate([a, b]) for a, b in
            zip(minute_bars(100, np.linspace(1.10, 1.11, 60)),
                minute_bars(101, np.linspace(1.11, 1.12, 60))))
check("two complete hours yield two bars", len(build_bars(*two)), 2)


print()
print("The path measures travel, which OHLC cannot")
# Two bars with the SAME open, high, low and close and very different paths.
straight = np.linspace(1.1000, 1.1010, 60)
zigzag = straight.copy()
zigzag[1:-1] += np.tile([0.0005, -0.0005], 29)
a = build_bars(*minute_bars(200, straight))[0]
b = build_bars(*minute_bars(201, zigzag))[0]
check("both bars share an open and a close",
      (round(a["open"], 6), round(a["close"], 6)),
      (round(b["open"], 6), round(b["close"], 6)))
check("but the zigzag travelled much further per unit of range",
      bool(b["ppr"] > a["ppr"] * 2), True)
check("and crossed its own open far more often",
      bool(b["cross"] > a["cross"] + 10), True)
check("a monotone rise never crosses its open", float(a["cross"]), 0.0)


print()
print("Residualising removes exactly what OHLC explains")
rng = np.random.default_rng(5)
n = 4000
ohlc = rng.normal(0, 1, (n, 3))
# A feature that is PURELY a linear function of OHLC must residualise to zero.
pure = ohlc @ np.array([2.0, -1.0, 0.5]) + 3.0
res, r2 = residualise(pure, ohlc)
check("a feature made only of OHLC has R2 of one", round(r2, 6), 1.0)
check("and residualises to nothing",
      bool(np.abs(res).max() < 1e-8), True)
# A feature independent of OHLC must survive intact.
indep = rng.normal(0, 1, n)
res2, r2b = residualise(indep, ohlc)
check("an independent feature has R2 near zero", bool(r2b < 0.01), True)
check("and keeps essentially all its variance",
      bool(res2.std() > indep.std() * 0.98), True)
# The mixed case: half signal, half OHLC.
mixed = pure / pure.std() + indep
_r, r2c = residualise(mixed, ohlc)
check("a half-and-half feature lands between the two",
      bool(0.2 < r2c < 0.8), True)
check("the drop threshold is a stated constant",
      bool(0.0 < RECOVERABLE < 1.0), True)


print()
print("It would find a path effect if one were planted")
res_f, _ = residualise(indep, ohlc)
fwd = np.sign(res_f) * np.abs(rng.normal(0, 0.001, n)) + rng.normal(0, 0.0005, n)
check("a planted directional link is found",
      bool(tstat(np.sign(res_f) * fwd) > 5.0), True)
check("and pure noise is not",
      bool(abs(tstat(np.sign(res_f) * rng.normal(0, 0.001, n))) < 3.0), True)
check("too few observations give nan rather than a number",
      tstat(np.zeros(5)) != tstat(np.zeros(5)), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall intrabar checks passed")
