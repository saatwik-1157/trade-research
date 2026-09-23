#!/usr/bin/env python
"""The gotobi test, and whether it was pointed at the right four hours.

A calendar-and-clock search has one failure mode that swamps all the others:
it can be aimed at the wrong window and return a confident null about hours
nobody claimed anything for. MT5 stamps a bar with the SERVER's wall clock
rendered as a UTC epoch and this broker is UTC+3, so 10:00 JST is 01:00 UTC
is server hour 4. Reading the local hour on a UTC+5:30 machine would have put
the window five and a half hours away, and the output would have looked
exactly the same.

So the clock is derived here rather than trusted, and the detector is shown
to have power before its null is believed -- the same order `cross_search`
and `pairs_search` use.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from gotobi_search import (  # noqa: E402
    FIX_SERVER_HOUR, GOTOBI_DAYS, server_hour, welch, window_returns,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


print()
print("The fix hour is derived from the clocks, not assumed")
# The broker is UTC+3 and JST is UTC+9, so JST == server + 6.
utc_at_fix = (FIX_SERVER_HOUR - 3) % 24
jst_at_fix = (utc_at_fix + 9) % 24
check("server hour 4 is 01:00 UTC", utc_at_fix, 1)
check("and 01:00 UTC is 10:00 JST, the Tokyo fix", jst_at_fix, 10)
# Confirmed against a real timezone conversion rather than the arithmetic alone.
stamp = dt.datetime(2024, 5, 10, 1, 0, tzinfo=dt.UTC)
check("a real conversion agrees",
      stamp.astimezone(dt.timezone(dt.timedelta(hours=9))).hour, 10)
# An MT5 stamp is the SERVER's wall clock rendered as a UTC epoch, so a bar
# at server hour 4 carries an epoch whose hour reads 4 -- NOT the epoch of the
# real 01:00 UTC instant, whose hour reads 1. Building the second kind and
# asserting the first is how this check failed on its first run, and the
# distinction is the whole reason `server_hour` is a plain modulo.
mt5_style = dt.datetime(2024, 5, 10, FIX_SERVER_HOUR, 0, tzinfo=dt.UTC)
check("an MT5 stamp carries the SERVER hour, not the real UTC hour",
      int(server_hour(np.array([int(mt5_style.timestamp())]))[0]),
      FIX_SERVER_HOUR)
check("and the true UTC instant is three hours earlier",
      int(server_hour(np.array([int(stamp.timestamp())]))[0]),
      FIX_SERVER_HOUR - 3)
check("the settlement days are the 5s and 10s",
      list(GOTOBI_DAYS), [5, 10, 15, 20, 25, 30])


print()
print("A window return pairs the right two bars, and drops incomplete days")
# Two full days at hours 0..5, then a third day missing hour 4.
times, closes = [], []
base = dt.datetime(2024, 5, 8, tzinfo=dt.UTC)
price = 100.0
for day in range(3):
    for h in range(6):
        if day == 2 and h == 4:
            continue           # the day that must be dropped
        stamp = base + dt.timedelta(days=day, hours=h)
        # server hour == stored hour, since these are built as server stamps
        times.append(int(stamp.timestamp()))
        price *= 1.01
        closes.append(price)
times = np.array(times)
closes = np.array(closes)
# server_hour reads (epoch // 3600) % 24, so build the expectation the same way
sh = server_hour(times)
start_h, end_h = int(sh[0]), int(sh[4])
dates, rets = window_returns(times, closes, start_h, end_h)
check("only the two complete days produce a return", len(dates), 2)
check("and the return is close(end) over close(start)",
      round(float(rets[0]), 6), round(math.log(1.01 ** 4), 6))
check("a day missing the end bar is dropped, not filled",
      bool(all(d != (base + dt.timedelta(days=2)).date() for d in dates)), True)


print()
print("Welch's t recovers a difference that is there and none that is not")
rng = np.random.default_rng(3)
days = [dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(2400)]
goto = np.isin(np.array([d.day for d in days]), GOTOBI_DAYS)
base_r = rng.normal(0, 0.0015, len(days))
d0, t0 = welch(base_r[goto], base_r[~goto])
check("no planted effect gives a small t", bool(abs(t0) < 2.0), True)
for planted, floor in ((0.0002, 2.5), (0.0005, 5.0)):
    r = base_r + goto * planted
    d, t = welch(r[goto], r[~goto])
    check(f"a planted {planted * 100:.2f}% effect is found",
          bool(t > floor), True)
    # The recovered difference is the planted effect PLUS whatever difference
    # the baseline noise already had between the two groups. Demanding it
    # equal the planted value alone asks the estimator to remove sampling
    # error, which is not what it does -- so the identity is asserted instead.
    check(f"and it recovers planted + baseline exactly, at {planted * 100:.2f}%",
          bool(abs(d - (planted + d0)) < 1e-12), True)
check("unequal variances do not break it (Welch, not Student)",
      bool(np.isfinite(welch(rng.normal(0, 0.01, 300),
                             rng.normal(0, 0.0001, 300))[1])), True)
check("too few observations returns nan rather than a number",
      welch(np.zeros(5), np.zeros(5))[1] != welch(np.zeros(5), np.zeros(5))[1],
      True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall gotobi checks passed")
