"""model_monitoring_snapshots and model_alerts (L29).

**Two tables. Nothing existing is altered and no data is destroyed.**

`model_monitoring_snapshots` is §34's reproducible snapshot: the model version,
the scope, the baseline it was measured against, the exact window bounds, the
sample count, and one JSON block per metric family. A block that was not
computed is NULL rather than an empty object, because an empty object reads as
"measured, and nothing there".

`model_alerts` is §24's state machine rather than a log: one row per open
condition, keyed by a fingerprint, with `resolved_at` written when it clears.

Four constraints carry the level's rules into the schema:

    window_start IS NOT NULL AND window_end IS NOT NULL AND window_end > window_start
        §34. A snapshot that cannot say what it covered cannot be re-derived,
        and a figure nobody can re-derive is a number nobody can check.

    health_state <> 'HEALTHY' OR sample_count > 0
        §13 and §43. A snapshot claiming health on no observations is the false
        clean bill of health this level exists to prevent.

    (status = 'resolved') = (resolved_at IS NOT NULL)
        §25. "Still firing" and "cleared at a time nobody recorded" must not be
        the same row, because the second reads as the first.

    UNIQUE (fingerprint) WHERE status <> 'resolved'
        §24. One OPEN alert per condition, as a database guarantee rather than a
        check the caller has to remember. A monitor that inserted a row per
        observation would produce exactly the flood this prevents.

**The partial unique index is over a NOT NULL column**, unlike L28's first
attempt: `fingerprint` is always present, so the "NULLs are distinct" trap that
migration 0020 had to work around does not arise here.

Revision ID: 0021_model_monitoring
Revises: 0020_model_registry
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_model_monitoring"
down_revision: str | Sequence[str] | None = "0020_model_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

_HEALTH = (
    "health_state IN ('HEALTHY','WARNING','DEGRADED','CRITICAL',"
    "'INSUFFICIENT_DATA','OFFLINE')"
)
_BASELINE = (
    "baseline_kind IS NULL OR baseline_kind IN ('TRAINING','VALIDATION','PAPER',"
    "'PRODUCTION_REFERENCE','PREVIOUS_PERIOD','PREVIOUS_MODEL')"
)
_ALERT_SEVERITY = "severity IN ('info','warning','critical')"
_ALERT_STATUS = "status IN ('firing','acknowledged','resolved')"


def upgrade() -> None:
    op.create_table(
        "model_monitoring_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("model_version_id", sa.String(length=36), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("model_version_label", sa.String(length=16), nullable=True),
        sa.Column("deployment_id", sa.String(length=36), nullable=True),
        sa.Column("strategy_key", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("timeframe", sa.String(length=4), nullable=True),
        sa.Column("environment", sa.String(length=8), nullable=False),
        sa.Column("baseline_kind", sa.String(length=24), nullable=True),
        sa.Column("baseline_id", sa.String(length=36), nullable=True),
        sa.Column("baseline_period_start", sa.DateTime(), nullable=True),
        sa.Column("baseline_period_end", sa.DateTime(), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("health_state", sa.String(length=24), nullable=False),
        sa.Column("feature_metrics", _JSON, nullable=True),
        sa.Column("prediction_metrics", _JSON, nullable=True),
        sa.Column("calibration_metrics", _JSON, nullable=True),
        sa.Column("performance_metrics", _JSON, nullable=True),
        sa.Column("latency_metrics", _JSON, nullable=True),
        sa.Column("availability_metrics", _JSON, nullable=True),
        sa.Column("trading_metrics", _JSON, nullable=True),
        sa.Column("regime_metrics", _JSON, nullable=True),
        sa.Column("findings", _JSON, nullable=True),
        sa.Column("health_detail", _JSON, nullable=True),
        sa.Column("thresholds", _JSON, nullable=True),
        sa.Column("monitoring_engine_version", sa.String(length=16), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(_HEALTH, name="health_state"),
        sa.CheckConstraint(_BASELINE, name="baseline_kind"),
        sa.CheckConstraint("sample_count >= 0", name="sample_count_not_negative"),
        sa.CheckConstraint(
            "window_start IS NOT NULL AND window_end IS NOT NULL AND window_end > window_start",
            name="a_snapshot_states_its_window",
        ),
        sa.CheckConstraint(
            "health_state <> 'HEALTHY' OR sample_count > 0",
            name="a_healthy_snapshot_has_a_sample",
        ),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deployment_id"], ["model_deployments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_monitoring_snapshots_model_version_id",
        "model_monitoring_snapshots",
        ["model_version_id"],
    )
    op.create_index(
        "ix_model_monitoring_snapshots_model_key", "model_monitoring_snapshots", ["model_key"]
    )
    op.create_index(
        "ix_model_monitoring_snapshots_symbol", "model_monitoring_snapshots", ["symbol"]
    )
    op.create_index(
        "ix_model_monitoring_snapshots_window_end", "model_monitoring_snapshots", ["window_end"]
    )
    op.create_index(
        "ix_model_monitoring_snapshots_health_state",
        "model_monitoring_snapshots",
        ["health_state"],
    )
    op.create_index(
        "ix_model_monitoring_snapshots_version_at",
        "model_monitoring_snapshots",
        ["model_version_id", "created_at"],
    )
    op.create_index(
        "ix_model_monitoring_snapshots_scope",
        "model_monitoring_snapshots",
        ["model_key", "environment", "created_at"],
    )

    op.create_table(
        "model_alerts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("fingerprint", sa.String(length=32), nullable=False),
        sa.Column("model_version_id", sa.String(length=36), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("model_version_label", sa.String(length=16), nullable=True),
        sa.Column("snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("scope_key", sa.String(length=112), nullable=True),
        sa.Column("check", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("metric", sa.String(length=32), nullable=True),
        sa.Column("current_value", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("baseline_value", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("threshold", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("detail", _JSON, nullable=True),
        sa.CheckConstraint(_ALERT_SEVERITY, name="severity"),
        sa.CheckConstraint(_ALERT_STATUS, name="status"),
        sa.CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)",
            name="a_resolved_alert_says_when",
        ),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["model_monitoring_snapshots.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["acknowledged_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_alerts_fingerprint", "model_alerts", ["fingerprint"])
    op.create_index("ix_model_alerts_model_version_id", "model_alerts", ["model_version_id"])
    op.create_index("ix_model_alerts_model_key", "model_alerts", ["model_key"])
    op.create_index("ix_model_alerts_check", "model_alerts", ["check"])
    op.create_index("ix_model_alerts_status", "model_alerts", ["status"])
    op.create_index("ix_model_alerts_version_at", "model_alerts", ["model_version_id", "created_at"])
    op.create_index("ix_model_alerts_open", "model_alerts", ["status", "severity"])
    op.create_index(
        "uq_model_alerts_one_open_per_fingerprint",
        "model_alerts",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'resolved'"),
        sqlite_where=sa.text("status <> 'resolved'"),
    )


def downgrade() -> None:
    for name in (
        "uq_model_alerts_one_open_per_fingerprint",
        "ix_model_alerts_open",
        "ix_model_alerts_version_at",
        "ix_model_alerts_status",
        "ix_model_alerts_check",
        "ix_model_alerts_model_key",
        "ix_model_alerts_model_version_id",
        "ix_model_alerts_fingerprint",
    ):
        op.drop_index(name, table_name="model_alerts")
    op.drop_table("model_alerts")

    for name in (
        "ix_model_monitoring_snapshots_scope",
        "ix_model_monitoring_snapshots_version_at",
        "ix_model_monitoring_snapshots_health_state",
        "ix_model_monitoring_snapshots_window_end",
        "ix_model_monitoring_snapshots_symbol",
        "ix_model_monitoring_snapshots_model_key",
        "ix_model_monitoring_snapshots_model_version_id",
    ):
        op.drop_index(name, table_name="model_monitoring_snapshots")
    op.drop_table("model_monitoring_snapshots")
