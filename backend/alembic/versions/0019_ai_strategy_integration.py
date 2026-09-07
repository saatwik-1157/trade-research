"""ai_strategy_configs and ai_decisions (L27).

Two tables. **Nothing existing is altered and no data is destroyed.**

`ai_strategy_configs` holds how a strategy uses AI: the mode, the failure
policy, the models it names, and every threshold — all configurable, §38 and
§39. It duplicates no model, training or registry table; the models are
`{"key", "version"}` references and the version is exact, because §12 forbids
`latest`.

`ai_decisions` is §35's journal. It references the inference in
`model_predictions` rather than restating it: L24 already records every
prediction including the refusals, and two records of one probability is one
too many. The one denormalised copy (`probability`) exists so a dashboard can
aggregate without a join and is range-checked.

Three constraints are the level's rules put into the schema:

    decision <> 'ERROR' OR probability IS NULL
        §47. A failed inference that reports a number is exactly the shape a
        fabricated result takes.

    mode <> 'AI_DISABLED' OR (probability IS NULL AND model_version_id IS NULL)
        §7. A disabled run computed nothing, so it cannot have produced a
        reading.

    mode = 'AI_DISABLED' OR required_models IS NOT NULL
        §12 and §28. An AI_FILTER naming no model would silently behave as
        AI_DISABLED under AI_OPTIONAL, which is the implicit fallback §28
        forbids.

**Column widths are sized against the vocabulary**, not by habit: `mode` is
String(16) against `AI_ADVISORY` (11), `status` String(32) against
`FEATURE_VERSION_MISMATCH` (24), `decision` String(8) against `NEUTRAL` (7).
Migration 0017 exists because a column was not sized this way.

Revision ID: 0019_ai_strategy_integration
Revises: 0018_validation_runs
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_ai_strategy_integration"
down_revision: str | Sequence[str] | None = "0018_validation_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

_MODES = "mode IN ('AI_DISABLED','AI_ADVISORY','AI_FILTER','AI_SCORING')"
_POLICIES = "policy IN ('AI_REQUIRED','AI_OPTIONAL')"
_DECISIONS = "decision IN ('ACCEPT','REJECT','NEUTRAL','ERROR')"
_SCORING = "scoring_method IN ('minimum','weighted','product')"


def upgrade() -> None:
    op.create_table(
        "ai_strategy_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("strategy_key", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("policy", sa.String(length=16), nullable=False),
        sa.Column("required_models", _JSON, nullable=True),
        sa.Column("optional_models", _JSON, nullable=True),
        sa.Column("feature_version", sa.String(length=16), nullable=True),
        sa.Column("minimum_probability", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("maximum_anomaly_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("allowed_regimes", _JSON, nullable=True),
        sa.Column("minimum_expected_return", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("max_latency_ms", sa.Integer(), nullable=False),
        sa.Column("scoring_method", sa.String(length=16), nullable=False),
        sa.Column("ai_weight", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("minimum_combined_score", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(_MODES, name="mode"),
        sa.CheckConstraint(_POLICIES, name="policy"),
        sa.CheckConstraint(_SCORING, name="scoring_method"),
        sa.CheckConstraint(
            "minimum_probability >= 0 AND minimum_probability <= 1",
            name="minimum_probability_is_a_probability",
        ),
        sa.CheckConstraint(
            "maximum_anomaly_score IS NULL OR "
            "(maximum_anomaly_score >= 0 AND maximum_anomaly_score <= 1)",
            name="maximum_anomaly_score_is_a_score",
        ),
        sa.CheckConstraint("ai_weight >= 0 AND ai_weight <= 1", name="ai_weight_is_a_fraction"),
        sa.CheckConstraint(
            "minimum_combined_score >= 0 AND minimum_combined_score <= 1",
            name="minimum_combined_score_is_a_fraction",
        ),
        sa.CheckConstraint("max_latency_ms > 0", name="latency_budget_is_positive"),
        sa.CheckConstraint(
            "mode = 'AI_DISABLED' OR required_models IS NOT NULL",
            name="an_active_mode_names_a_model",
        ),
        # The BARE name. The naming convention adds `uq_<table>_`, and passing a
        # pre-prefixed one is the defect migrations 0014 and 0015 had -- it
        # produced `uq_ai_strategy_configs_uq_ai_strategy_configs_...` and the
        # drift check caught it.
        sa.UniqueConstraint("strategy_key", "account_id", name="one_config_per_scope"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_strategy_configs_strategy_key", "ai_strategy_configs", ["strategy_key"])

    op.create_table(
        "ai_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("strategy_key", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("timeframe", sa.String(length=4), nullable=True),
        sa.Column("bar_time", sa.DateTime(), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=True),
        sa.Column("signal_id", sa.String(length=36), nullable=True),
        sa.Column("prediction_id", sa.String(length=36), nullable=True),
        sa.Column("model_version_id", sa.String(length=36), nullable=True),
        sa.Column("model_key", sa.String(length=64), nullable=True),
        sa.Column("model_version", sa.String(length=16), nullable=True),
        sa.Column("feature_version", sa.String(length=16), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("policy", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("probability", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("predicted_class", sa.String(length=32), nullable=True),
        sa.Column("regime", sa.String(length=32), nullable=True),
        sa.Column("anomaly_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("strategy_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("combined_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("feature_latency_ms", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("inference_latency_ms", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("total_latency_ms", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("final_outcome", sa.String(length=32), nullable=True),
        sa.Column("risk_verdict", sa.String(length=32), nullable=True),
        sa.Column("order_id", sa.String(length=36), nullable=True),
        sa.Column("execution_id", sa.String(length=36), nullable=True),
        sa.Column("bot_id", sa.String(length=36), nullable=True),
        sa.Column("account_id", sa.String(length=36), nullable=True),
        sa.Column("mode_of_trading", sa.String(length=8), nullable=True),
        sa.Column("detail", _JSON, nullable=True),
        sa.CheckConstraint(_MODES, name="mode"),
        sa.CheckConstraint(_POLICIES, name="policy"),
        sa.CheckConstraint(_DECISIONS, name="decision"),
        sa.CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name="probability_is_a_probability",
        ),
        sa.CheckConstraint(
            "anomaly_score IS NULL OR (anomaly_score >= 0 AND anomaly_score <= 1)",
            name="anomaly_score_is_a_score",
        ),
        sa.CheckConstraint(
            "decision <> 'ERROR' OR probability IS NULL",
            name="an_error_carries_no_probability",
        ),
        sa.CheckConstraint(
            "mode <> 'AI_DISABLED' OR (probability IS NULL AND model_version_id IS NULL)",
            name="a_disabled_run_produced_no_reading",
        ),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["prediction_id"], ["model_predictions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_decisions_strategy_key", "ai_decisions", ["strategy_key"])
    op.create_index("ix_ai_decisions_symbol", "ai_decisions", ["symbol"])
    op.create_index("ix_ai_decisions_bar_time", "ai_decisions", ["bar_time"])
    op.create_index("ix_ai_decisions_signal_id", "ai_decisions", ["signal_id"])
    op.create_index("ix_ai_decisions_model_version_id", "ai_decisions", ["model_version_id"])
    op.create_index("ix_ai_decisions_decision", "ai_decisions", ["decision"])
    op.create_index("ix_ai_decisions_final_outcome", "ai_decisions", ["final_outcome"])
    op.create_index("ix_ai_decisions_execution_id", "ai_decisions", ["execution_id"])
    op.create_index("ix_ai_decisions_strategy_at", "ai_decisions", ["strategy_key", "created_at"])
    op.create_index("ix_ai_decisions_decision_at", "ai_decisions", ["decision", "created_at"])


def downgrade() -> None:
    for name in (
        "ix_ai_decisions_decision_at",
        "ix_ai_decisions_strategy_at",
        "ix_ai_decisions_execution_id",
        "ix_ai_decisions_final_outcome",
        "ix_ai_decisions_decision",
        "ix_ai_decisions_model_version_id",
        "ix_ai_decisions_signal_id",
        "ix_ai_decisions_bar_time",
        "ix_ai_decisions_symbol",
        "ix_ai_decisions_strategy_key",
    ):
        op.drop_index(name, table_name="ai_decisions")
    op.drop_table("ai_decisions")

    op.drop_index("ix_ai_strategy_configs_strategy_key", table_name="ai_strategy_configs")
    op.drop_table("ai_strategy_configs")
