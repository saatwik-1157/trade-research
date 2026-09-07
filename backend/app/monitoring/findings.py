"""What a monitoring check produces, and the escalation it justifies.

The ladder the brief asks for:

    WARNING -> PAUSE/REDUCE STRATEGY -> REVIEW -> RETRAIN -> VALIDATE

It is a ladder of *recommendations*. Nothing in this package promotes,
deploys or replaces a model, and `FORBIDDEN_ACTIONS` records that as a rule
the tests enforce rather than a convention the code happens to follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Severity(StrEnum):
    ok = "ok"
    insufficient_data = "insufficient_data"
    warning = "warning"
    serious = "serious"
    critical = "critical"


ORDERED_SEVERITY: tuple[Severity, ...] = (
    Severity.insufficient_data,
    Severity.ok,
    Severity.warning,
    Severity.serious,
    Severity.critical,
)


def worst(severities: list[Severity]) -> Severity:
    if not severities:
        return Severity.insufficient_data
    return max(severities, key=lambda s: ORDERED_SEVERITY.index(s))


class Action(StrEnum):
    none = "none"
    warn = "warn"
    reduce_exposure = "reduce_exposure"
    pause_strategy = "pause_strategy"
    review = "review"
    retrain = "retrain"
    validate = "validate"


# Actions this package must never emit. Replacing what is serving live
# traffic is a human decision made through the model registry, and a monitor
# that could take it would be able to swap a model on the strength of one
# noisy window.
FORBIDDEN_ACTIONS: frozenset[str] = frozenset(
    {"promote", "deploy", "replace_production_model", "rollback"}
)


class Check(StrEnum):
    feature_drift = "feature_drift"
    prediction_drift = "prediction_drift"
    prediction_distribution = "prediction_distribution"
    feature_distribution = "feature_distribution"
    performance = "performance"
    calibration = "calibration"
    trade_outcomes = "trade_outcomes"
    market_regime = "market_regime"
    # L29. The four that measure the model's SERVICE rather than the model:
    # how fast it answered, how often it failed, whether its confidence changed
    # shape, and whether performance moved while the inputs did not.
    latency = "latency"
    availability = "availability"
    confidence_drift = "confidence_drift"
    # Never measured directly. Inferred only from the pattern §15 names.
    possible_concept_drift = "possible_concept_drift"


@dataclass(frozen=True)
class Finding:
    """One check's result on one subject."""

    check: Check
    subject: str  # feature name, model version, strategy, "portfolio"
    severity: Severity
    summary: str
    statistic: float | None = None
    p_value: float | None = None
    reference_n: int = 0
    current_n: int = 0
    survives_fdr: bool | None = None
    detail: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """Insufficient data is a reason to gather more, not to act."""
        return self.severity in (Severity.warning, Severity.serious, Severity.critical)


@dataclass(frozen=True)
class Alert:
    severity: Severity
    action: Action
    title: str
    body: str
    raised_at: datetime
    findings: tuple[Finding, ...] = ()

    def __post_init__(self) -> None:
        if str(self.action) in FORBIDDEN_ACTIONS:  # pragma: no cover - guarded by type
            raise ValueError(f"{self.action} is not an action monitoring may recommend")


@dataclass(frozen=True)
class MonitoringReport:
    model_version_id: str | None
    generated_at: datetime
    findings: tuple[Finding, ...]
    alerts: tuple[Alert, ...]
    note: str | None = None

    @property
    def severity(self) -> Severity:
        return worst([f.severity for f in self.findings])

    @property
    def actionable_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.actionable)
