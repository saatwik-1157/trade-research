"""What monitoring measures against, over what window, and where the bars are.

**Every threshold is configurable and none is universal.** Section 41. The
defaults are stated with their reasoning and are meant to be argued with — and
two of them are labelled as CONVENTION rather than measurement, because this
repository has spent its whole history distinguishing a number somebody chose
from a number somebody found.

**A window is a choice, not a constant.** Section 14: different metrics need
different windows. A latency figure is meaningful over an hour; a win rate is
not meaningful over anything short enough to be interesting.

**Health is derived, never scored.** Section 21: `HealthState` comes from the
findings by a precedence rule, exactly as L26's verdict does. There is no
composite, because a composite can be tuned until it hides the check that
mattered — and `INSUFFICIENT_DATA` outranks `HEALTHY` for the reason §43 gives:
too little data must never become a false healthy state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

MONITORING_ENGINE_VERSION = "1.0.0"


class HealthState(StrEnum):
    """How a model version is behaving. Section 21.

    Six values, and the two that are not about the model matter most:
    `insufficient_data` says we have not measured enough to say anything, and
    `offline` says the model is not serving at all. Both are commonly reported
    as `healthy` by monitoring systems, and both mean the opposite.
    """

    healthy = "HEALTHY"
    warning = "WARNING"
    degraded = "DEGRADED"
    critical = "CRITICAL"
    # Measured, and the sample cannot support a conclusion. NEVER a pass.
    insufficient_data = "INSUFFICIENT_DATA"
    # Nothing is deployed, or the deployment cannot serve. A statement about
    # the registry, not a clean bill of health.
    offline = "OFFLINE"


#: Precedence, most severe first. `offline` leads because a model that is not
#: serving cannot be healthy, degraded or anything else — every other reading
#: would be about a model nobody is using.
_ORDER: tuple[HealthState, ...] = (
    HealthState.offline,
    HealthState.critical,
    HealthState.degraded,
    HealthState.warning,
    HealthState.insufficient_data,
    HealthState.healthy,
)


def worst_health(states: list[HealthState]) -> HealthState:
    """The overall state, by precedence rather than by arithmetic.

    `insufficient_data` sits ABOVE `healthy` deliberately: a run in which half
    the checks could not be evaluated is not a healthy run, and §43 is explicit
    that too little data must not become a false healthy state.
    """
    if not states:
        return HealthState.insufficient_data
    return min(states, key=_ORDER.index)


class BaselineKind(StrEnum):
    """What a current window is being compared against. Sections 5 and 40.

    Named rather than implied, because §5's rule is that the baseline must be
    explicitly identified and never silently changed. A drift score means
    nothing without the answer to "drifted from what".
    """

    # The distribution the model was FITTED on. The strongest reference, and
    # the only one that answers "is this the world the model learned".
    training = "TRAINING"
    # The held-out segment L26 measured on. What the validation report's
    # figures were computed over, so a performance comparison against it is
    # like for like.
    validation = "VALIDATION"
    # A recorded stretch of paper trading that somebody nominated as normal.
    paper = "PAPER"
    # A stretch of production inference nominated as normal.
    production_reference = "PRODUCTION_REFERENCE"
    # The window immediately before this one. Sensitive to gradual change and
    # blind to it: a slow drift never looks different from its own yesterday.
    previous_period = "PREVIOUS_PERIOD"
    # A different model version over the same window.
    previous_model = "PREVIOUS_MODEL"


class DriftKind(StrEnum):
    """What a drift reading is about. Section 15, and it is the level's caution.

    §15 asks that data drift, feature drift, prediction drift, performance drift
    and possible concept drift be kept apart — and that concept drift never be
    CLAIMED when only input drift was measured. So the vocabulary makes the
    weaker claim spellable and the stronger one require different evidence.
    """

    feature = "FEATURE_DRIFT"
    prediction = "PREDICTION_DRIFT"
    confidence = "CONFIDENCE_DRIFT"
    calibration = "CALIBRATION_DRIFT"
    performance = "PERFORMANCE_DRIFT"
    # Never measured directly. Inferred ONLY when performance or calibration
    # moved while the inputs did not — which is the signature of the
    # relationship changing rather than the world changing.
    possible_concept = "POSSIBLE_CONCEPT_DRIFT"


@dataclass(frozen=True)
class Windows:
    """The rolling windows a run uses. Section 14, and none is hardcoded.

    Different metrics get different windows because they need different
    amounts of evidence, not because of a preference: an inference-health
    figure is meaningful over an hour and a win rate is not meaningful over
    anything short enough to be interesting.
    """

    #: Latency, error rate, availability. High-frequency, cheap to compute.
    health: timedelta = timedelta(hours=1)
    #: Feature and prediction distributions. Long enough for a PSI to mean
    #: something at the sample floor below.
    drift: timedelta = timedelta(days=7)
    #: Calibration and performance, which need OUTCOMES and therefore time.
    performance: timedelta = timedelta(days=30)

    def as_dict(self) -> dict[str, Any]:
        return {
            "health_hours": self.health.total_seconds() / 3600,
            "drift_days": self.drift.days,
            "performance_days": self.performance.days,
            "why": (
                "different metrics need different amounts of evidence. A latency figure "
                "is meaningful over an hour; a win rate is not meaningful over anything "
                "short enough to be interesting."
            ),
        }


@dataclass(frozen=True)
class Thresholds:
    """Section 41, and every number here is arguable on purpose."""

    # --- sample size (§13, §43). Below these a check reports
    # INSUFFICIENT_DATA rather than a verdict. Inherited from
    # `app/monitoring/stats.MIN_SAMPLE`, which took it from the research
    # tooling and the live record: under about 100 observations a win rate or
    # a distribution statistic is dominated by luck.
    minimum_samples: int = 100
    #: Outcomes are scarcer than predictions, so calibration and performance
    #: get their own floor rather than sharing one that would never be met.
    minimum_outcomes: int = 50
    minimum_trades: int = 30

    # --- drift (§6, §41). PSI bands are CONVENTION and are labelled as such
    # in every payload: no study in this repository established them.
    psi_warning: float = 0.10
    psi_critical: float = 0.25
    #: Benjamini-Hochberg q. Monitoring 22 features is 22 hypotheses, and at
    #: that count one "significant" feature per run is expected with nothing
    #: happening at all.
    fdr_q: float = 0.05

    # --- calibration (§11). Measured against the validation baseline rather
    # than an absolute bar, because L26 already decided what "calibrated
    # enough" meant for this candidate.
    calibration_degradation_warning: float = 0.05
    calibration_degradation_critical: float = 0.10

    # --- performance (§12, §28). A DIFFERENCE from the baseline, not a level:
    # an AUC of 0.55 is fine for a model validated at 0.56 and alarming for one
    # validated at 0.72.
    auc_drop_warning: float = 0.05
    auc_drop_critical: float = 0.10

    # --- latency (§19). The AI layer's own budget is per-signal and L27
    # enforces it; these are about the DISTRIBUTION over a window.
    latency_p95_warning_ms: float = 500.0
    latency_p95_critical_ms: float = 2000.0

    # --- availability (§20). An error rate over the health window.
    error_rate_warning: float = 0.05
    error_rate_critical: float = 0.20

    # --- alerting (§24). How long an identical alert stays suppressed.
    cooldown_minutes: int = 60

    def as_dict(self) -> dict[str, Any]:
        return {
            "minimum_samples": self.minimum_samples,
            "minimum_outcomes": self.minimum_outcomes,
            "minimum_trades": self.minimum_trades,
            "psi_warning": self.psi_warning,
            "psi_critical": self.psi_critical,
            "fdr_q": self.fdr_q,
            "calibration_degradation_warning": self.calibration_degradation_warning,
            "calibration_degradation_critical": self.calibration_degradation_critical,
            "auc_drop_warning": self.auc_drop_warning,
            "auc_drop_critical": self.auc_drop_critical,
            "latency_p95_warning_ms": self.latency_p95_warning_ms,
            "latency_p95_critical_ms": self.latency_p95_critical_ms,
            "error_rate_warning": self.error_rate_warning,
            "error_rate_critical": self.error_rate_critical,
            "cooldown_minutes": self.cooldown_minutes,
            "psi_bands": (
                "industry CONVENTION, not measured here. No study in this repository "
                "established 0.10 and 0.25; they are reported as thresholds that were "
                "chosen rather than found."
            ),
            "performance_bars_are_differences": (
                "a DROP from the baseline, not an absolute level. An AUC of 0.55 is fine "
                "for a model validated at 0.56 and alarming for one validated at 0.72."
            ),
        }


DEFAULT_THRESHOLDS = Thresholds()
DEFAULT_WINDOWS = Windows()


def describe() -> dict[str, Any]:
    """The engine's own contract, for the API and the docs to render."""
    return {
        "monitoring_engine_version": MONITORING_ENGINE_VERSION,
        "health_states": {
            str(HealthState.healthy): "measured, and within every configured bar",
            str(HealthState.warning): "one or more readings crossed a warning bar",
            str(HealthState.degraded): "a model-implicating reading is seriously off",
            str(HealthState.critical): "a critical bar was crossed",
            str(HealthState.insufficient_data): (
                "measured, and the sample cannot support a conclusion. NEVER a pass: "
                "too little data must not become a false healthy state."
            ),
            str(HealthState.offline): (
                "nothing is deployed for this scope, or the deployment cannot serve. A "
                "statement about the registry, not a clean bill of health."
            ),
        },
        "health_precedence": [str(s) for s in _ORDER],
        "baselines": {
            str(BaselineKind.training): "the distribution the model was fitted on",
            str(BaselineKind.validation): "the held-out segment L26 measured on",
            str(BaselineKind.paper): "a stretch of paper trading nominated as normal",
            str(BaselineKind.production_reference): "a stretch of production inference",
            str(BaselineKind.previous_period): (
                "the window before this one. Sensitive to sudden change and blind to "
                "gradual: a slow drift never looks different from its own yesterday."
            ),
            str(BaselineKind.previous_model): "a different version over the same window",
        },
        "drift_kinds": {
            str(DriftKind.feature): "the inputs moved",
            str(DriftKind.prediction): "the outputs moved",
            str(DriftKind.confidence): "the confidence distribution moved",
            str(DriftKind.calibration): "predicted probabilities match outcomes less well",
            str(DriftKind.performance): "the model scores worse than its baseline",
            str(DriftKind.possible_concept): (
                "performance or calibration moved while the INPUTS did not. Inferred, "
                "never measured directly -- §15 forbids claiming concept drift from "
                "input drift alone."
            ),
        },
        "windows": DEFAULT_WINDOWS.as_dict(),
        "thresholds": DEFAULT_THRESHOLDS.as_dict(),
        "does_not": [
            "retrain, promote, replace, deploy or roll back a model",
            "modify a strategy, a risk limit or a position size",
            "enable live trading",
            "place, modify or cancel an order",
            "convert insufficient data into a healthy state",
            "claim concept drift from input drift alone",
        ],
        "statistical_caution": (
            "a distribution change is not a broken model. A market regime changed and "
            "the feature distribution changed with it is the expected case, and the "
            "wording throughout is 'distribution change detected; investigation "
            "recommended' rather than 'model failure'."
        ),
    }
