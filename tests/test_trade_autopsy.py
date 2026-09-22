#!/usr/bin/env python
"""The live-trade autopsy, and the three ways it would flatter a losing book.

This tool exists to answer "which trades make money" without falling into
the way that question is usually answered, so its failure modes are all
failures of being too encouraging:

  * **A guessed enum.** The first version had DEAL_REASON 3=SL and 4=TP,
    which is wrong by one, and printed "stop loss: +297.31 net, 93.9% win"
    -- the harvest's numbers under the stop's name. A reader would have
    concluded the stops were profitable. The mapping is now checked against
    MetaTrader5's own constants where the package is importable, and against
    the fixed integers where it is not (CI has no MT5).
  * **A permutation test that cannot say no.** If the null were built
    wrongly -- resampling instead of shuffling, or shuffling the returns
    rather than the labels -- it would report a real ranking where there is
    none. So it is checked twice: planted structure must be FOUND, and pure
    noise must NOT be.
  * **Cost attribution that reads the outcomes.** The whole argument rests
    on cost being measured independently. A residual computed with the sign
    flipped, or a drag derived from the same returns it explains, would make
    any loss look fully explained.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import trade_autopsy as ta  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def trade(symbol="EURUSD", r=0.1, net=1.0, reason=3, direction="long",
          sl=0.0010, hour=10):
    return {"symbol": symbol, "r_multiple": r, "net_profit": net,
            "close_reason": reason, "direction": direction,
            "open_time": f"2026-09-01T{hour:02d}:00:00",
            "bracket": {"sl_distance": sl}}


# ------------------------------------------------------------------ the enum

print()
print("DEAL_REASON is read, not remembered")
try:
    import MetaTrader5 as mt5
except ImportError:
    # CI has no MT5. The integers are still pinned, so a silent edit to the
    # table is caught on every runner rather than only on Windows.
    print("  note: MetaTrader5 absent, checking the pinned integers only")
    expected = {0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT", 4: "SL",
                5: "TP", 6: "SO"}
else:
    expected = {mt5.DEAL_REASON_CLIENT: "CLIENT", mt5.DEAL_REASON_MOBILE: "MOBILE",
                mt5.DEAL_REASON_WEB: "WEB", mt5.DEAL_REASON_EXPERT: "EXPERT",
                mt5.DEAL_REASON_SL: "SL", mt5.DEAL_REASON_TP: "TP",
                mt5.DEAL_REASON_SO: "SO"}
    check("the package agrees SL is 4 and TP is 5",
          (mt5.DEAL_REASON_SL, mt5.DEAL_REASON_TP), (4, 5))

check("3 is the harvest, not the stop", "harvest" in ta.REASON[3], True)
check("4 is the stop loss", ta.REASON[4], "stop loss")
check("5 is the take profit", ta.REASON[5], "take profit")
# The specific inversion that produced the wrong table.
check("3 is NOT labelled a stop", "stop" in ta.REASON[3], False)
check("4 is NOT labelled a take profit", "take profit" in ta.REASON[4], False)
for code, name in expected.items():
    check(f"code {code} has a label", code in ta.REASON, True)

# --------------------------------------------------------- the permutation

print()
print("The permutation test can both find structure and refuse to")

# Planted: one symbol genuinely better by a wide margin. Must be found.
rng = np.random.default_rng(4)
planted = []
for i in range(700):
    sym = "AAA" if i % 7 == 0 else f"S{i % 7}"
    r = rng.normal(0.8 if sym == "AAA" else 0.0, 0.3)
    planted.append(trade(symbol=sym, r=float(r)))
got = ta.permutation_spread(planted, rounds=400, seed=1)
check("a planted strong symbol is detected", got["p_value"] < 0.01, True)
check("and its real spread exceeds the null's 95th percentile",
      got["real_spread_r"] > got["null_p95_spread_r"], True)

# Pure noise, same shape. Must NOT be found. Across several independent
# draws rather than one: a single draw is itself a coin flip, and the first
# version of this test reused an rng already advanced by the planted loop
# and tripped on one unlucky sample. Measured over eight fresh seeds the
# real rate below 0.05 is 0 of 8.
flagged = 0
for seed in range(8):
    r2 = np.random.default_rng(seed)
    noise = [trade(symbol=f"S{i % 7}", r=float(r2.normal(0, 0.3)))
             for i in range(700)]
    if ta.permutation_spread(noise, rounds=300, seed=seed)["p_value"] <= 0.05:
        flagged += 1
check("pure noise is not called a real ranking (8 draws)", flagged <= 1, True)

# The null must SHUFFLE the labels, not resample them, and this tells the
# two apart behaviourally rather than by grepping the docstring -- which is
# what the previous version did, and it failed on "shuffling" not containing
# "shuffle" while the code was correct.
#
# With one trade per symbol every permutation is a relabelling of the same
# seven values, so max-minus-min is IDENTICAL in every round and p must be
# exactly 1.0. Resampling with replacement would sometimes draw duplicates,
# shrink the spread, and let p fall below 1.
singles = [trade(symbol=f"S{i}", r=float(v))
           for i, v in enumerate([-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0])]
got = ta.permutation_spread(singles, rounds=200, seed=2)
check("a shuffle cannot change the spread when groups are size one",
      got["p_value"], 1.0)
check("and that spread is the full range of the values",
      got["real_spread_r"], 6.0)
check("the null's mean equals the real spread, not something smaller",
      got["null_mean_spread_r"], 6.0)

# -------------------------------------------------------------- summarise

print()
print("Summaries count what they say they count")
rs = [trade(r=0.5, net=2.0), trade(r=-1.0, net=-5.0), trade(r=0.25, net=1.0)]
s = ta.summarise(rs)
check("trades", s["trades"], 3)
check("net is the sum of net_profit", s["net"], -2.0)
check("mean R is the mean of r_multiple", s["mean_r"], round((0.5 - 1.0 + 0.25) / 3, 4))
check("win rate counts POSITIVE net, not positive R", s["win_rate"], round(2 / 3, 3))

# A break-even trade is not a win. It cost nothing and proves nothing.
s = ta.summarise([trade(r=0.0, net=0.0), trade(r=1.0, net=1.0)])
check("a zero-profit trade is not counted as a win", s["win_rate"], 0.5)

# ----------------------------------------------------------------- usable

print()
print("A trade with no bracket is dropped, never zeroed")
raw = [trade(), {"symbol": "X", "r_multiple": None, "net_profit": 1.0,
                 "bracket": {"sl_distance": 0.001}},
       {"symbol": "Y", "r_multiple": 0.5, "net_profit": 1.0, "bracket": {}},
       {"symbol": "Z", "r_multiple": 0.5, "net_profit": 1.0}]
u = ta.usable(raw)
check("only the complete row survives", len(u), 1)
check("a null R-multiple is dropped",
      any(r.get("symbol") == "X" for r in u), False)
check("a bracket with no stop distance is dropped",
      any(r.get("symbol") == "Y" for r in u), False)
check("a missing bracket is dropped",
      any(r.get("symbol") == "Z" for r in u), False)

# -------------------------------------------------------------- end to end

print()
print("End to end: cost attribution has the sign the argument needs")
with tempfile.TemporaryDirectory() as d:
    # Every trade loses exactly its cost and nothing else. A correct
    # attribution must return a residual of zero.
    ledger = os.path.join(d, "l.jsonl")
    hurdle = os.path.join(d, "h.json")
    cost_r = 0.02                       # 2% of R per trade
    breakeven = 0.5 + cost_r / 2.0      # the relation the tool inverts
    with open(ledger, "w", encoding="utf-8") as fh:
        for i in range(400):
            fh.write(json.dumps(trade(symbol="EURUSD", r=-cost_r,
                                      net=-0.2, reason=3)) + "\n")
    with open(hurdle, "w", encoding="utf-8") as fh:
        json.dump({"symbols": {"EURUSD": {
            "breakeven_win_rate_median_spread": breakeven}}}, fh)

    out = os.path.join(d, "o.json")
    rc = ta.main.__wrapped__ if hasattr(ta.main, "__wrapped__") else ta.main
    argv = sys.argv
    sys.argv = ["trade_autopsy", "--ledger", ledger, "--hurdle", hurdle,
                "--rounds", "50", "--out", out]
    try:
        rc()
    finally:
        sys.argv = argv
    rep = json.load(open(out, encoding="utf-8"))
    ca = rep["cost_attribution"]
    check("the breakeven win rate inverts back to the cost",
          ca["predicted_cost_r"], -cost_r)
    check("a book that loses exactly its cost has a zero residual",
          abs(ca["residual_mean_r"]) < 1e-9, True)
    check("and cost then explains 100% of the loss",
          ca["share_of_loss_explained_by_cost"], 1.0)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall trade-autopsy checks passed")
