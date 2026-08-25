#!/usr/bin/env python
"""Swap conversion and night counting.

Two failure modes are worth guarding, and both bias the same way. A unit
mishandled turns a cost into a rounding error, and a night uncounted turns it
into nothing at all - so every bug here flatters the result. The tests assert
the direction as well as the number.
"""
from __future__ import annotations

import calendar
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from swap import (CURRENCY_DEPOSIT, CURRENCY_MARGIN, CURRENCY_SYMBOL, POINTS,
                  implausible, nights_between, swap_points_per_night,
                  value_per_point)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:52s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


class Info:
    """Minimal stand-in for MT5's symbol_info."""

    def __init__(self, name="TEST", swap_mode=POINTS, swap_long=-1.0, swap_short=-1.0,
                 point=1e-05, tick_size=1e-05, tick_value=1.0, base="EUR",
                 profit="USD", margin="EUR"):
        self.name = name
        self.swap_mode = swap_mode
        self.swap_long = swap_long
        self.swap_short = swap_short
        self.point = point
        self.trade_tick_size = tick_size
        self.trade_tick_value = tick_value
        self.currency_base = base
        self.currency_profit = profit
        self.currency_margin = margin


def ts(y, m, d, hh=0, mm=0):
    return calendar.timegm(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timetuple())


print("\nPoints mode - already the right unit, passed through unchanged")
lng, sht, note = swap_points_per_night(Info(swap_mode=POINTS, swap_long=294.231,
                                            swap_short=-304.231), 1.0, "USD")
check("a positive carry stays positive", lng, 294.231)
check("a negative carry stays negative", sht, -304.231)
check("no conversion note is raised", note, None)

print("\nDeposit-currency mode - divides by the value of a point")
# 1 point on 1 lot is worth 0.01 USD, so -0.70 USD a night is -70 points.
info = Info(name="DE40", swap_mode=CURRENCY_DEPOSIT, swap_long=-0.70, swap_short=-1.12,
            point=0.01, tick_size=0.01, tick_value=0.01, base="USD", profit="EUR",
            margin="EUR")
check("value of one point is the tick value here", value_per_point(info), 0.01)
lng, sht, note = swap_points_per_night(info, 0.0, "USD")
check("long cost converts to points", round(lng, 2), -70.0)
check("short cost converts to points", round(sht, 2), -112.0)
check("a both-sided charge stays negative both ways", (lng < 0, sht < 0), (True, True))

print("\nBase-currency mode - the symbol's own price does the conversion")
# Swap is -0.70 EUR a night; EURUSD trades at 1.20, so -0.84 USD; a point is
# worth 1.00 USD, so -0.84 points.
info = Info(name="EURUSD", swap_mode=CURRENCY_SYMBOL, swap_long=-0.70, swap_short=-1.00,
            point=1e-05, tick_size=1e-05, tick_value=1.0, base="EUR", profit="USD")
lng, sht, note = swap_points_per_night(info, 1.20, "USD")
check("base currency is converted at the symbol price", round(lng, 4), -0.84)
check("the short side converts too", round(sht, 4), -1.2)
check("no gap is reported for an XXXUSD pair", note, None)

print("\nBase currency already the deposit currency - no rate needed")
info = Info(name="USDJPY", swap_mode=CURRENCY_SYMBOL, swap_long=-0.10, swap_short=-0.60,
            point=0.001, tick_size=0.001, tick_value=0.6279, base="USD", profit="JPY")
lng, sht, note = swap_points_per_night(info, 0.0, "USD")
check("no conversion is applied when base is deposit", round(lng, 4),
      round(-0.10 / 0.6279, 4))
check("and no gap is raised", note, None)

print("\nMargin-currency mode - deposit margin converts, foreign margin gaps")
info = Info(name="US500", swap_mode=CURRENCY_MARGIN, swap_long=-0.31, swap_short=-0.26,
            point=0.01, tick_size=0.01, tick_value=0.01, base="USD", profit="USD",
            margin="USD")
lng, _, note = swap_points_per_night(info, 0.0, "USD")
check("a USD-margin symbol converts exactly", round(lng, 2), -31.0)
check("and raises no gap", note, None)

print("\nAn unconvertible unit is refused, never guessed")
info = Info(name="EURGBP", swap_mode=CURRENCY_SYMBOL, swap_long=-0.70, swap_short=-1.00,
            base="EUR", profit="GBP", margin="EUR")
lng, sht, note = swap_points_per_night(info, 0.86, "USD")
check("no number is returned for a cross", (lng, sht), (None, None))
check("the reason names the third instrument problem", "third instrument" in (note or ""), True)

info = Info(name="ODD", swap_mode=5, swap_long=-2.0, swap_short=-2.0)
lng, _, note = swap_points_per_night(info, 1.0, "USD")
check("an interest-mode symbol is refused", lng, None)
check("and says why", "INTEREST_OPEN" in (note or ""), True)

print("\nNight counting - midnights crossed, not bars held or hours elapsed")
# 2026-08-25 is a Tuesday; 2026-08-26 a Wednesday.
check("a hold inside one day crosses nothing",
      nights_between(ts(2026, 8, 25, 1), ts(2026, 8, 25, 23), triple_dow=-1), 0)
check("22:00 to 02:00 crosses one midnight",
      nights_between(ts(2026, 8, 25, 22), ts(2026, 8, 26, 2), triple_dow=-1), 1)
check("a same-second hold crosses nothing",
      nights_between(ts(2026, 8, 25), ts(2026, 8, 25), triple_dow=-1), 0)
check("six days crosses six midnights",
      nights_between(ts(2026, 8, 20, 12), ts(2026, 8, 26, 12), triple_dow=-1), 6)

print("\nThe triple day is billed three times, and it is the Wednesday midnight")
# Wednesday is ENUM_DAY_OF_WEEK 3. The midnight opening 2026-08-26 is that day.
check("crossing Wednesday midnight bills three nights",
      nights_between(ts(2026, 8, 25, 22), ts(2026, 8, 26, 2), triple_dow=3), 3)
check("crossing Tuesday midnight bills one",
      nights_between(ts(2026, 8, 24, 22), ts(2026, 8, 25, 2), triple_dow=3), 1)
check("a week's hold picks up exactly one triple",
      nights_between(ts(2026, 8, 20, 12), ts(2026, 8, 27, 12), triple_dow=3), 7 + 2)
check("ignoring the triple day understates the bill",
      nights_between(ts(2026, 8, 25, 22), ts(2026, 8, 26, 2), triple_dow=3)
      > nights_between(ts(2026, 8, 25, 22), ts(2026, 8, 26, 2), triple_dow=-1), True)

print("\nDirection - swap must never improve a result")
info = Info(name="DE40", swap_mode=CURRENCY_DEPOSIT, swap_long=-0.70, swap_short=-1.12,
            point=0.01, tick_size=0.01, tick_value=0.01)
lng, sht, _ = swap_points_per_night(info, 0.0, "USD")
nights = nights_between(ts(2026, 8, 20, 12), ts(2026, 8, 26, 12), triple_dow=3)
check("six nights over a triple day is eight charges", nights, 8)
check("the long bill is negative and material", round(lng * nights, 1), -560.0)

print("\nThe plausibility fence - a swap larger than the daily range is a unit bug")
check("a normal charge passes", implausible(-60.0, 20213.0), False)
check("a charge at 57% of the range is refused", implausible(-304.23, 535.0), True)
check("gold read as ounces is refused", implausible(-584241.0, 2196.0), True)
check("the fence is off when no scale is known", implausible(-584241.0, None), False)
check("a None figure is not flagged", implausible(None, 500.0), False)
# The boundary matters: the fence must not fire on a merely expensive symbol.
check("exactly at the threshold passes", implausible(-50.0, 500.0), False)
check("just past the threshold is refused", implausible(-50.01, 500.0), True)

print("")
print("simulate() charging financing - opting in must never flatter a result")
import numpy as np
from rule_backtest import simulate

# One long trade that runs to take-profit five days later. D1 bars at midnight,
# so bar index differences are exactly nights slept.
n = 100
day = 86400
times = np.array([ts(2026, 3, 2) + i * day for i in range(n)], dtype="int64")
c = np.full(n, 100.0)
o = np.full(n, 100.0)
h = np.full(n, 100.0)
l = np.full(n, 100.0)
h[70:] = 103.0           # take profit is hit from bar 70 on
atr = np.full(n, 1.0)
sig = np.zeros(n, dtype=int)
sig[64] = 1              # actionable at bar 65

base = simulate(o, h, l, c, sig, atr, spread=0.0, sl_atr=1.5, tp_atr=1.5)
check("the trade is found", len(base), 1)
check("it entered at bar 65 and exited at bar 70",
      (base[0]["entry_idx"], base[0]["bars"]), (65, 6))
check("no financing is charged by default", base[0]["financing"], 0.0)
check("and no nights are counted", base[0]["nights"], 0)

# -0.5 price units a night, long side, no triple day in this window.
charged = simulate(o, h, l, c, sig, atr, spread=0.0, sl_atr=1.5, tp_atr=1.5,
                   times=times, swap=(-0.5, -0.2), triple_dow=-1)
check("five nights are slept between bar 65 and bar 70", charged[0]["nights"], 5)
check("the long rate is applied, not the short one", charged[0]["financing"], -2.5)
check("net is worse once financing is charged",
      charged[0]["net"] < base[0]["net"], True)
check("gross is untouched by financing", charged[0]["gross"], base[0]["gross"])

# A carry CREDIT must survive with its sign - AUDUSD pays to be long here.
credit = simulate(o, h, l, c, sig, atr, spread=0.0, sl_atr=1.5, tp_atr=1.5,
                  times=times, swap=(+0.5, -0.2), triple_dow=-1)
check("a positive carry improves the trade", credit[0]["financing"], 2.5)

# The short side must draw the short rate.
sig_s = np.zeros(n, dtype=int)
sig_s[64] = -1
ls = np.full(n, 100.0)
ls[70:] = 97.0
short = simulate(o, h, ls, c, sig_s, atr, spread=0.0, sl_atr=1.5, tp_atr=1.5,
                 times=times, swap=(-0.5, -0.2), triple_dow=-1)
check("a short trade is charged the short rate", round(short[0]["financing"], 4), -1.0)

# The triple day multiplies. 2026-03-02 is a Monday, so bar 5 is Saturday the
# 7th and the window bars 5..10 covers one Wednesday.
tri = simulate(o, h, l, c, sig, atr, spread=0.0, sl_atr=1.5, tp_atr=1.5,
               times=times, swap=(-0.5, -0.2), triple_dow=5)
check("a triple day inside the hold costs more", tri[0]["financing"] < -2.5, True)
# The entry-instant boundary must NOT be billed, or every trade entered on
# the triple day would be overcharged two nights it never slept.
edge = simulate(o, h, l, c, sig, atr, spread=0.0, sl_atr=1.5, tp_atr=1.5,
                times=times, swap=(-0.5, -0.2), triple_dow=3)
check("entering ON the triple day is not billed for it", edge[0]["financing"], -2.5)
check("and it costs exactly two extra nights", round(tri[0]["financing"], 4), -3.5)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall swap checks passed")
