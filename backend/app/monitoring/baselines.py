"""What a current window is compared against, and where that reference came from.

Sections 5 and 40. A drift score means nothing without the answer to *"drifted
from what"*, so a baseline is a first-class object with a kind, an identity and
a period — and every snapshot records the one it used.

**A baseline is never changed silently.** §40's rule, and the reason it matters
is that changing it after an alert makes the alert go away. So a `Baseline`
carries the id of the thing it came from, and a snapshot stores that id: two
snapshots computed against different references are visibly different rather
than quietly incomparable.

**The strongest available baseline wins, and the choice is recorded.**
`resolve()` prefers the validation report — those figures were measured on the
held-out segment L26 judged, so a comparison against them is like for like —
and falls back to the training metrics, then to a previous production window.
Each fallback is named in `reason`, because a weaker reference silently
substituted is a comparison nobody can read.

**A baseline that does not exist is not a baseline of zeros.** `resolve()`
returns `None` and the checks that need one report INSUFFICIENT_DATA. Section
52: no fabricated reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import ModelVersion
from app.models.validation import ValidationRun
from app.monitoring.config import BaselineKind


@dataclass(frozen=True)
class Baseline:
    """One explicit reference. Section 5."""

    kind: BaselineKind
    #: What this came from: a validation run id, a model version id, a window.
    baseline_id: str
    #: The model version the baseline describes. Not always the version being
    #: monitored -- a PREVIOUS_MODEL baseline describes a different one, and
    #: conflating them is how a comparison ends up against itself.
    model_version_id: str
    period: tuple[datetime | None, datetime | None] = (None, None)
    #: The figures the current window is compared against. Only what was
    #: actually recorded; absent keys are absent, never zero.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Per-feature reference distributions, when the baseline has them.
    feature_samples: dict[str, list[float]] = field(default_factory=dict)
    prediction_samples: list[float] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        start, end = self.period
        return {
            "kind": str(self.kind),
            "baseline_id": self.baseline_id,
            "model_version_id": self.model_version_id,
            "period": [start.isoformat() if start else None, end.isoformat() if end else None],
            "metrics": self.metrics,
            "features_with_reference": sorted(self.feature_samples),
            "prediction_samples": len(self.prediction_samples),
            "reason": self.reason,
            "rule": (
                "a baseline is never changed silently. Two snapshots computed against "
                "different references are visibly different rather than quietly "
                "incomparable, because the snapshot records which one it used."
            ),
        }


def _from_validation(run: ValidationRun, version: ModelVersion) -> Baseline:
    """The validation report as a baseline. The strongest one available.

    L26 measured these figures on the final test segment — the one training
    never saw — so a current window compared against them is being compared
    against a held-out measurement rather than against an in-sample one.
    """
    report = run.report or {}
    context = report.get("context") or {}
    scored = context.get("scored") or {}
    economic = context.get("economic") or {}

    metrics: dict[str, Any] = {}
    for check in report.get("checks") or []:
        evidence = check.get("evidence") or {}
        if check["check"] == "discrimination" and "auc" in evidence:
            metrics["auc"] = evidence["auc"]
        elif check["check"] == "calibration" and "expected_calibration_error" in evidence:
            metrics["expected_calibration_error"] = evidence["expected_calibration_error"]
        elif check["check"] == "economic":
            for name in ("profit_factor", "trades"):
                if evidence.get(name) is not None:
                    metrics[name] = evidence[name]
    if scored.get("rows"):
        metrics["samples"] = scored["rows"]
    if economic.get("win_rate") is not None:
        metrics["win_rate"] = economic["win_rate"]

    return Baseline(
        kind=BaselineKind.validation,
        baseline_id=run.id,
        model_version_id=version.id,
        period=(version.test_start, version.test_end),
        metrics=metrics,
        reason=(
            f"validation run {run.id[:12]} returned {run.verdict}. Its figures were "
            "measured on the final test segment, which training never saw, so a current "
            "window compared against them is compared against a held-out measurement."
        ),
    )


def _from_training(version: ModelVersion) -> Baseline | None:
    """The training record as a baseline. Weaker, and it says so.

    These figures came from the same run that fitted the model. They are a real
    measurement and they are not a held-out one, which is exactly the difference
    §5 asks a baseline to state about itself.
    """
    metrics = (version.metrics or {}).get("classification") or {}
    if not metrics:
        return None
    kept = {
        name: metrics[name]
        for name in ("samples", "accuracy", "log_loss", "brier", "majority_share")
        if metrics.get(name) is not None
    }
    if not kept:
        return None
    return Baseline(
        kind=BaselineKind.training,
        baseline_id=version.id,
        model_version_id=version.id,
        period=(version.test_start, version.test_end),
        metrics=kept,
        reason=(
            "no completed validation run carries a report, so the training record is "
            "used. Weaker: these figures came from the run that fitted the model, and "
            "the comparison should be read as such."
        ),
    )


async def resolve(
    db: AsyncSession,
    version: ModelVersion,
    *,
    prefer: BaselineKind | None = None,
) -> Baseline | None:
    """The strongest baseline available for this version, or None.

    None means no reference exists — which the checks report as
    INSUFFICIENT_DATA. §52: a baseline that does not exist is not a baseline of
    zeros, and a drift score against a fabricated reference is a fabricated
    drift score.
    """
    if prefer in (None, BaselineKind.validation):
        run = None
        if version.validation_run_id:
            run = await db.get(ValidationRun, version.validation_run_id)
        if run is None:
            run = await db.scalar(
                select(ValidationRun)
                .where(
                    ValidationRun.model_version_id == version.id,
                    ValidationRun.status == "completed",
                )
                .order_by(ValidationRun.created_at.desc())
                .limit(1)
            )
        if run is not None and run.report:
            return _from_validation(run, version)
        if prefer is BaselineKind.validation:
            return None

    if prefer in (None, BaselineKind.training):
        return _from_training(version)

    return None


def compare(baseline: Baseline, current: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """Current against baseline, per named metric. Section 28.

    A metric absent on either side is reported as absent rather than as a
    difference of zero: an unmeasured figure and an unchanged one are different
    facts, and only one of them is reassuring.
    """
    out: dict[str, Any] = {}
    for name in names:
        a, b = baseline.metrics.get(name), current.get(name)
        if not isinstance(a, int | float) or not isinstance(b, int | float):
            out[name] = {
                "baseline": a,
                "current": b,
                "delta": None,
                "note": "not measured on both sides",
            }
            continue
        out[name] = {"baseline": a, "current": b, "delta": round(b - a, 8)}
    return out
