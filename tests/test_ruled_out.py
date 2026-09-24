#!/usr/bin/env python
"""The power arithmetic behind "what has been ruled out".

This file converts twenty-five nulls into bounds, so a wrong power
calculation would not look wrong -- it would quietly make the record claim to
exclude edges it never could, or fail to exclude ones it did. The reports it
reads are generated and not committed, so what is tested here is the
arithmetic alone, against closed forms where they exist and against identities
where they do not.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from ruled_out import (  # noqa: E402
    POWER, joint_power, min_detectable, phi, trades_needed,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


Z80 = 0.8416212335729143            # standard normal quantile at 0.80

print()
print("The normal CDF is the normal CDF")
check("phi(0) is a half", round(phi(0.0), 12), 0.5)
check("phi(1.96) is 0.975", round(phi(1.96), 4), 0.975)
check("and it is symmetric", round(phi(1.3) + phi(-1.3), 12), 1.0)

print()
print("One gate reduces to the textbook closed form")
# With an enormous out-of-sample set, that gate's power is 1 and the joint
# power is just the in-sample gate: mu = (z + z_0.8) * sd / sqrt(n).
for n, z, sd in ((10_000, 1.96, 1.0), (2_869, 3.234, 0.9999), (400, 2.5, 1.2)):
    got = min_detectable(sd, n, z, 10 ** 12)
    want = (z + Z80) * sd / math.sqrt(n)
    check(f"n={n}, z={z}: solver matches closed form",
          round(got, 6), round(want, 6))

print()
print("Two gates are a PRODUCT, and the solver hits the stated power exactly")
m = min_detectable(1.0, 10_000, 1.96, 10_000, 1.96)
check("joint power at the returned edge is 0.80",
      round(joint_power(m, 1.0, 10_000, 1.96, 10_000, 1.96), 6), POWER)
check("two gates need a LARGER edge than either alone",
      bool(m > min_detectable(1.0, 10_000, 1.96, 10 ** 12)), True)
# Each of two identical gates must sit at sqrt(0.8) for the product to be 0.8.
each = phi(m * 100 - 1.96)
check("each identical gate sits at sqrt(0.8)",
      round(each, 5), round(math.sqrt(POWER), 5))

print()
print("More data means a smaller detectable edge, and none means no bound")
check("quadrupling both samples halves the edge",
      round(min_detectable(1.0, 40_000, 3.0, 20_000)
            / min_detectable(1.0, 10_000, 3.0, 5_000), 4), 0.5)
check("a stricter correction needs a larger edge",
      bool(min_detectable(1.0, 5_000, 3.5, 2_000)
           > min_detectable(1.0, 5_000, 2.5, 2_000)), True)
check("an empty sample rules out nothing",
      min_detectable(1.0, 0, 3.0, 100), float("inf"))
check("and has zero power at any edge", joint_power(5.0, 1.0, 0, 3.0, 10), 0.0)

print()
print("trades_needed is the inverse of min_detectable")
# Round trip: the sample it returns must detect exactly the target edge.
for target, n_is, n_oos, z in ((0.0225, 2_869, 1_261, 3.234),
                               (0.0079, 724, 306, 3.22),
                               (0.05, 15_350, 6_537, 2.955)):
    need = trades_needed(1.0, target, n_is, z, n_oos)
    back = min_detectable(1.0, need, z, need * n_oos / n_is)
    check(f"target {target}R: the returned sample detects it",
          round(back / target, 4), 1.0)
check("a target already within reach needs fewer trades than were had",
      bool(trades_needed(1.0, 1.0, 5_000, 3.0, 2_000) < 5_000), True)
check("a missing hurdle returns no figure rather than a number",
      trades_needed(1.0, None, 5_000, 3.0, 2_000), float("inf"))

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall ruled-out checks passed")
