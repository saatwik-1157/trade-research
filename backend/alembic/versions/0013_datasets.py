"""The AI data pipeline gets its registry (L23).

Four new tables and not one existing column touched. No data is moved, no
history is rewritten, and `market_bars` — the raw layer this whole level reads
— is untouched by design: section 10 says raw data is never overwritten by
cleaned values, and the cleaning here has no write path to it at all.

**No table stores a training row.** A dataset is a recipe plus a fingerprint,
and `app.datasets.builder` rebuilds its rows from `market_bars` on demand. The
alternative is a second copy of data the platform already holds, and a second
copy is one that can drift from the first — which is the argument `market_bars`
itself makes for keeping `provider` in its identity.

**`feature_sets` and `label_sets` are immutable versions.** Unique on
`(key, version)`, and nothing updates a row. A changed formula mints a new
version, so a dataset built six months ago still describes what it was built
from. Sections 18 and 21.

**`datasets.status` is limited to the three states this level can reach.**
Section 35 also names LOCKED, TRAINING and ARCHIVED; those belong to levels 25
and 28. Declaring a value nothing can write would make the vocabulary a wish
list — the same reasoning that kept `created` out of the bot run states at L22.

**`ready_is_reproducible` is the constraint worth reading.** A row may only
claim READY while it carries a fingerprint and a positive row count. A dataset
that says it is ready to train on, without the identity that reproduces it, is
a claim nobody can check — and this level exists to make that class of claim
checkable.

Revision ID: 0013_datasets
Revises: 0012_bot_lifecycle
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_datasets"
down_revision: str | Sequence[str] | None = "0012_bot_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "status IN ('RAW','CLEAN','READY')"
CATEGORIES = "category IN ('quality','leakage','split')"


def upgrade() -> None:
    op.create_table(
        "feature_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "definition",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("warmup_bars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_sets")),
        sa.UniqueConstraint("key", "version", name="feature_set_identity"),
    )
    op.create_index(op.f("ix_feature_sets_key"), "feature_sets", ["key"])

    op.create_table(
        "label_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "params",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("horizon", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_sets")),
        sa.UniqueConstraint("key", "version", name="label_set_identity"),
    )
    op.create_index(op.f("ix_label_sets_key"), "label_sets", ["key"])

    op.create_table(
        "datasets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column("feature_set_id", sa.String(length=36), nullable=False),
        sa.Column("label_set_id", sa.String(length=36), nullable=False),
        sa.Column("symbol_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="RAW"),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_at", sa.DateTime(), nullable=True),
        sa.Column("end_at", sa.DateTime(), nullable=True),
        sa.Column("quality_score", sa.Integer(), nullable=True),
        sa.Column(
            "manifest",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
        sa.UniqueConstraint("key", "version", name="dataset_identity"),
        # RESTRICT rather than CASCADE: deleting a feature set out from under a
        # dataset that was built with it would leave the dataset unable to say
        # what it is, which is the one thing this table exists to preserve.
        sa.ForeignKeyConstraint(
            ["feature_set_id"],
            ["feature_sets.id"],
            name=op.f("fk_datasets_feature_set_id_feature_sets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["label_set_id"],
            ["label_sets.id"],
            name=op.f("fk_datasets_label_set_id_label_sets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["symbol_id"],
            ["symbols.id"],
            name=op.f("fk_datasets_symbol_id_symbols"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_datasets_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(STATUSES, name=op.f("ck_datasets_status")),
        sa.CheckConstraint("row_count >= 0", name=op.f("ck_datasets_row_count_not_negative")),
        sa.CheckConstraint(
            "status <> 'READY' OR (fingerprint IS NOT NULL AND row_count > 0)",
            name=op.f("ck_datasets_ready_is_reproducible"),
        ),
    )
    op.create_index(op.f("ix_datasets_key"), "datasets", ["key"])
    op.create_index("ix_datasets_fingerprint", "datasets", ["fingerprint"])
    op.create_index(op.f("ix_datasets_feature_set_id"), "datasets", ["feature_set_id"])
    op.create_index(op.f("ix_datasets_label_set_id"), "datasets", ["label_set_id"])
    op.create_index(op.f("ix_datasets_symbol_id"), "datasets", ["symbol_id"])

    op.create_table(
        "dataset_checks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("check_name", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("ran_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_checks")),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_dataset_checks_dataset_id_datasets"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(CATEGORIES, name=op.f("ck_dataset_checks_category")),
    )
    op.create_index(op.f("ix_dataset_checks_dataset_id"), "dataset_checks", ["dataset_id"])
    op.create_index("ix_dataset_checks_dataset", "dataset_checks", ["dataset_id", "category"])


def downgrade() -> None:
    op.drop_table("dataset_checks")
    op.drop_table("datasets")
    op.drop_table("label_sets")
    op.drop_table("feature_sets")
