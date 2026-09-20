#!/usr/bin/env python
"""The carry verdict, and the two ways it could quietly become a lie.

`carry_check.py` exists because AUDUSD is the one instrument on this account
whose long side is PAID rather than charged, and a contractual positive is the
only candidate in this repository that does not have to clear a permutation
null. What it does have to clear is the spot drift, and the failures worth
guarding are both failures of understatement:

  * Judging on ONE window. The first version of the tool did, and returned
    CARRY_EXCEEDS_DRIFT at ten years for a symbol that is negative at fifteen.
    A verdict that flips with the window and does not say so is worse than no
    verdict.
  * Losing the demonstrability arithmetic. WINDOW_DEPENDENT invites the
    correct rebuttal that drift is unforecastable, so its variation is noise
    and a certain carry is still positive expected value. The answer is that
    the carry is smaller than one standard error of the drift over any window
    available, and that answer is a calculation, not an opinion. If it ever
    silently returns "a few years", the tool has started promising something.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import numpy as np

from carry_check import carry_stats, demonstrability, judge, spot_stats

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def window(drift_points_per_year, vol=10.0, dd=-30.0):
    """A spot_stats-shaped dict with only the fields judge() reads."""
    return {"drift_points_per_year": drift_points_per_year,
            "vol_pct_annual": vol, "worst_drawdown_pct": dd}


# ---------------------------------------------------------------- the verdict

PAID = carry_stats(4.49, 0.71241, 0.00001, 19.0)      # AUDUSD, roughly
CHARGED = carry_stats(-0.8, 1.14865, 0.00001, 12.0)   # EURUSD, roughly

check("a charged long side is refused outright",
      judge(CHARGED, {10: window(0.0)})[0], "NO_CARRY")

# 1,638 points a year of carry. A window losing more than that is negative.
check("carry beaten in every window is DRIFT_EATS_CARRY",
      judge(PAID, {5: window(-3000.0), 10: window(-2500.0)})[0],
      "DRIFT_EATS_CARRY")

check("carry surviving every window is CARRY_EXCEEDS_DRIFT",
      judge(PAID, {5: window(+500.0), 10: window(-200.0)})[0],
      "CARRY_EXCEEDS_DRIFT")

# The regression that matters. These are the real AUDUSD numbers: positive at
# ten years, negative at fifteen. A tool judging on either alone is confident
# and wrong.
REAL = {3: window(+2097.1, 8.86, -13.9), 5: window(-462.4, 10.15, -21.4),
        10: window(-455.4, 9.65, -29.3), 15: window(-1976.2, 9.77, -47.0),
        25: window(+710.5, 12.12, -48.0)}
verdict, detail = judge(PAID, REAL)
check("the real AUDUSD windows are WINDOW_DEPENDENT", verdict, "WINDOW_DEPENDENT")
check("and judging the 10y window ALONE would have said otherwise",
      judge(PAID, {10: REAL[10]})[0], "CARRY_EXCEEDS_DRIFT")
check("and the 15y window alone would have said the opposite again",
      judge(PAID, {15: REAL[15]})[0], "DRIFT_EATS_CARRY")
check("the detail names the swing rather than just the verdict",
      "changes sign with the window" in detail, True)

# A single window can never be WINDOW_DEPENDENT, so a caller that passes one
# gets a confident answer. That is the trap, and the CLI defaults to five.
check("one window cannot produce the hedged verdict",
      judge(PAID, {10: REAL[10]})[0] == "WINDOW_DEPENDENT", False)

# --------------------------------------------------------- demonstrability

d = demonstrability(PAID, REAL)

# Sharpe = carry% / vol%. The carry is 2.3%/yr against a mean vol near 10.1%.
expect_sharpe = round(PAID["pct_per_year"] / d["spot_vol_pct_annual"], 3)
check("Sharpe is carry over volatility, nothing else",
      d["sharpe_if_spot_is_a_martingale"], expect_sharpe)

# t grows as Sharpe*sqrt(T), so years to 1.96 is (1.96/Sharpe)^2. Recomputing
# it from the ROUNDED Sharpe gives 74.6 against the tool's 74.2, so the
# comparison is made to a tolerance rather than exactly - the tool carries the
# unrounded ratio through, which is the correct thing for it to do.
approx = (1.96 / d["sharpe_if_spot_is_a_martingale"]) ** 2
check("years to t=1.96 follows (1.96/Sharpe)^2",
      abs(d["years_to_t_1_96"] - approx) / approx < 0.01, True)

# The headline. If this ever drops below a human planning horizon, something
# in the conversion or the volatility has gone wrong and the tool is about to
# recommend a trade.
check("AUDUSD needs more than fifty years to demonstrate",
      d["years_to_t_1_96"] > 50.0, True)

# One standard error of a T-year drift estimate is vol/sqrt(T). The carry has
# to be BIGGER than that to be visible, and it is not.
se10 = d["drift_standard_error_by_window"][10]
check("SE of the 10y drift estimate is vol/sqrt(10)",
      se10["se_pct_per_year"], round(d["spot_vol_pct_annual"] / math.sqrt(10), 2))
check("and the carry is smaller than that one standard error",
      se10["carry_over_se"] < 1.0, True)

# It does clear ONE standard error eventually - 1.13 at twenty-five years -
# and an earlier version of this test asserted otherwise and was wrong. One
# SE is not the bar in any case; 1.96 is, and the horizon for that is the
# figure the tool leads with.
se25 = d["drift_standard_error_by_window"][25]
check("the carry does clear one SE by twenty-five years",
      se25["carry_over_se"] > 1.0, True)
check("but not the 1.96 that would actually make it visible",
      se25["carry_over_se"] < 1.96, True)

# A zero or negative carry must not produce a finite horizon - dividing by it
# would report a confident number for an instrument that pays nothing.
dz = demonstrability(CHARGED, REAL)
check("a charged side reports no horizon rather than a negative one",
      dz["years_to_t_1_96"], None)

# ------------------------------------------------------------- spot_stats

# A flat series has no drift and no drawdown; a monotonic riser has drift and
# still no drawdown. Both guard the sign convention.
flat = np.full(600, 1.5)
s = spot_stats(flat, 0.00001)
check("a flat series has zero drift", s["drift_points_per_year"], 0.0)
check("a flat series has zero drawdown", s["worst_drawdown_pct"], 0.0)

rising = np.linspace(1.0, 1.2, 600)
s = spot_stats(rising, 0.00001)
check("a rising series has positive drift", s["drift_points_per_year"] > 0, True)
check("a rising series still has no drawdown", s["worst_drawdown_pct"], 0.0)

falling = np.linspace(1.2, 1.0, 600)
s = spot_stats(falling, 0.00001)
check("a falling series has negative drift", s["drift_points_per_year"] < 0, True)
check("and a drawdown of the whole fall",
      round(s["worst_drawdown_pct"], 1), round(100.0 * (1.0 / 1.2 - 1.0), 1))

# nights_to_repay_spread is the one figure that makes carry look different
# from the harvest loop: pay the spread once, earn every night after.
check("the spread repays in a handful of nights at AUDUSD's rate",
      PAID["nights_to_repay_spread"], round(19.0 / 4.49, 1))
check("and a charged side reports no repayment at all",
      CHARGED["nights_to_repay_spread"], None)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall carry checks passed")
