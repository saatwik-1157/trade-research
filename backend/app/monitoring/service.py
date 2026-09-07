"""One monitoring run: collect, check, derive health, snapshot, alert.

The database-facing half of L29. `monitor.evaluate` stays pure and `collect`
stays free of arithmetic; this joins them and writes the two rows.

**Nothing here acts on what it finds.** Section 26 is the strict rule of the
level, and it is structural: this module imports no training service, no
registry lifecycle, no risk engine, no sizer, no OMS and no broker adapter, and
writes no model status. A test parses every module in the package. The strongest
thing a run produces is a row and an event.

**A run that cannot measure says so.** Section 43. No deployment means OFFLINE;
no baseline means the comparison checks report INSUFFICIENT_DATA; too few
observations means the same. None of those becomes HEALTHY, and a CHECK
constraint stops a healthy snapshot existing on zero samples.

**Alerts are a state machine.** Sections 24 and 25. A condition already open and
inside its cooldown is suppressed; one that has cleared is resolved and
announced. A monitor that repeated itself every run would be muted within a
week, at which point it detects nothing.

**Never in the execution path.** Section 45. This reads rows that were already
written and is driven by a worker; no execution code calls it, so a drift
calculation has never delayed an order and cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import ModelVersion
from app.models.ai_registry import ModelDeployment
from app.models.monitoring import ModelAlert, ModelMonitoringSnapshot
from app.monitoring import alerts as alerting
from app.monitoring import baselines, collect, health
from app.monitoring.config import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WINDOWS,
    MONITORING_ENGINE_VERSION,
    HealthState,
    Thresholds,
    Windows,
)
from app.monitoring.findings import Finding
from app.monitoring.monitor import ModelMonitor, MonitoringInputs

log = logging.getLogger("app.monitoring")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 8)))


@dataclass
class RunResult:
    """What one run produced. Returned so a caller can report without a query."""

    snapshot: ModelMonitoringSnapshot | None
    findings: list[Finding]
    raised: list[dict[str, Any]]
    suppressed: list[dict[str, Any]]
    resolved: list[str]
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot.id if self.snapshot else None,
            "health": self.snapshot.health_state if self.snapshot else str(HealthState.offline),
            "findings": [_finding_dict(f) for f in self.findings],
            "alerts_raised": self.raised,
            "alerts_suppressed": self.suppressed,
            "alerts_resolved": self.resolved,
            "note": self.note,
            "authority": (
                "monitoring detects, records and alerts. It never retrains, promotes, "
                "replaces or deploys a model, and never modifies a strategy, a risk "
                "limit or a position size."
            ),
        }


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {
        "check": str(f.check),
        "subject": f.subject,
        "severity": str(f.severity),
        "summary": f.summary,
        "statistic": f.statistic,
        "p_value": f.p_value,
        "reference_n": f.reference_n,
        "current_n": f.current_n,
        "survives_fdr": f.survives_fdr,
        "detail": f.detail,
    }


def _block(findings: list[Finding], *names: str) -> dict[str, Any] | None:
    """One metric block, or None when nothing in it was computed.

    None rather than `{}`: an empty object reads as "measured, and nothing
    there", and a block that was never computed is a different fact.
    """
    chosen = [f for f in findings if str(f.check) in names]
    if not chosen:
        return None
    return {
        "findings": [_finding_dict(f) for f in chosen],
        "n": max((f.current_n for f in chosen), default=0),
        "severity": str(max(chosen, key=lambda f: str(f.severity)).severity),
    }


class MonitoringService:
    """Runs monitoring over the deployments L28's registry says are active."""

    def __init__(
        self,
        sessions: Any = None,
        hub: object | None = None,
        *,
        thresholds: Thresholds | None = None,
        windows: Windows | None = None,
    ) -> None:
        self.sessions = sessions
        self.hub = hub
        self.thresholds = thresholds or DEFAULT_THRESHOLDS
        self.windows = windows or DEFAULT_WINDOWS

    # ------------------------------------------------------------- one run

    async def run_for(
        self,
        db: AsyncSession,
        deployment: ModelDeployment,
        *,
        now: datetime | None = None,
    ) -> RunResult:
        """Monitor one deployment. Reads, measures, writes one snapshot."""
        at = _now() if now is None else now
        version = await db.get(ModelVersion, deployment.model_version_id)
        if version is None:  # pragma: no cover - the FK is RESTRICT
            return RunResult(None, [], [], [], [], note="the deployed version no longer exists")

        gathered = await collect.collect(db, deployment, config=self.windows, now=at)
        baseline = await baselines.resolve(db, version)

        inputs = MonitoringInputs(
            reference_predictions=collect.split_window(gathered.probabilities)[0] or None,
            current_predictions=collect.split_window(gathered.probabilities)[1] or None,
            reference_confidence=collect.split_window(gathered.confidences)[0] or None,
            current_confidence=collect.split_window(gathered.confidences)[1] or None,
            probabilities=gathered.outcome_probabilities or None,
            outcomes=gathered.outcomes or None,
            reference_r=collect.split_window(gathered.r_multiples)[0] or None,
            current_r=collect.split_window(gathered.r_multiples)[1] or None,
            reference_regimes=collect.split_window(gathered.regimes)[0] or None,
            current_regimes=collect.split_window(gathered.regimes)[1] or None,
            statuses=gathered.statuses or None,
            latencies_ms=gathered.latencies_ms or None,
            min_sample=self.thresholds.minimum_samples,
            thresholds=self.thresholds,
        )
        findings = ModelMonitor().evaluate(inputs)
        state = health.derive(findings, deployed=True)

        snapshot = await self._write_snapshot(
            db,
            deployment=deployment,
            gathered=gathered,
            baseline=baseline,
            findings=findings,
            state=state,
            at=at,
        )
        raised, suppressed, resolved = await self._reconcile_alerts(
            db,
            deployment=deployment,
            findings=findings,
            snapshot=snapshot,
            baseline=baseline,
            at=at,
        )
        await db.commit()

        for entry in raised:
            log.warning(
                "model monitoring alert",
                extra={
                    "event": "model_monitoring_alert",
                    "model": deployment.model_key,
                    "version": deployment.model_version_label,
                    "check": entry["check"],
                    "severity": entry["severity"],
                },
            )
        await self._publish(deployment, state, raised, resolved)
        return RunResult(snapshot, findings, raised, suppressed, resolved)

    async def run_all(
        self,
        db: AsyncSession,
        *,
        model_key: str | None = None,
        environment: str | None = None,
        now: datetime | None = None,
    ) -> list[RunResult]:
        """Every active deployment. Section 4: model-version aware throughout."""
        deployments = await collect.active_deployments(
            db, model_key=model_key, environment=environment
        )
        if not deployments:
            return [
                RunResult(
                    None,
                    [],
                    [],
                    [],
                    [],
                    note=(
                        "no model is deployed, so there is nothing to monitor. A "
                        "statement about the registry, not a clean bill of health."
                    ),
                )
            ]
        return [await self.run_for(db, d, now=now) for d in deployments]

    # ------------------------------------------------------------ snapshot

    async def _write_snapshot(
        self,
        db: AsyncSession,
        *,
        deployment: ModelDeployment,
        gathered: collect.Collected,
        baseline: baselines.Baseline | None,
        findings: list[Finding],
        state: HealthState,
        at: datetime,
    ) -> ModelMonitoringSnapshot:
        window = gathered.windows["drift"]
        sample = gathered.counts.get("drift_decisions", 0)

        # §13 and §43, and the CHECK constraint agrees: a run that measured
        # nothing cannot be HEALTHY. Corrected here rather than allowed to fail
        # at the database, so the reason is in the snapshot rather than in a log.
        if state is HealthState.healthy and sample == 0:
            state = HealthState.insufficient_data

        snapshot = ModelMonitoringSnapshot(
            model_version_id=deployment.model_version_id,
            model_key=deployment.model_key,
            model_version_label=deployment.model_version_label,
            deployment_id=deployment.id,
            strategy_key=deployment.strategy_key,
            symbol=deployment.symbol,
            timeframe=deployment.timeframe,
            environment=deployment.environment,
            baseline_kind=str(baseline.kind) if baseline else None,
            baseline_id=baseline.baseline_id if baseline else None,
            baseline_period_start=baseline.period[0] if baseline else None,
            baseline_period_end=baseline.period[1] if baseline else None,
            window_start=window.start,
            window_end=window.end,
            sample_count=sample,
            health_state=str(state),
            feature_metrics=_block(findings, "feature_drift", "feature_distribution"),
            prediction_metrics=_block(
                findings, "prediction_drift", "prediction_distribution", "confidence_drift"
            ),
            calibration_metrics=_block(findings, "calibration"),
            performance_metrics=_block(findings, "performance", "possible_concept_drift"),
            latency_metrics=_block(findings, "latency"),
            availability_metrics=_block(findings, "availability"),
            trading_metrics=_trading_block(gathered),
            regime_metrics=_block(findings, "market_regime"),
            findings=[_finding_dict(f) for f in findings],
            health_detail=health.explain(findings, deployed=True),
            thresholds=self.thresholds.as_dict(),
            monitoring_engine_version=MONITORING_ENGINE_VERSION,
            note=(
                None
                if baseline
                else (
                    "no baseline was available, so every comparison check reports "
                    "INSUFFICIENT_DATA. A drift score against a reference that does not "
                    "exist would be a fabricated drift score."
                )
            ),
        )
        db.add(snapshot)
        await db.flush()
        return snapshot

    # -------------------------------------------------------------- alerts

    async def _reconcile_alerts(
        self,
        db: AsyncSession,
        *,
        deployment: ModelDeployment,
        findings: list[Finding],
        snapshot: ModelMonitoringSnapshot,
        baseline: baselines.Baseline | None,
        at: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        scope_key = "|".join(
            [
                deployment.strategy_key or "",
                deployment.symbol or "",
                deployment.timeframe or "",
            ]
        )
        candidates = alerting.candidates_from(
            findings,
            model_version_id=deployment.model_version_id,
            model_key=deployment.model_key,
            model_version=deployment.model_version_label,
            scope_key=scope_key,
            baseline_id=baseline.baseline_id if baseline else None,
        )

        open_rows = list(
            (
                await db.scalars(
                    select(ModelAlert).where(
                        ModelAlert.model_version_id == deployment.model_version_id,
                        ModelAlert.status != "resolved",
                    )
                )
            ).all()
        )
        by_fingerprint = {row.fingerprint: row for row in open_rows}
        last_notified = {
            row.fingerprint: row.last_notified_at or row.first_seen_at for row in open_rows
        }

        decisions = alerting.deduplicate(
            candidates,
            open_alerts=last_notified,
            now=at,
            cooldown=timedelta(minutes=self.thresholds.cooldown_minutes),
        )

        raised: list[dict[str, Any]] = []
        suppressed: list[dict[str, Any]] = []
        for decision in decisions:
            candidate = decision.candidate
            row = by_fingerprint.get(candidate.fingerprint)
            if row is None:
                row = ModelAlert(
                    fingerprint=candidate.fingerprint,
                    model_version_id=candidate.model_version_id,
                    model_key=candidate.model_key,
                    model_version_label=candidate.model_version,
                    snapshot_id=snapshot.id,
                    scope_key=candidate.scope_key,
                    check=candidate.check[:32],
                    subject=candidate.subject[:64],
                    severity=alerting.NOTIFICATION_SEVERITY.get(candidate.severity, "warning"),
                    status="firing",
                    title=candidate.title[:200],
                    body=candidate.body,
                    metric=(candidate.metric or "")[:32] or None,
                    current_value=_decimal(candidate.current_value),
                    baseline_value=_decimal(candidate.baseline_value),
                    threshold=_decimal(candidate.threshold),
                    sample_size=candidate.sample_size,
                    first_seen_at=at,
                    last_seen_at=at,
                    last_notified_at=at if decision.notify else None,
                    occurrences=1,
                    detail=candidate.detail,
                )
                db.add(row)
            else:
                row.last_seen_at = at
                row.occurrences += 1
                row.snapshot_id = snapshot.id
                row.current_value = _decimal(candidate.current_value)
                row.body = candidate.body
                if decision.notify:
                    row.last_notified_at = at

            entry = alerting.summarise(candidate, notified=decision.notify, reason=decision.reason)
            (raised if decision.notify else suppressed).append(entry)

        # §25. A condition that stopped being reported is RESOLVED and said so,
        # never left to fade out of a dashboard.
        gone = alerting.recoveries(
            open_alerts=set(by_fingerprint), current=[d.candidate for d in decisions]
        )
        for fingerprint in sorted(gone):
            row = by_fingerprint[fingerprint]
            row.status = "resolved"
            row.resolved_at = at
            row.last_seen_at = at

        await db.flush()
        return raised, suppressed, sorted(gone)

    # --------------------------------------------------------------- events

    async def _publish(
        self,
        deployment: ModelDeployment,
        state: HealthState,
        raised: list[dict[str, Any]],
        resolved: list[str],
    ) -> None:
        """Through L07's hub, never a second realtime system (§38)."""
        if self.hub is None:
            return
        from app.core.events import Event
        from app.realtime.catalogue import EventType

        payloads: list[tuple[EventType, dict[str, Any]]] = [
            (
                EventType.MODEL_HEALTH_CHANGED,
                {
                    "model": deployment.model_key,
                    "version": deployment.model_version_label,
                    "health": str(state),
                },
            )
        ]
        for entry in raised:
            payloads.append((EventType.MODEL_ALERT_CREATED, entry))
        for fingerprint in resolved:
            payloads.append(
                (
                    EventType.MODEL_ALERT_RECOVERED,
                    {
                        "model": deployment.model_key,
                        "version": deployment.model_version_label,
                        "fingerprint": fingerprint,
                    },
                )
            )

        for event_type, payload in payloads:
            try:
                await self.hub.publish(  # type: ignore[attr-defined]
                    Event(
                        type=str(event_type),
                        payload=payload,
                        source="model_monitoring",
                        channel=f"model:{deployment.model_key}",
                    )
                )
            except Exception:  # noqa: BLE001 - a run does not fail because an event did
                log.warning(
                    "a monitoring event could not be published",
                    extra={
                        "event": "monitoring_event_failed",
                        "model": deployment.model_key,
                        "type": str(event_type),
                    },
                )


def _trading_block(gathered: collect.Collected) -> dict[str, Any] | None:
    """Section 29 and §30: what the AI's own decisions led to.

    **An observed comparison, and labelled as one.** §30 is explicit that a
    causal claim cannot be made from observational data — the accepted and
    rejected signals are not a randomised split, and whatever separates them may
    be what the model was reacting to rather than what it caused.
    """
    if not gathered.decisions:
        return None
    accepted = sum(gathered.accepted_outcomes.values())
    rejected = sum(gathered.rejected_outcomes.values())
    return {
        "decisions": dict(sorted(gathered.decisions.items())),
        "final_outcomes": dict(sorted(gathered.final_outcomes.items())),
        "accepted_outcomes": dict(sorted(gathered.accepted_outcomes.items())),
        "rejected_outcomes": dict(sorted(gathered.rejected_outcomes.items())),
        "n_accepted_with_outcome": accepted,
        "n_rejected_with_outcome": rejected,
        "acceptance_rate": (
            round(
                gathered.decisions.get("ACCEPT", 0) / sum(gathered.decisions.values()),
                6,
            )
            if sum(gathered.decisions.values())
            else None
        ),
        "reading": (
            "OBSERVED COMPARISON. Accepted and rejected signals are not a randomised "
            "split: whatever separates them may be what the model was reacting to "
            "rather than what it caused. No causal claim is made from this."
        ),
    }


def summarise_snapshot(row: ModelMonitoringSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "model": row.model_key,
        "version": row.model_version_label,
        "model_version_id": row.model_version_id,
        "scope": {
            "strategy_key": row.strategy_key,
            "symbol": row.symbol,
            "timeframe": row.timeframe,
            "environment": row.environment,
        },
        "health": row.health_state,
        "baseline": {
            "kind": row.baseline_kind,
            "id": row.baseline_id,
            "period": [
                row.baseline_period_start.isoformat() if row.baseline_period_start else None,
                row.baseline_period_end.isoformat() if row.baseline_period_end else None,
            ],
        },
        "window": {
            "start": row.window_start.isoformat(),
            "end": row.window_end.isoformat(),
        },
        "sample_count": row.sample_count,
        "metrics": {
            "feature": row.feature_metrics,
            "prediction": row.prediction_metrics,
            "calibration": row.calibration_metrics,
            "performance": row.performance_metrics,
            "latency": row.latency_metrics,
            "availability": row.availability_metrics,
            "trading": row.trading_metrics,
            "regime": row.regime_metrics,
        },
        "health_detail": row.health_detail,
        "monitoring_engine_version": row.monitoring_engine_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "note": row.note,
    }


def summarise_alert(row: ModelAlert) -> dict[str, Any]:
    return {
        "id": row.id,
        "fingerprint": row.fingerprint,
        "model": row.model_key,
        "version": row.model_version_label,
        "check": row.check,
        "subject": row.subject,
        "severity": row.severity,
        "status": row.status,
        "title": row.title,
        "body": row.body,
        "metric": row.metric,
        "current_value": float(row.current_value) if row.current_value is not None else None,
        "baseline_value": float(row.baseline_value) if row.baseline_value is not None else None,
        "threshold": float(row.threshold) if row.threshold is not None else None,
        "sample_size": row.sample_size,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "occurrences": row.occurrences,
        "detail": row.detail,
        "authority": (
            "an alert is a message. Monitoring never retrains, promotes, replaces or "
            "deploys a model, and never changes a risk limit or a position size."
        ),
    }
