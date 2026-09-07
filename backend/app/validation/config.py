"""What validation asks, and where the bar is set.

**Every threshold is configurable and none is universal.** Section 25. A
minimum profit factor that is right for EURUSD H1 is wrong for a daily index,
and a hardcoded one would be a number nobody chose applied to instruments
nobody checked. The defaults here are stated with their reasoning and are meant
to be argued with.

**The verdict is not a score.** Section 27. Each check returns its own
severity, and the overall verdict is derived from them by a rule that cannot
average a failure away. A composite number is offered as *supplementary*
information and is never the answer.

**BLOCKED outranks FAIL.** Section 26 draws the distinction and it is the one
most easily lost: FAIL means the candidate is not good enough, BLOCKED means we
could not tell. Reporting "not good enough" when the truth is "the inputs were
unusable" is a claim the evidence does not support, so the precedence is
BLOCKED > FAIL > CONDITIONAL > PASS.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# Bumped when a check's arithmetic or the verdict rule changes. A report stores
# the version it was produced under, so two reports are never compared across a
# change in what the words mean.
VALIDATION_ENGINE_VERSION = "1.0.0"

MAX_CONCURRENT = 2
MAX_QUEUED_PER_USER = 3


class ValidationError(Exception):
    """A run that cannot proceed. Refused rather than reported optimistically."""


class Severity(StrEnum):
    """One check's answer."""

    passed = "PASS"
    warning = "WARNING"
    failed = "FAIL"
    # The check could not be evaluated. Never treated as a pass, and never
    # treated as a failure either -- both would be claims about the model.
    blocked = "BLOCKED"


class Verdict(StrEnum):
    """The overall answer. Section 26."""

    passed = "PASS"
    failed = "FAIL"
    conditional = "CONDITIONAL"
    blocked = "BLOCKED"


# Precedence, most severe first. A single BLOCKED check blocks the report,
# because a report containing an unevaluable check cannot honestly say the
# candidate passed or failed.
_ORDER: tuple[Verdict, ...] = (
    Verdict.blocked,
    Verdict.failed,
    Verdict.conditional,
    Verdict.passed,
)


def verdict_from(severities: list[Severity]) -> Verdict:
    """The overall verdict, by rule rather than by arithmetic.

    Deliberately not a weighted score. Section 27's whole point is that a
    composite must not hide an important failure, and any weighting scheme can
    be tuned until it does.
    """
    if not severities:
        return Verdict.blocked
    if Severity.blocked in severities:
        return Verdict.blocked
    if Severity.failed in severities:
        return Verdict.failed
    if Severity.warning in severities:
        return Verdict.conditional
    return Verdict.passed


def worst(a: Verdict, b: Verdict) -> Verdict:
    return a if _ORDER.index(a) <= _ORDER.index(b) else b


class ValidationStage(StrEnum):
    """Where a job is. Every value is a real boundary in the service."""

    queued = "queued"
    loading = "loading"
    data_integrity = "data_integrity"
    artifact = "artifact"
    leakage = "leakage"
    temporal = "temporal"
    baseline = "baseline"
    calibration = "calibration"
    economic = "economic"
    walk_forward = "walk_forward"
    robustness = "robustness"
    regime = "regime"
    significance = "significance"
    reporting = "reporting"
    done = "done"


ORDERED_STAGES: tuple[ValidationStage, ...] = tuple(ValidationStage)


def progress_of(stage: ValidationStage) -> float:
    return round(ORDERED_STAGES.index(stage) / (len(ORDERED_STAGES) - 1), 4)


@dataclass(frozen=True)
class Thresholds:
    """Section 25, and every number here is arguable on purpose.

    They are defaults, not constants: `ValidationConfig` carries them, the job
    stores them, and the report quotes the ones it used. A caller validating a
    daily index model should change them and the report will say so.
    """

    # --- sample size (§24). Below these the checks report BLOCKED rather than
    # a verdict, because a figure from 30 trades is not a small result -- it is
    # a number whose confidence interval is wider than the thing it measures.
    minimum_samples: int = 200
    minimum_trades: int = 30
    minimum_walk_forward_windows: int = 3

    # --- ML. `minimum_auc` is 0.5 by default, i.e. "better than a coin flip",
    # deliberately low: this project's own searches say the interesting failure
    # is not a weak model but a model that looks strong and does not replicate.
    minimum_auc: float = 0.5
    maximum_calibration_error: float = 0.10
    # A candidate must beat its baseline by this much on log loss to count as
    # an improvement at all. Zero would let noise through.
    minimum_baseline_improvement: float = 0.0

    # --- economic (§17). Reused from the project's own measured hurdle: at
    # SL=TP=1.5xATR, breakeven on this broker needs a 50.5-52.7% win rate
    # depending on the pair, so a profit factor barely over 1.0 is inside the
    # noise the spread already creates.
    minimum_profit_factor: float = 1.0
    maximum_drawdown_ratio: float = 0.5

    # --- robustness (§22). A model whose edge disappears when the decision
    # threshold moves by this much is fitted to the threshold, not the market.
    threshold_probe: float = 0.05
    cost_probe_multiplier: float = 1.5
    # Share of probes that must stay on the right side of breakeven.
    minimum_stable_share: float = 0.6

    # --- significance (§23). The permutation null is the gate this project
    # trusts; the number of permutations trades precision for time.
    permutations: int = 200
    significance_alpha: float = 0.05
    # Bonferroni across however many candidates were tried for this family.
    # 1 means "no correction", which is only honest when one was tried.
    candidates_tried: int = 1

    # --- overfitting (§19). How much worse the test segment may be than the
    # training one before it is a warning.
    maximum_train_test_gap: float = 0.10

    def as_dict(self) -> dict[str, Any]:
        return {
            "minimum_samples": self.minimum_samples,
            "minimum_trades": self.minimum_trades,
            "minimum_walk_forward_windows": self.minimum_walk_forward_windows,
            "minimum_auc": self.minimum_auc,
            "maximum_calibration_error": self.maximum_calibration_error,
            "minimum_baseline_improvement": self.minimum_baseline_improvement,
            "minimum_profit_factor": self.minimum_profit_factor,
            "maximum_drawdown_ratio": self.maximum_drawdown_ratio,
            "threshold_probe": self.threshold_probe,
            "cost_probe_multiplier": self.cost_probe_multiplier,
            "minimum_stable_share": self.minimum_stable_share,
            "permutations": self.permutations,
            "significance_alpha": self.significance_alpha,
            "candidates_tried": self.candidates_tried,
            "maximum_train_test_gap": self.maximum_train_test_gap,
            "note": (
                "defaults, not constants. A threshold right for EURUSD H1 is wrong for a "
                "daily index; the report quotes the ones it actually used."
            ),
        }


@dataclass(frozen=True)
class ValidationConfig:
    """One validation run, specified completely enough to repeat it.

    Every version is named explicitly. Section 6: there is no `latest` option
    for the model or the dataset, because a report against "whatever was
    current" cannot be checked afterwards.
    """

    model_version_id: str
    training_job_id: str | None = None
    thresholds: Thresholds = None  # type: ignore[assignment]
    # The probability above which the model is taken to be saying "trade".
    decision_threshold: float = 0.5
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.model_version_id:
            raise ValidationError(
                "a model version id is required. There is no 'latest model' option: a "
                "report against whatever was current cannot be checked afterwards."
            )
        if self.thresholds is None:
            object.__setattr__(self, "thresholds", Thresholds())
        if not 0.0 < self.decision_threshold < 1.0:
            raise ValidationError(
                f"the decision threshold must be strictly between 0 and 1, not "
                f"{self.decision_threshold}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "validation_engine_version": VALIDATION_ENGINE_VERSION,
            "model_version_id": self.model_version_id,
            "training_job_id": self.training_job_id,
            "decision_threshold": self.decision_threshold,
            "thresholds": self.thresholds.as_dict(),
            "notes": self.notes,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
