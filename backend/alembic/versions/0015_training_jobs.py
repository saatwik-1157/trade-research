"""`training_runs` becomes a training job that can be observed (L25).

Additive, plus one column relaxed. No row is rewritten, no history touched, and
the table has been empty since L05 — it was created with the schema and never
used, which the L25 audit confirmed.

**`model_version_id` becomes nullable, and that is the load-bearing change.**
It was NOT NULL with a foreign key to `model_versions`, which assumes a run
already has a model — but a job is created when it is *queued*, and the
candidate does not exist until the fit finishes. Under the old shape a queued
job could not be recorded at all, so a caller would have had to invent a model
version before training one. A job that has not produced a candidate has no
model version, and the column now says so.

**Two statuses are added and three are deliberately not.** `cancelling` (asked
to stop, not yet stopped — the same distinction L19 draws between
`cancel_requested` and `cancelled`) and `validation_pending`, which is where a
successful run ENDS. Section 25: "candidate model successfully trained" is not
"model approved for trading", so there is no `completed` that could be read as
the second.

Not added: `paused`, because nothing here can pause — these fits are seconds
long and there is no checkpoint to resume from, so the state would be
unreachable; and `validation_passed` / `validation_failed`, which belong to
level 26 and should arrive with the code that writes them. Declaring a value
nothing can write makes the vocabulary a wish list, which is the reasoning L22
used on bot states and L23 on dataset ones.

**`config_fingerprint` is indexed, not unique.** Section 38 wants a duplicate
*running* job refused, which the service does by querying active rows. A unique
index would also forbid ever re-running a recipe after it finished, and
re-running a training job on new data is a thing somebody legitimately wants.

Revision ID: 0015_training_jobs
Revises: 0014_model_metadata
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_training_jobs"
down_revision: str | Sequence[str] | None = "0014_model_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "status IN ('queued','running','finished','failed','cancelled')"
NEW_STATUSES = (
    "status IN ('queued','running','cancelling','cancelled','failed',"
    "'finished','validation_pending')"
)


def upgrade() -> None:
    # A queued job has no candidate yet. The old NOT NULL made a queued row
    # impossible to write without inventing a model version first.
    op.alter_column(
        "training_runs", "model_version_id", existing_type=sa.String(length=36), nullable=True
    )

    # The BARE name. The naming convention adds the ck_<table>_ prefix, so
    # passing the prefixed one asks Postgres to drop
    # ck_training_runs_ck_training_runs_status, which does not exist. Every
    # migration from 0007 onward passes the bare name.
    op.drop_constraint("status", "training_runs", type_="check")
    op.create_check_constraint("status", "training_runs", sa.text(NEW_STATUSES))

    for column in (
        sa.Column("model_key", sa.String(length=64), nullable=True),
        sa.Column("model_version_label", sa.String(length=16), nullable=True),
        sa.Column("dataset_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("config_fingerprint", sa.String(length=32), nullable=True),
        sa.Column("current_stage", sa.String(length=32), nullable=True),
        sa.Column("progress", sa.Numeric(5, 4), nullable=True),
        sa.Column("random_seed", sa.Integer(), nullable=True),
        sa.Column("requested_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        # Section 5 lists `created_at` and the table never had one: a job that
        # cannot say when it was requested cannot be ordered against the others.
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    ):
        op.add_column("training_runs", column)
    op.alter_column("training_runs", "created_at", server_default=None)

    op.create_foreign_key(
        op.f("fk_training_runs_requested_by_user_id_users"),
        "training_runs",
        "users",
        ["requested_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_training_runs_config_fingerprint"), "training_runs", ["config_fingerprint"]
    )
    op.create_index(op.f("ix_training_runs_status"), "training_runs", ["status"])

    # A job that claims to have produced a candidate must name it. Without this
    # a `validation_pending` row with no model version would be a claim nobody
    # can check -- the same shape as `ready_is_reproducible` on `datasets`.
    op.create_check_constraint(
        "pending_validation_names_its_candidate",
        "training_runs",
        "status <> 'validation_pending' OR model_version_id IS NOT NULL",
    )
    op.create_check_constraint(
        "progress_is_a_fraction",
        "training_runs",
        "progress IS NULL OR (progress >= 0 AND progress <= 1)",
    )


def downgrade() -> None:
    op.drop_constraint("progress_is_a_fraction", "training_runs", type_="check")
    op.drop_constraint("pending_validation_names_its_candidate", "training_runs", type_="check")
    op.drop_index(op.f("ix_training_runs_status"), table_name="training_runs")
    op.drop_index(op.f("ix_training_runs_config_fingerprint"), table_name="training_runs")
    op.drop_constraint(
        op.f("fk_training_runs_requested_by_user_id_users"), "training_runs", type_="foreignkey"
    )
    for column in (
        "created_at",
        "cancelled_at",
        "requested_by_user_id",
        "random_seed",
        "progress",
        "current_stage",
        "config_fingerprint",
        "dataset_fingerprint",
        "model_version_label",
        "model_key",
    ):
        op.drop_column("training_runs", column)
    op.drop_constraint("status", "training_runs", type_="check")
    op.create_check_constraint("status", "training_runs", sa.text(OLD_STATUSES))
    op.alter_column(
        "training_runs", "model_version_id", existing_type=sa.String(length=36), nullable=False
    )
