"""validation_runs: one validation of one candidate.

**A new table, not new columns on `training_runs`.** A candidate can be
validated more than once — new data, changed thresholds, a re-check after a
disputed result — and a column holds one answer.

**`validation_runs.status` is String(16) and the longest value is
`cancelling` (10).** Sized against the vocabulary rather than by habit, because
L21's `VARCHAR(8)` for `partially_closed` is what migration 0017 exists to fix.
The verdict column is String(16) against `CONDITIONAL` (11).

**`model_versions` is deliberately untouched.** The obvious-looking addition
here is a `validating` or `rejected` status on the version row, and it would be
a state nothing can enter: `app/validation/` writes no version status, because
section 34 says a passing report makes a candidate eligible for CONSIDERATION
and nothing more. L25 declined to add `validation_passed` for the same reason,
and a test asserts the validation package contains no path to that column.
Whichever states promotion needs are L28's to add, when something can set them.

**No data is destroyed and no existing table is altered.** One table is created.

Revision ID: 0018_validation_runs
Revises: 0017_status_column_widths
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_validation_runs"
down_revision: str | Sequence[str] | None = "0017_status_column_widths"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "validation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("model_version_id", sa.String(length=36), nullable=False),
        sa.Column("training_run_id", sa.String(length=36), nullable=True),
        sa.Column("dataset_id", sa.String(length=36), nullable=True),
        sa.Column("dataset_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        # JSONB on PostgreSQL, matching `JSONType` on the model. Declaring a
        # plain JSON here passes on SQLite and drifts on PostgreSQL, which the
        # migration drift test catches.
        sa.Column(
            "config",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "report",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("checks_passed", sa.Numeric(precision=5, scale=0), nullable=True),
        sa.Column("checks_failed", sa.Numeric(precision=5, scale=0), nullable=True),
        sa.Column("checks_warning", sa.Numeric(precision=5, scale=0), nullable=True),
        sa.Column("checks_blocked", sa.Numeric(precision=5, scale=0), nullable=True),
        sa.Column("validation_engine_version", sa.String(length=16), nullable=True),
        sa.Column("config_fingerprint", sa.String(length=32), nullable=True),
        sa.Column("current_stage", sa.String(length=32), nullable=True),
        sa.Column("progress", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by_user_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','cancelling','cancelled','failed','completed')",
            name="status",
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('PASS','FAIL','CONDITIONAL','BLOCKED')",
            name="verdict",
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR (verdict IS NOT NULL AND report IS NOT NULL)",
            name="completed_states_its_verdict",
        ),
        sa.CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 1)",
            name="progress_is_a_fraction",
        ),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["training_run_id"], ["training_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_validation_runs_model_version_id", "validation_runs", ["model_version_id"]
    )
    op.create_index("ix_validation_runs_status", "validation_runs", ["status"])
    op.create_index("ix_validation_runs_verdict", "validation_runs", ["verdict"])
    op.create_index("ix_validation_runs_training_run_id", "validation_runs", ["training_run_id"])
    op.create_index("ix_validation_runs_dataset_id", "validation_runs", ["dataset_id"])
    op.create_index(
        "ix_validation_runs_config_fingerprint", "validation_runs", ["config_fingerprint"]
    )
    op.create_index(
        "ix_validation_runs_model_version",
        "validation_runs",
        ["model_version_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_validation_runs_model_version", table_name="validation_runs")
    op.drop_index("ix_validation_runs_config_fingerprint", table_name="validation_runs")
    op.drop_index("ix_validation_runs_dataset_id", table_name="validation_runs")
    op.drop_index("ix_validation_runs_training_run_id", table_name="validation_runs")
    op.drop_index("ix_validation_runs_verdict", table_name="validation_runs")
    op.drop_index("ix_validation_runs_status", table_name="validation_runs")
    op.drop_index("ix_validation_runs_model_version_id", table_name="validation_runs")
    op.drop_table("validation_runs")
