"""Checks on the indicator layer, run with plain python - no test framework needed.

    python tests/test_indicators.py

The point of these is narrow and specific: the whole project rests on the claim
that the numbers are computed rather than generated, so the computation itself
has to be checked against values worked out independently. An indicator that is
subtly wrong is worse than one that is missing, because it still looks right.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import indicators  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want, tol=1e-6):
    if want is None:
        ok = got is None
    elif got is None:
        ok = False
    else:
        ok = abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<46} got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def frame(closes, highs=None, lows=None, volumes=None) -> pd.DataFrame:
    n = len(closes)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs if highs is not None else [c * 1.01 for c in closes],
            "Low": lows if lows is not None else [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": volumes if volumes is not None else [1_000_000] * n,
        },
        index=idx,
    )


print("\nSMA and returns")
closes = list(range(1, 61))  # 1..60, so the last 20 are 41..60
df = frame(closes)
check("sma20 of 41..60", indicators.sma(df["Close"], 20), float(np.mean(range(41, 61))))
check("sma200 with only 60 bars is None", indicators.sma(df["Close"], 200), None)

print("\nRSI")
# A series that only ever rises has no down moves, so RSI pins at 100.
check("rsi14 on a monotonic rise", indicators.rsi(df["Close"], 14), 100.0)
# A flat series has zero gain and zero loss; avg_loss is 0, so it also pins.
check("rsi14 on a flat series", indicators.rsi(pd.Series([50.0] * 60), 14), 100.0)
check("rsi14 with too little history", indicators.rsi(pd.Series([1.0] * 10), 14), None)

# Independent Wilder RSI on a known series, computed with an explicit loop.
rng = np.random.default_rng(7)
walk = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
delta = walk.diff().dropna()
n = 14
gains = delta.clip(lower=0).to_numpy()
losses = (-delta).clip(lower=0).to_numpy()
ag, al = gains[:n].mean(), losses[:n].mean()
for g, l in zip(gains[n:], losses[n:]):
    ag = (ag * (n - 1) + g) / n
    al = (al * (n - 1) + l) / n
expected_rsi = 100 - 100 / (1 + ag / al)
# The module seeds with an EWM rather than an SMA, so early bars differ slightly;
# after 300 bars the two converge to well within a tenth of a point.
check("rsi14 vs independent Wilder loop", indicators.rsi(walk, 14), expected_rsi, tol=0.1)

print("\nATR and true range")
# Gapping series: each bar's true range is dominated by the gap from prev close.
d = frame([10, 20, 30, 40] * 15)
tr = indicators.true_range(d)
# The first bar has no previous close, so the two gap terms are undefined and
# true range is conventionally just the bar's own range.
check("first bar's true range is its high minus low",
      float(tr.iloc[0]), float(d["High"].iloc[0] - d["Low"].iloc[0]))
# Every later bar here gaps by 10, which must dominate the intrabar range.
check("gap dominates true range on a gapping series",
      float(tr.iloc[1]) >= 10.0, True, tol=0)
check("atr14 is positive on a gapping series", indicators.atr(d, 14) > 0, True, tol=0)
check("atr14 with too little history", indicators.atr(frame([1, 2, 3]), 14), None)

print("\nMACD")
flat = pd.Series([100.0] * 100)
m = indicators.macd(flat)
check("macd line on a flat series", m["macd"], 0.0)
check("macd histogram on a flat series", m["histogram"], 0.0)

print("\nMax drawdown")
# Rises to 100, falls to 50: a 50% drawdown.
dd = pd.Series([50, 75, 100, 80, 50, 60])
check("max drawdown of a 100 to 50 fall", indicators.max_drawdown(dd), -0.5)
check("max drawdown of a monotonic rise", indicators.max_drawdown(pd.Series([1, 2, 3, 4])), 0.0)

print("\nBeta")
# A series built as exactly 2x the benchmark's log returns must have beta 2.
bench = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400))), index=pd.bdate_range("2020-01-01", periods=400))
stock = pd.Series(100 * np.exp(np.cumsum(np.log(bench / bench.shift(1)).fillna(0) * 2)), index=bench.index)
check("beta of a 2x-levered series", indicators.beta(stock, bench), 2.0, tol=0.02)
check("beta of a series against itself", indicators.beta(bench, bench), 1.0, tol=0.02)
check("beta with no benchmark", indicators.beta(bench, None), None)

print("\nSwing points and levels")
# One clean peak at index 10 and one clean trough at index 20.
prices = [10] * 5 + [11, 12, 13, 14, 15, 20, 15, 14, 13, 12, 11] + [10] * 4 + [5] + [10] * 10
sw = frame(prices)
highs, lows = indicators.swing_points(sw, left=5, right=5)
check("finds the single dominant swing high", any(abs(p - 20 * 1.01) < 1e-6 for _, p in highs), True, tol=0)
check("finds the single dominant swing low", any(abs(p - 5 * 0.99) < 1e-6 for _, p in lows), True, tol=0)

lv = indicators.support_resistance(sw, price=10.0)
check("support levels all sit below price", all(s < 10.0 for s in lv["support"]), True, tol=0)
check("resistance levels all sit above price", all(r > 10.0 for r in lv["resistance"]), True, tol=0)

print("\nFibonacci")
# Low then high, so the swing is upward and levels retrace down from the high.
fib_df = frame(list(range(10, 110)))
fib = indicators.fibonacci(fib_df)
check("swing direction is up when the low comes first", fib["direction"] == "up", True, tol=0)
span = fib["swing_high"] - fib["swing_low"]
check("0.5 level is the midpoint of the swing", fib["levels"]["0.500"], fib["swing_high"] - span * 0.5)
check("0.618 level sits below the 0.382 level",
      fib["levels"]["0.618"] < fib["levels"]["0.382"], True, tol=0)
check("fibonacci refuses to guess on a short series", indicators.fibonacci(frame([1, 2, 3])), None)

print("\nMissing data returns None rather than a plausible value")
short = frame([100, 101, 102])
allf = indicators.compute_all(short)
check("sma200 is None on 3 bars", allf["sma200"], None)
check("rsi14 is None on 3 bars", allf["rsi14"], None)
check("adx14 is None on 3 bars", allf["adx14"], None)
check("beta is None with no benchmark", allf["beta_vs_benchmark"], None)
check("price is still reported", allf["price"], 102.0)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}\n")
    raise SystemExit(1)
print("All indicator checks passed.\n")
