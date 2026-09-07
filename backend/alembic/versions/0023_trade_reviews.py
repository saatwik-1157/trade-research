"""trade_reviews: one reviewed version of one completed trade (L33).

**One table. Nothing existing is altered and no data is destroyed.**

`journal_entries.ai_review` has carried a `# L33` comment since L05, and it is
KEPT and left alone: it is a USER's note about a trade, and §39 of L31 already
separated user notes from system-generated facts. A machine-generated review
living in the same column would erase that separation the moment somebody
queried it.

Four constraints carry the level's rules into the schema:

    status IN (...)
        Section 10. Only states the pipeline can reach. The enum in
        `app.review.contract` asserts against this tuple on import, so a state
        the code can produce and the schema rejects cannot exist.

    review_version >= 1
        Section 33. Version 0 is not a draft, it is a bug.

    confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
        Section 22. A confidence outside the interval is not a low confidence.

    status <> 'COMPLETED' OR (review_model IS NOT NULL AND
                              review_model_version IS NOT NULL)
        Sections 21 and 38. A completed review that cannot say which model
        produced it is untraceable output stored as trusted data, which §38
        forbids in as many words.

    UNIQUE (trade_id, review_version)
        Section 11. A redelivered TRADE_CLOSED event resolves to the existing
        row rather than writing a second one, and the guarantee is the
        database's rather than the worker's to remember. Both columns are NOT
        NULL, so the "NULLs are distinct" trap migration 0020 hit does not
        arise here.

**Every JSON section column is nullable and means "not assessed".** A block that
was not evaluated is NULL rather than `{}` -- the rule migration 0021 recorded:
an empty object reads as "measured, and nothing there".

Revision ID: 0023_trade_reviews
Revises: 0022_trade_journal
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_trade_reviews"
down_revision: str | None = "0022_trade_journal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    """JSONB on PostgreSQL, JSON elsewhere.

    Declared explicitly rather than as `sa.JSON()`: the model uses `JSONType`,
    which resolves to JSONB on PostgreSQL, and migration 0018 shipped the drift
    that comes from forgetting it -- passing on SQLite and diverging on Postgres.
    """
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "trade_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("trade_id", sa.String(length=36), nullable=False),
        sa.Column("environment", sa.String(length=8), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("compliance", sa.String(length=24), nullable=True),
        sa.Column("strategy_alignment", _json(), nullable=True),
        sa.Column("entry_quality", _json(), nullable=True),
        sa.Column("exit_quality", _json(), nullable=True),
        sa.Column("risk_quality", _json(), nullable=True),
        sa.Column("execution_quality", _json(), nullable=True),
        sa.Column("market_context", _json(), nullable=True),
        sa.Column("ai_context", _json(), nullable=True),
        sa.Column("key_factors", _json(), nullable=True),
        sa.Column("warnings", _json(), nullable=True),
        sa.Column("lessons", _json(), nullable=True),
        sa.Column("follow_up_questions", _json(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("completeness", _json(), nullable=True),
        sa.Column("review_model", sa.String(length=64), nullable=True),
        sa.Column("review_model_version", sa.String(length=32), nullable=True),
        sa.Column("prediction_model", sa.String(length=64), nullable=True),
        sa.Column("prediction_model_version", sa.String(length=32), nullable=True),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="1.0"),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("input_snapshot", _json(), nullable=True),
        sa.Column("validation", _json(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("requested_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade_reviews")),
        # Bare names. The naming convention interpolates the prefix, and four
        # migrations in this repository have now shipped a doubled one.
        sa.CheckConstraint(
            "status IN ('PENDING','PROCESSING','COMPLETED','FAILED','RETRYING','CANCELLED')",
            name="status",
        ),
        sa.CheckConstraint("review_version >= 1", name="version_is_positive"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_is_a_probability",
        ),
        sa.CheckConstraint(
            "status <> 'COMPLETED' OR (review_model IS NOT NULL "
            "AND review_model_version IS NOT NULL)",
            name="a_completed_review_names_its_model",
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["trades.id"],
            name=op.f("fk_trade_reviews_trade_id_trades"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name=op.f("fk_trade_reviews_requested_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("trade_id", "review_version", name="one_review_per_version"),
    )
    op.create_index("ix_trade_reviews_trade_id", "trade_reviews", ["trade_id"])
    op.create_index("ix_trade_reviews_request_id", "trade_reviews", ["request_id"])
    op.create_index("ix_trade_reviews_trade_status", "trade_reviews", ["trade_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_trade_reviews_trade_status", table_name="trade_reviews")
    op.drop_index("ix_trade_reviews_request_id", table_name="trade_reviews")
    op.drop_index("ix_trade_reviews_trade_id", table_name="trade_reviews")
    op.drop_table("trade_reviews")
