"""model_monitoring_snapshots and model_alerts: what was measured, and what it raised.

Two tables, and §33 is explicit that existing entities be reused where
equivalent ones exist — so neither holds a model, a version, a prediction, a
trade or a portfolio figure. They reference and summarise.

**A snapshot is reproducible.** Section 34. It records the model version, the
scope, the baseline it was measured against, the exact window bounds, the
sample counts and every metric block — so a figure can be re-derived and two
snapshots can be compared without anyone having to reconstruct what the second
one was computed over.

**A snapshot always states its sample size.** Section 13. Every metric block
carries its own `n`, and `health_state` can be `INSUFFICIENT_DATA` — which is
NOT a healthy state and outranks one, because §43 forbids converting too little
data into a false pass.

**An alert is a state machine, not a log line.** Sections 24 and 25. One row per
open condition, identified by a fingerprint, with `resolved_at` written when the
condition clears. A monitor that inserted a row per observation would produce
the flood §24 exists to prevent, and a condition that simply stopped appearing
would leave a gap rather than a recovery.

**Nothing here can act.** No column carries an order, a quantity, a risk limit
or a model status. Monitoring detects, records and alerts; §26's rule is that it
never retrains, promotes, replaces or deploys, and there is no field by which it
could.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import CreatedMixin, IdMixin, JSONType

# Section 21. Six, and the two that are not about the model matter most:
# INSUFFICIENT_DATA says we could not measure enough to say anything, and
# OFFLINE says nothing is deployed. Both are commonly reported as healthy by
# monitoring systems, and both mean the opposite.
HEALTH_STATES = (
    "HEALTHY",
    "WARNING",
    "DEGRADED",
    "CRITICAL",
    "INSUFFICIENT_DATA",
    "OFFLINE",
)

# Section 5 and §40. What a current window was compared against.
BASELINE_KINDS = (
    "TRAINING",
    "VALIDATION",
    "PAPER",
    "PRODUCTION_REFERENCE",
    "PREVIOUS_PERIOD",
    "PREVIOUS_MODEL",
)

# Section 23.
ALERT_SEVERITIES = ("info", "warning", "critical")

# Sections 24 and 25. `firing` and `resolved` are the two an operator reads;
# `acknowledged` records that somebody has seen it without claiming the
# condition went away, which is the distinction §35's acknowledge route needs.
ALERT_STATUSES = ("firing", "acknowledged", "resolved")


class ModelMonitoringSnapshot(IdMixin, CreatedMixin, Base):
    """One monitoring run over one deployment. Section 34."""

    __tablename__ = "model_monitoring_snapshots"
    __table_args__ = (
        CheckConstraint(
            "health_state IN ('" + "','".join(HEALTH_STATES) + "')", name="health_state"
        ),
        CheckConstraint(
            "baseline_kind IS NULL OR baseline_kind IN ('" + "','".join(BASELINE_KINDS) + "')",
            name="baseline_kind",
        ),
        CheckConstraint("sample_count >= 0", name="sample_count_not_negative"),
        # §34: a snapshot is reproducible, which needs the window it covered.
        # Without both bounds a figure cannot be re-derived, and a snapshot that
        # cannot be re-derived is a number nobody can check.
        CheckConstraint(
            "window_start IS NOT NULL AND window_end IS NOT NULL AND window_end > window_start",
            name="a_snapshot_states_its_window",
        ),
        # §13 and §43, in the schema. A snapshot claiming to be healthy on no
        # observations is the false clean bill of health this level exists to
        # prevent.
        CheckConstraint(
            "health_state <> 'HEALTHY' OR sample_count > 0",
            name="a_healthy_snapshot_has_a_sample",
        ),
        Index("ix_model_monitoring_snapshots_version_at", "model_version_id", "created_at"),
        Index("ix_model_monitoring_snapshots_scope", "model_key", "environment", "created_at"),
    )

    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_version_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_deployments.id", ondelete="SET NULL"), nullable=True
    )

    # The scope this snapshot covers. §17 and §18: a model watched on one
    # symbol and one timeframe is watched separately, because aggregation hides
    # exactly the problems worth finding.
    strategy_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    timeframe: Mapped[str | None] = mapped_column(String(4), nullable=True)
    environment: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")

    # §5 and §40. Which reference, and where it came from. NULL means no
    # baseline was available -- which the checks report as INSUFFICIENT_DATA,
    # never as a comparison against zero.
    baseline_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    baseline_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    baseline_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    baseline_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    health_state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    # The metric blocks, each carrying its own sample size. Separate columns
    # rather than one payload so a query can ask "show me every snapshot whose
    # latency block exists" without parsing every row -- and so a block that was
    # not computed is NULL rather than an empty object that reads as measured.
    feature_metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    prediction_metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    calibration_metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    performance_metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    latency_metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    availability_metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    trading_metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    regime_metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    # Every finding, with its severity and the numbers behind it.
    findings: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    # What health the state was derived from, so the conclusion is checkable.
    health_detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    thresholds: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    monitoring_engine_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelAlert(IdMixin, CreatedMixin, Base):
    """One open (or closed) monitoring condition. Sections 22 to 25."""

    __tablename__ = "model_alerts"
    __table_args__ = (
        CheckConstraint("severity IN ('" + "','".join(ALERT_SEVERITIES) + "')", name="severity"),
        CheckConstraint("status IN ('" + "','".join(ALERT_STATUSES) + "')", name="status"),
        # §25. A resolved alert says when it cleared; an open one has not.
        # Without this, "still firing" and "cleared at a time nobody recorded"
        # are the same row and the second reads as the first.
        CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)",
            name="a_resolved_alert_says_when",
        ),
        # §24. One OPEN alert per fingerprint. A monitor that inserted a row per
        # observation would produce the flood this exists to prevent; the
        # partial index makes the deduplication a database guarantee rather than
        # a check the caller has to remember.
        Index(
            "uq_model_alerts_one_open_per_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status <> 'resolved'"),
            sqlite_where=text("status <> 'resolved'"),
        ),
        Index("ix_model_alerts_version_at", "model_version_id", "created_at"),
        Index("ix_model_alerts_open", "status", "severity"),
    )

    #: `(model version, scope, check, subject, severity)`, hashed. Includes the
    #: severity deliberately: a WARNING that becomes CRITICAL is a NEW alert,
    #: because suppressing that transition would hide what an operator most
    #: needs to see.
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_version_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_monitoring_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    scope_key: Mapped[str | None] = mapped_column(String(112), nullable=True)

    check: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="warning")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="firing", index=True)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # §22: an alert carries its metric, its current value, its baseline, its
    # threshold and its sample size. A message that says "drift detected" and
    # nothing else cannot be acted on or argued with.
    metric: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    baseline_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    #: When it last produced a notification, so the cooldown is a stored fact
    #: rather than one recomputed from a log.
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
