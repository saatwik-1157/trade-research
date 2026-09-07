"""What the model would have cost, through the engine every other result used.

**There is no second simulator here.** Sections 18 and 37 forbid one, and the
project has a stronger reason than the brief: every measured figure in
`CLAUDE.md` — the −1.50 points per trade for `rsi_reversion`, the 50.5–52.7%
breakeven win rates, the bracket sweeps, the exit searches — came out of
`tools/rule_backtest.simulate`. A model evaluated by a different simulator
could not be compared with any of them, and the comparison is the point.

So this module does one thing: it turns a model's predictions into the signal
array `simulate()` already takes, and hands it the same spread, the same
ATR-scaled bracket and the same next-bar-open entry rule.

**The friction is real and inherited.** `simulate()` enters at the *next bar's
open* from a signal read on closed bars, books the **loss** when one bar's
range covers both the stop and the target, and charges the spread on every
trade. Those three are why its numbers are lower than a naive evaluation's, and
they are exactly the assumptions section 18 asks to be honoured.

**A prediction is not a trade until it clears the threshold.** Below it the
signal is 0 — flat — and `simulate()` skips the bar. That is what makes the
threshold-sensitivity probe in `checks.py` meaningful: moving it changes how
many trades exist, not just how they are scored.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any


def _toolkit(module: str):  # noqa: ANN202
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    tools = os.path.join(root, "tools")
    if os.path.isdir(tools) and tools not in sys.path:
        sys.path.insert(0, tools)
    import importlib

    return importlib.import_module(module)


def build_signal(probabilities: dict[int, float], length: int, *, threshold: float) -> Any:
    """A +1/0 signal array over bar indices. Long-only, because the label is.

    L23's `bracket_outcome` label describes a LONG entry, so a model trained on
    it estimates the probability of a long working. Emitting a short below the
    threshold would be evaluating a decision the model was never asked to make
    — which is the "do not compare unrelated models unfairly" rule of section
    13 applied to a model against itself.
    """
    import numpy as np

    signal = np.zeros(length, dtype=float)
    for index, probability in probabilities.items():
        if 0 <= index < length and probability >= threshold:
            signal[index] = 1.0
    return signal


def evaluate(
    bars: list[Any],
    probabilities: dict[int, float],
    *,
    threshold: float,
    spread: float,
    atr_period: int = 14,
    stop_atr: float = 1.5,
    take_profit_atr: float = 1.5,
    max_hold: int = 240,
    symbol: str = "UNKNOWN",
    point: float = 1.0,
) -> dict[str, Any]:
    """Run the model's decisions through `simulate()` and report what happened.

    Returns the toolkit's own `stats()` output plus the trade rows, shaped for
    `statistics.clustered()` — so the economic figures and the significance
    figures are computed from the same trades rather than from two evaluations
    that could disagree.
    """
    import numpy as np

    backtest = _toolkit("rule_backtest")

    opens = np.array([float(b.open) for b in bars], dtype=float)
    highs = np.array([float(b.high) for b in bars], dtype=float)
    lows = np.array([float(b.low) for b in bars], dtype=float)
    closes = np.array([float(b.close) for b in bars], dtype=float)
    atr = backtest.atr_series(highs, lows, closes, atr_period)

    signal = build_signal(probabilities, len(closes), threshold=threshold)
    entries = int((signal != 0).sum())
    if entries == 0:
        return {
            "trades": 0,
            "entries_offered": 0,
            "note": (
                f"no bar reached the {threshold} decision threshold, so the model asked "
                "for no trades. That is a result, not a failure to evaluate."
            ),
            "rows": [],
        }

    trades = backtest.simulate(
        opens,
        highs,
        lows,
        closes,
        signal,
        atr,
        spread,
        sl_atr=stop_atr,
        tp_atr=take_profit_atr,
        max_hold=max_hold,
    )
    summary = dict(backtest.stats(trades, point))

    # `simulate()` is flat between trades, so it takes far fewer trades than
    # there are signals. Reporting both stops a reader assuming every decision
    # was acted on.
    summary["entries_offered"] = entries
    summary["entries_taken"] = len(trades)
    summary["threshold"] = threshold
    summary["spread_charged"] = spread
    summary["engine"] = (
        "tools/rule_backtest.simulate -- the same engine every measured figure in "
        "CLAUDE.md came from"
    )
    summary["friction"] = (
        "entry at the NEXT bar's open from a signal read on closed bars; the spread "
        "charged on every trade; and the LOSS booked when one bar's range covers both "
        "the stop and the target, because intrabar order is unknown."
    )
    summary["rows"] = [
        {
            "symbol": symbol,
            "net": float(row["net"]),
            "gross": float(row["gross"]),
            "bars": int(row["bars"]),
            "reason": row["reason"],
            "entry_idx": int(row["entry_idx"]),
            "entry_time": _epoch(bars[int(row["entry_idx"])].bar_time),
        }
        for row in trades
    ]
    return summary


def _epoch(at: datetime) -> int:
    """Epoch seconds, which is the shape `rule_search`'s clustering expects."""
    return int(at.timestamp())


def drawdown_ratio(rows: list[dict[str, Any]]) -> float | None:
    """Worst peak-to-trough fall as a share of gross profit.

    A ratio rather than an absolute figure because the absolute one is in the
    instrument's own points, and comparing those across symbols is the unit
    error this project documents at length.
    """
    if not rows:
        return None
    equity = 0.0
    peak = 0.0
    worst = 0.0
    gross_profit = 0.0
    for row in rows:
        net = row["net"]
        equity += net
        if net > 0:
            gross_profit += net
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    if gross_profit <= 0:
        return None
    return abs(worst) / gross_profit
