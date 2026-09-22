#!/usr/bin/env python
"""The exit simulator, and the two exits added to answer "never take a loss".

`simulate_exit` produces published numbers, so a change to it can restate
history silently. Two things were added on 2026-09-23 and both need pinning:

  * `sl_atr=None` removes the stop entirely. A stop check that is skipped by
    accident rather than by request would turn every exit into an unbounded
    one and make every recorded figure wrong in the flattering direction.
  * `profit_exit` closes at the first bar that is NET positive. The spread has
    to be cleared or "in profit" is a gross figure that books a loss - the
    same error as reading a quote instead of a fill.

The result these were built to measure is counter-intuitive enough to be
worth asserting directly: closing at the first profit means closing at
approximately BREAKEVEN, because the moment the spread is covered is the
moment it exits. So the winners make nothing and the losers still take the
full stop. That asymmetry is the whole finding, and a future edit that made
`profit_exit` capture more than the spread would quietly erase it.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from exit_search import EXITS, simulate_exit  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def series(closes, band=0.0005):
    """OHLC from a close path. `band` is the intrabar wiggle either side.

    It is a parameter because it decides the outcome and an earlier version
    of this file hard-coded it at 0.0005, which at price 100 is 0.05 against
    a 0.02 spread -- so `profit_exit` fired on the next bar's high before the
    price could ever reach the stop. That is realistic behaviour and it made
    three checks here assert the wrong thing. Where a test needs the stop to
    win the race, the band has to be smaller than the spread.
    """
    c = np.asarray(closes, dtype=float)
    return c.copy(), c * (1 + band), c * (1 - band), c


def one(closes, params, spread=0.02, atr_v=1.0, long=True, max_hold=5000,
        band=0.0005):
    o, h, low, c = series(closes, band)
    n = len(c)
    atr = np.full(n, atr_v)
    sig = np.zeros(n)
    sig[59] = 1 if long else -1
    t = simulate_exit(o, h, low, c, sig, atr, spread, "x", params, max_hold=max_hold)
    return t[0] if t else None


RISE = list(np.linspace(100, 100, 60)) + list(np.linspace(100, 130, 300))
FALL = list(np.linspace(100, 100, 60)) + list(np.linspace(100, 70, 300))
DIP = (list(np.linspace(100, 100, 60)) + list(np.linspace(100, 96, 200))
       + list(np.linspace(96, 105, 200)))

# ------------------------------------------------------------- the new exits

print()
print("The two exits are registered and reachable")
names = [n for n, _k, _p in EXITS]
check("hold_for_profit is in the table", "hold_for_profit" in names, True)
check("hold_for_profit_nostop is in the table",
      "hold_for_profit_nostop" in names, True)
params = {n: p for n, _k, p in EXITS}
check("the no-stop variant really has no stop",
      params["hold_for_profit_nostop"]["sl_atr"], None)
check("and the other keeps a real stop",
      params["hold_for_profit"]["sl_atr"], 1.5)
check("both raise max_hold above the 480 default, or the hold is capped",
      all(params[n].get("max_hold", 0) > 480
          for n in ("hold_for_profit", "hold_for_profit_nostop")), True)

# --------------------------------------------------------------- no stop

print()
print("sl_atr=None removes the stop, and only when asked")
PROFIT = {"sl_atr": None, "profit_exit": True, "max_hold": 5000}
STOPPED = {"sl_atr": 1.5, "profit_exit": True, "max_hold": 5000}

t = one(FALL, STOPPED)
check("with a stop, a falling price is stopped out", t["reason"], "sl")
t = one(FALL, PROFIT)
check("without one it is NOT stopped", t["reason"] != "sl", True)
check("and it runs to the end of the data instead", t["reason"], "timeout")
# The number that matters: the loss is no longer bounded by the stop.
bounded = one(FALL, {"sl_atr": 1.5, "tp_atr": 1.5})["net"]
unbounded = one(FALL, PROFIT)["net"]
check("the unbounded loss is far larger than the bounded one",
      unbounded < bounded * 5, True)

# A stop must still fire for every exit that asks for one - the None path
# must not leak into the others.
check("a normal bracket still stops out", one(FALL, {"sl_atr": 1.5, "tp_atr": 1.5})["reason"], "sl")

# ------------------------------------------------------- profit means NET

print()
print("'In profit' is net of the spread, and is therefore breakeven")
t = one(RISE, PROFIT, spread=0.02)
check("a rising price exits on profit", t["reason"], "profit")
# The finding: exiting at the first net-positive bar captures the spread and
# nothing more. If this ever returns a meaningfully positive number, the
# exit has started capturing upside and the comparison below is void.
check("and the net captured is ~zero, not a gain", abs(t["net"]) < 1e-9, True)

t_big = one(RISE, PROFIT, spread=0.5)
check("a wider spread still exits at ~zero net", abs(t_big["net"]) < 1e-9, True)
check("but it takes longer to get there", t_big["bars"] > t["bars"], True)

# A short must work the same way, or the asymmetry is a sign error.
t_short = one(FALL, PROFIT, long=False)
check("a short exits on profit when the price falls", t_short["reason"], "profit")
check("also at ~zero net", abs(t_short["net"]) < 1e-9, True)

# ------------------------------------------------- the stop wins the race

print()
print("With a stop in place, hold-for-profit IS the bracket")
# A dip below the stop that later recovers. The band is set to zero so the
# price has no upward wiggle to exit on before it falls - otherwise
# profit_exit fires on the next bar and the race is never run.
FLAT = 0.0
dip_bracket = one(DIP, {"sl_atr": 1.5, "tp_atr": 1.5}, band=FLAT)
dip_hold = one(DIP, STOPPED, band=FLAT)
check("the dip stops the bracket out", dip_bracket["reason"], "sl")
check("and stops hold_for_profit out identically", dip_hold["reason"], "sl")
check("for the same loss", round(dip_hold["net"], 9), round(dip_bracket["net"], 9))
check("at the same bar, because the stop is what ended both",
      dip_hold["bars"], dip_bracket["bars"])
# Only with the stop removed does it survive to recover.
dip_nostop = one(DIP, PROFIT, band=FLAT)
check("only removing the stop lets it wait for the recovery",
      dip_nostop["reason"], "profit")
check("and the wait is much longer", dip_nostop["bars"] > dip_hold["bars"], True)

# The realistic case, restored as its own check rather than as an accident:
# with an ordinary intrabar band WIDER than the spread, hold_for_profit
# exits almost immediately on noise, long before any stop or target.
noisy = one(DIP, STOPPED, band=0.0005)
check("with a band wider than the spread it exits on noise instead",
      noisy["reason"], "profit")
check("within a couple of bars", noisy["bars"] <= 3, True)
check("capturing ~nothing", abs(noisy["net"]) < 1e-9, True)

# ------------------------------------------------- losses hide, not vanish

print()
print("Refusing to close does not remove a loss, it hides it in open trades")
# Many signals on a path that mostly falls. With no stop and no deadline,
# trades that never reach profit are still open at the end of the data and
# are booked at timeout - they do not disappear from the count.
o, h, low, c = series(FALL)
n = len(c)
atr = np.full(n, 1.0)
sig = np.zeros(n)
sig[59:300:20] = 1
t_all = simulate_exit(o, h, low, c, sig, atr, 0.02, "x", PROFIT, max_hold=5000)
reasons = {x["reason"] for x in t_all}
check("a never-recovering path yields no 'profit' exits at all",
      "profit" in reasons, False)
check("they are all still open at the end, booked at timeout",
      reasons, {"timeout"})
check("and every one of them is a loss",
      all(x["net"] < 0 for x in t_all), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall exit-search checks passed")
