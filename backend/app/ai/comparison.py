"""Two model versions, side by side, with the reasons they may not be comparable.

Section 18 asks for a comparison and then adds the sentence that shapes this
module: *"Do not compare metrics generated from incompatible datasets without
clearly labeling the difference."*

So the comparison leads with the difference. Every figure is shown beside the
dataset fingerprint, feature set and evaluation period it came from, and when
those disagree the payload says so first — before the numbers, not in a footnote
under them. A reader who takes one number from this output and none of the
context should be unable to do so.

**No verdict.** This says what two versions measured; it does not say which is
better. That is L26's job for a single candidate and a human's for a pair — and
§28's own caution applies: a difference between two versions measured on
different data is not evidence about either of them.

**The two kinds of metric stay apart.** L25 and L26 both keep ML metrics and
economic metrics in separate dictionaries and never merge them, because a model
can score well on the first and lose money on the second — something this
repository has measured. The comparison keeps them apart for the same reason.
"""

from __future__ import annotations

from typing import Any

from app.models.ai import ModelVersion

#: Metrics where a LOWER value is better. Needed because a bare difference is
#: meaningless without it: log loss falling by 0.05 and AUC falling by 0.05 are
#: opposite events.
LOWER_IS_BETTER = frozenset({"log_loss", "brier", "expected_calibration_error"})

#: The ML figures worth putting side by side. Drawn from L25's `classification()`
#: output, so the names match what is actually stored rather than a wish list.
ML_METRICS = (
    "samples",
    "accuracy",
    "majority_share",
    "precision",
    "recall",
    "f1",
    "log_loss",
    "brier",
    "positive_rate",
)

#: The economic figures, from `tools/rule_backtest.stats` via L26.
ECONOMIC_METRICS = (
    "trades",
    "win_rate",
    "profit_factor",
    "expectancy_points_net",
    "total_points_net",
    "avg_bars_held",
)


def _provenance(row: ModelVersion) -> dict[str, Any]:
    return {
        "version": (row.artifact_ref or "").split(":", 1)[-1],
        "status": row.status,
        "dataset_version": row.dataset_version,
        "dataset_fingerprint": row.dataset_fingerprint,
        "feature_version": row.feature_version,
        "label_version": row.label_version,
        "preprocessing_version": row.preprocessing_version,
        "code_version": row.code_version,
        "training_period": _period(row.training_start, row.training_end),
        "validation_period": _period(row.validation_start, row.validation_end),
        "test_period": _period(row.test_start, row.test_end),
        "artifact_sha256": row.artifact_sha256,
    }


def _period(start: Any, end: Any) -> list[str] | None:
    if start is None or end is None:
        return None
    return [start.isoformat(), end.isoformat()]


def _differences(left: ModelVersion, right: ModelVersion) -> list[str]:
    """Everything that makes these two figures answers to different questions."""
    out: list[str] = []
    if left.dataset_fingerprint != right.dataset_fingerprint:
        out.append(
            f"different DATASETS: {(left.dataset_fingerprint or 'none')[:12]} against "
            f"{(right.dataset_fingerprint or 'none')[:12]}. Two metrics measured on "
            "different data are not a comparison of two models."
        )
    if left.feature_version != right.feature_version:
        out.append(
            f"different FEATURE SETS: {left.feature_version} against "
            f"{right.feature_version}. The models were asked about different quantities."
        )
    if left.label_version != right.label_version:
        out.append(
            f"different LABEL SETS: {left.label_version} against {right.label_version}. "
            "The models were scored against different definitions of success."
        )
    if left.preprocessing_version != right.preprocessing_version:
        out.append(
            f"different PREPROCESSING: {left.preprocessing_version} against "
            f"{right.preprocessing_version}."
        )
    if _period(left.test_start, left.test_end) != _period(right.test_start, right.test_end):
        out.append(
            "different EVALUATION PERIODS. A market that moved differently in the two "
            "windows accounts for more of most differences than a model does."
        )
    return out


def _pull(metrics: dict[str, Any] | None, group: str, names: tuple[str, ...]) -> dict[str, Any]:
    block = (metrics or {}).get(group) or {}
    return {name: block.get(name) for name in names if name in block}


def _deltas(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """b − a per metric, with the direction of improvement named.

    `better` is None when either side is missing. A missing figure is not a
    tie, and rendering it as 0.0 would make an unmeasured metric look equal.
    """
    out: dict[str, Any] = {}
    for name in sorted(set(left) | set(right)):
        a, b = left.get(name), right.get(name)
        if not isinstance(a, int | float) or not isinstance(b, int | float):
            out[name] = {
                "a": a,
                "b": b,
                "delta": None,
                "better": None,
                "note": "not measured on both",
            }
            continue
        delta = b - a
        if delta == 0:
            better = None
        elif name in LOWER_IS_BETTER:
            better = "b" if delta < 0 else "a"
        else:
            better = "b" if delta > 0 else "a"
        out[name] = {
            "a": a,
            "b": b,
            "delta": round(delta, 8),
            "better": better,
            "lower_is_better": name in LOWER_IS_BETTER,
        }
    return out


def compare(left: ModelVersion, right: ModelVersion) -> dict[str, Any]:
    """Two versions side by side. Section 18.

    `a` is `left`, `b` is `right`, and every delta is `b − a`. Stated because a
    sign read the wrong way round is the whole answer inverted.
    """
    differences = _differences(left, right)

    ml_a = _pull(left.metrics, "classification", ML_METRICS)
    ml_b = _pull(right.metrics, "classification", ML_METRICS)
    econ_a = _pull(left.metrics, "economic", ECONOMIC_METRICS)
    econ_b = _pull(right.metrics, "economic", ECONOMIC_METRICS)
    cal_a = _pull(left.metrics, "calibration", ("expected_calibration_error", "brier", "samples"))
    cal_b = _pull(right.metrics, "calibration", ("expected_calibration_error", "brier", "samples"))

    return {
        "comparable": not differences,
        # First in the payload, deliberately: a reader who takes one number and
        # none of the context should not be able to.
        "differences": differences
        or ["none found: both versions state the same dataset, feature set, labels and period"],
        "a": _provenance(left),
        "b": _provenance(right),
        "ml_metrics": {"a": ml_a, "b": ml_b, "delta": _deltas(ml_a, ml_b)},
        "economic_metrics": {"a": econ_a, "b": econ_b, "delta": _deltas(econ_a, econ_b)},
        "calibration": {"a": cal_a, "b": cal_b, "delta": _deltas(cal_a, cal_b)},
        "sample_sizes": {
            "a_ml": ml_a.get("samples"),
            "b_ml": ml_b.get("samples"),
            "a_trades": econ_a.get("trades"),
            "b_trades": econ_b.get("trades"),
            "why": (
                "shown beside every figure. A profit factor from 7 trades and one from "
                "700 are not the same claim, and the table would render them identically."
            ),
        },
        "reading": {
            "direction": "every delta is b - a.",
            "no_verdict": (
                "this says what two versions measured. It does not say which is better: "
                "that is a judgement, and a difference measured on different data is not "
                "evidence about either model."
            ),
            "separation": (
                "ML metrics and economic metrics are never merged. A model can score well "
                "on the first and lose money on the second, and this repository has "
                "measured exactly that."
            ),
        },
    }
