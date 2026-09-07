"""Run the checks and assemble a report.

**The `evaluate` half is pure**: inputs in, findings out, no session and no
clock. That is what lets every check be tested without a database, and it is
why L29 could add four checks here without touching the collector.

**The `run` half persists**, and it kept the sentence it was written with:
*"There is no model in this platform yet"* was true when this module was
written, and L27 and L28 made it false. It now resolves the deployed version
from L28's registry rather than guessing at a status, and `app/monitoring/
service.py` is what feeds it recorded data.

Nothing here invents a prediction, a feature or an outcome. An absent input
skips its check, and the check reports INSUFFICIENT_DATA -- which is a different
claim from "fine", and the only one the evidence supports.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.lifecycle import SERVING_STATUSES
from app.auth.models import utcnow
from app.models.ai import ModelVersion
from app.models.ops import Notification
from app.monitoring import checks as C
from app.monitoring.config import DEFAULT_THRESHOLDS
from app.monitoring.escalation import build_alerts
from app.monitoring.findings import Alert, Finding, MonitoringReport, Severity
from app.monitoring.stats import MIN_SAMPLE
from app.notifications.contract import Category
from app.notifications.contract import Severity as NotificationSeverity

log = logging.getLogger("app.monitoring")

# L34 gave the platform ONE severity vocabulary
# (`app.notifications.contract.Severity`) and this maps L29's five check
# severities onto it, as `alerts.NOTIFICATION_SEVERITY` maps them onto the
# three words a `model_alerts` row carries. Read from the enum rather than
# spelled out, so a word the code can produce and the schema rejects cannot
# exist.
SEVERITY_TO_NOTIFICATION = {
    Severity.warning: str(NotificationSeverity.warning),
    Severity.serious: str(NotificationSeverity.error),
    Severity.critical: str(NotificationSeverity.critical),
}


@dataclass
class MonitoringInputs:
    """Everything a run needs. Absent fields skip their check rather than
    being filled with a default, because a default would be a fabrication."""

    reference_features: Mapping[str, Sequence[float]] = field(default_factory=dict)
    current_features: Mapping[str, Sequence[float]] = field(default_factory=dict)
    reference_predictions: Sequence[float] | None = None
    current_predictions: Sequence[float] | None = None
    probabilities: Sequence[float] | None = None
    outcomes: Sequence[int] | None = None
    reference_r: Sequence[float] | None = None
    current_r: Sequence[float] | None = None
    reference_regimes: Sequence[str] | None = None
    current_regimes: Sequence[str] | None = None
    # L29. The model's SERVICE rather than the model: how fast it answered, how
    # often it failed, and whether its confidence changed shape.
    reference_confidence: Sequence[float] | None = None
    current_confidence: Sequence[float] | None = None
    statuses: Sequence[str] | None = None
    latencies_ms: Sequence[float] | None = None
    min_sample: int = MIN_SAMPLE
    #: Section 41. None means the defaults; a caller that wants different bars
    #: passes them rather than editing a constant.
    thresholds: Any = None


class ModelMonitor:
    def __init__(self, db: AsyncSession | None = None) -> None:
        self.db = db

    def evaluate(self, inputs: MonitoringInputs) -> list[Finding]:
        findings: list[Finding] = []

        if inputs.reference_features or inputs.current_features:
            findings.extend(
                C.feature_drift(
                    inputs.reference_features,
                    inputs.current_features,
                    min_sample=inputs.min_sample,
                )
            )
        if inputs.reference_predictions is not None and inputs.current_predictions is not None:
            findings.append(
                C.prediction_drift(
                    inputs.reference_predictions,
                    inputs.current_predictions,
                    min_sample=inputs.min_sample,
                )
            )
        if inputs.current_predictions is not None:
            findings.append(
                C.prediction_distribution(inputs.current_predictions, min_sample=inputs.min_sample)
            )
        if inputs.probabilities is not None and inputs.outcomes is not None:
            findings.append(
                C.calibration(inputs.probabilities, inputs.outcomes, min_sample=inputs.min_sample)
            )
        if inputs.reference_r is not None and inputs.current_r is not None:
            findings.append(
                C.performance(inputs.reference_r, inputs.current_r, min_sample=inputs.min_sample)
            )
        if inputs.current_r is not None:
            findings.append(C.trade_outcomes(inputs.current_r))
        if inputs.reference_regimes is not None and inputs.current_regimes is not None:
            findings.append(
                C.market_regime(
                    inputs.reference_regimes,
                    inputs.current_regimes,
                    min_sample=inputs.min_sample,
                )
            )

        # --- L29: the model's service.
        bars = inputs.thresholds or DEFAULT_THRESHOLDS
        if inputs.reference_confidence is not None and inputs.current_confidence is not None:
            findings.append(
                C.confidence_drift(
                    inputs.reference_confidence,
                    inputs.current_confidence,
                    min_sample=inputs.min_sample,
                )
            )
        if inputs.latencies_ms is not None:
            findings.append(
                C.latency(
                    inputs.latencies_ms,
                    p95_warning_ms=bars.latency_p95_warning_ms,
                    p95_critical_ms=bars.latency_p95_critical_ms,
                )
            )
        if inputs.statuses is not None:
            findings.append(
                C.availability(
                    inputs.statuses,
                    error_rate_warning=bars.error_rate_warning,
                    error_rate_critical=bars.error_rate_critical,
                )
            )

        # Last, because it reads the others. §15: concept drift is INFERRED
        # from the pattern "outcomes moved, inputs did not" and never claimed
        # from input drift alone.
        if findings:
            findings.append(C.possible_concept_drift(findings))
        return findings

    async def _persist(self, alerts: list[Alert], model_version_id: str | None) -> None:
        if self.db is None:
            return
        for alert in alerts:
            self.db.add(
                Notification(
                    channel="inapp",
                    event_type="model_monitoring",
                    severity=SEVERITY_TO_NOTIFICATION.get(
                        alert.severity, str(NotificationSeverity.info)
                    ),
                    category=str(Category.monitoring),
                    title=alert.title[:200],
                    body=alert.body,
                    payload={
                        "model_version_id": model_version_id,
                        "action": str(alert.action),
                        "checks": sorted({str(f.check) for f in alert.findings}),
                        "findings": [
                            {
                                "check": str(f.check),
                                "subject": f.subject,
                                "severity": str(f.severity),
                                "summary": f.summary,
                                "statistic": f.statistic,
                                "p_value": f.p_value,
                                "survives_fdr": f.survives_fdr,
                            }
                            for f in alert.findings
                        ],
                    },
                    status="pending",
                )
            )
        await self.db.flush()

    async def run(
        self,
        inputs: MonitoringInputs,
        model_version_id: str | None = None,
        at: datetime | None = None,
    ) -> MonitoringReport:
        at = at or utcnow()

        if self.db is not None and model_version_id is None:
            # L28's registry, not a status guess. `serves_inference` is the
            # registry's own answer to "may this version answer a prediction",
            # and a second opinion here could disagree with it.
            deployed = await self.db.scalar(
                select(ModelVersion).where(
                    ModelVersion.status.in_(sorted(str(s) for s in SERVING_STATUSES))
                )
            )
            if deployed is None:
                # Nothing is serving. Say so; do not invent a subject.
                return MonitoringReport(
                    model_version_id=None,
                    generated_at=at,
                    findings=(),
                    alerts=(),
                    note=(
                        "no model version is in a status that serves inference, so "
                        "there is nothing to monitor. This is a statement about the "
                        "registry, not a clean bill of health."
                    ),
                )
            model_version_id = deployed.id

        findings = self.evaluate(inputs)
        alerts = build_alerts(findings, at)
        await self._persist(alerts, model_version_id)

        for alert in alerts:
            log.warning(
                "model monitoring alert",
                extra={
                    "event": "model_monitoring_alert",
                    "severity": str(alert.severity),
                    "action": str(alert.action),
                    "model_version_id": model_version_id,
                },
            )
        return MonitoringReport(
            model_version_id=model_version_id,
            generated_at=at,
            findings=tuple(findings),
            alerts=tuple(alerts),
            note=None if findings else "no inputs were supplied, so no check ran",
        )
