"""Two kinds of number, kept apart because they answer different questions.

Section 22 is explicit: *do not treat ML accuracy as equivalent to trading
profitability*. So this module returns two dictionaries and never merges them.

  * **`classification()`** — accuracy, precision, recall, F1, log loss, Brier,
    and the confusion matrix. What the model got right.
  * **`economic()`** — expected value per trade, win rate, profit factor and
    the worst drawdown of the equity path. What acting on it would have cost.

A model can score well on the first and lose money on the second, and this
repository has measured exactly that shape: `rsi_reversion` separates from a
coin flip on none of its readings and still loses 1.50 points per trade to the
spread. A dashboard that averaged the two into one "score" would hide it.

**Accuracy is never reported alone.** Section 19 of L24's brief and §22 of this
one both say so, and the reason is arithmetic: a labelled set that is 70% WIN
gives 70% accuracy to a model that always says WIN. `majority_share` is
returned beside it so the comparison is unavoidable.

**Calibration comes from `app.monitoring.stats`.** L29 already implements the
Brier score, reliability bins and expected calibration error. A second copy
would be a second answer to "is this model calibrated", and the one nobody
looked at would be the one quoted.
"""

from __future__ import annotations

import math
from typing import Any

from app.analytics import metrics as _metrics
from app.monitoring.stats import (
    brier_score,
    expected_calibration_error,
    reliability_bins,
)

# Below this many rows the metrics are reported with a warning rather than
# suppressed: a figure over 12 trades is a figure, and hiding it would leave a
# caller wondering whether the run worked. The same threshold the backtester
# uses to withhold its own summary statistics.
MIN_SAMPLE = 20


def _safe_divide(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def classification(
    probabilities: list[float], outcomes: list[int], *, threshold: float = 0.5
) -> dict[str, Any]:
    """What the model got right. Never an economic claim.

    `outcomes` are 0/1 under the dataset's own label definition, which travels
    on the model rather than being restated here.
    """
    if len(probabilities) != len(outcomes):
        raise ValueError(f"{len(probabilities)} probabilities against {len(outcomes)} outcomes")
    n = len(outcomes)
    if n == 0:
        return {"samples": 0, "note": "nothing to score"}

    predicted = [1 if p >= threshold else 0 for p in probabilities]
    tp = sum(1 for p, o in zip(predicted, outcomes, strict=True) if p == 1 and o == 1)
    fp = sum(1 for p, o in zip(predicted, outcomes, strict=True) if p == 1 and o == 0)
    fn = sum(1 for p, o in zip(predicted, outcomes, strict=True) if p == 0 and o == 1)
    tn = sum(1 for p, o in zip(predicted, outcomes, strict=True) if p == 0 and o == 0)

    precision = _safe_divide(tp, tp + fp)
    recall = _safe_divide(tp, tp + fn)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and (precision + recall) > 0
        else None
    )

    positives = sum(outcomes)
    majority = max(positives, n - positives) / n

    # Clamped before the log so a confident wrong answer is a large penalty
    # rather than an infinity that makes the whole metric unreadable.
    epsilon = 1e-12
    log_loss = (
        -sum(
            o * math.log(min(max(p, epsilon), 1 - epsilon))
            + (1 - o) * math.log(1 - min(max(p, epsilon), 1 - epsilon))
            for p, o in zip(probabilities, outcomes, strict=True)
        )
        / n
    )

    return {
        "samples": n,
        "accuracy": round((tp + tn) / n, 6),
        # Reported BESIDE accuracy, always. A set that is 70% WIN gives 70%
        # accuracy to a model that always says WIN, and the comparison is the
        # only thing that makes the first number mean anything.
        "majority_share": round(majority, 6),
        "beats_majority": round((tp + tn) / n, 6) > round(majority, 6),
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "f1": round(f1, 6) if f1 is not None else None,
        "log_loss": round(log_loss, 6),
        "brier": round(brier_score(probabilities, outcomes), 6),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "positive_rate": round(positives / n, 6),
        "threshold": threshold,
        "warning": (
            None
            if n >= MIN_SAMPLE
            else f"{n} rows is below the {MIN_SAMPLE}-row floor; every figure here has a "
            "confidence interval wider than the differences anyone would read from it"
        ),
        "note": "these are ML metrics. They are not trading performance.",
    }


def calibration(
    probabilities: list[float], outcomes: list[int], *, bins: int = 10
) -> dict[str, Any]:
    """Section 23, from L29's implementations rather than a second copy.

    `is_calibrated` is deliberately absent. Calibration is a curve and a number,
    not a verdict, and a boolean here would be quoted as one.
    """
    if not probabilities:
        return {"samples": 0, "note": "nothing to calibrate"}
    return {
        "samples": len(probabilities),
        "brier": round(brier_score(probabilities, outcomes), 6),
        "expected_calibration_error": round(
            expected_calibration_error(probabilities, outcomes, bins=bins), 6
        ),
        "reliability": reliability_bins(probabilities, outcomes, bins=bins),
        "note": (
            "a probability is calibrated when 0.80 corresponds to an 80% observed "
            "frequency over a sample large enough to say so. This reports the "
            "measurement; it does not certify one."
        ),
        "warning": (
            None
            if len(probabilities) >= MIN_SAMPLE * 5
            else "calibration measured on a small sample is a number with a confidence "
            "interval wider than the thing it describes"
        ),
    }


def economic(returns: list[float]) -> dict[str, Any]:
    """What acting on the model would have cost, in the label's own units.

    Separate from `classification()` and never merged with it. The returns come
    from the dataset's `forward_return` label, which is already net of the
    spread the label set charged -- so this is not a backtest and does not claim
    to be one: no sizing, no slippage beyond that spread, no financing, and
    every trade weighted equally.
    """
    n = len(returns)
    if n == 0:
        return {"trades": 0, "note": "no trades were selected"}

    # L32. The definitions come from `app.analytics.metrics`, the one
    # implementation. The output keys and the label-return unit are unchanged:
    # this is an ordering over a label set, not a P&L, and it still says so.
    gross_win = _metrics.gross_profit(returns)
    gross_loss = _metrics.gross_loss(returns)

    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for r in returns:
        equity += r
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)

    return {
        "trades": n,
        "expected_value": round(sum(returns) / n, 8),
        "win_rate": round(float(_metrics.win_rate(list(returns))), 6),
        "profit_factor": (
            round(float(_metrics.profit_factor(list(returns))), 6)
            if isinstance(_metrics.profit_factor(list(returns)), float)
            else None
        ),
        "gross_profit": round(gross_win, 8),
        "gross_loss": round(gross_loss, 8),
        "max_drawdown": round(drawdown, 8),
        "note": (
            "computed from the label's forward return, which is already net of the "
            "spread the label set charged. This is NOT a backtest: no position sizing, "
            "no financing, no per-trade slippage beyond that spread, and every trade "
            "weighted equally. Read it as an ordering, not as a P&L."
        ),
        "warning": (
            None if n >= MIN_SAMPLE else f"{n} trades is below the {MIN_SAMPLE}-trade floor"
        ),
    }


def compare(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Section 24. The difference, and whether it is worth anything.

    `meaningfully_better` is deliberately strict: a candidate must beat the
    baseline on log loss AND on accuracy-over-majority. This project's own
    record is a long list of candidates that beat something on one reading and
    nothing out of sample, so a comparison that could be satisfied by one metric
    would be satisfied constantly.
    """

    def delta(key: str) -> float | None:
        a, b = candidate.get(key), baseline.get(key)
        if a is None or b is None:
            return None
        return round(a - b, 8)

    log_loss_delta = delta("log_loss")
    accuracy_delta = delta("accuracy")
    better = (
        log_loss_delta is not None
        and accuracy_delta is not None
        and log_loss_delta < 0
        and accuracy_delta > 0
        and bool(candidate.get("beats_majority"))
    )
    return {
        "accuracy_delta": accuracy_delta,
        "log_loss_delta": log_loss_delta,
        "brier_delta": delta("brier"),
        "f1_delta": delta("f1"),
        "candidate_beats_majority": bool(candidate.get("beats_majority")),
        "meaningfully_better": better,
        "rule": (
            "better means LOWER log loss AND higher accuracy AND above the majority "
            "share. One metric moving is not a result: this repository's record is a "
            "long list of candidates that beat something on one reading and nothing "
            "out of sample."
        ),
        "caveat": (
            "this is a comparison on a held-out segment of one dataset. It is not the "
            "L26 gate: no permutation null, no correction for the number of candidates "
            "tried, and no walk-forward. A model is not validated until it has cleared "
            "those."
        ),
    }
