"""The model registry: lifecycle, artifact integrity, deployments and history (L28).

**No data is destroyed and no row is rewritten.** Two tables are created,
fourteen nullable columns are added to `model_versions`, and one CHECK
constraint is widened. Every existing row keeps its status: `draft`,
`validated`, `promoted` and `retired` all remain legal, so nothing has to be
migrated to a new vocabulary.

**The status CHECK gains four values and declines four.** Added: `registered`,
`paper`, `rejected`, `rolled_back`. NOT added: `validating`, `failed`, `active`,
`candidate` — `app/ai/lifecycle.DECLINED` records the reason for each, and the
common one is that nothing could set them or that the fact already lives
somewhere else. A state nothing can enter makes a vocabulary a wish list.

**Two new CHECKs put the level's own gates into the schema:**

    status IN ('draft','validated','rejected') OR artifact_sha256 IS NOT NULL
        §7. A version the registry accepted must carry the digest that makes
        its artifact checkable. Without one, "verified" and "never verified"
        are indistinguishable and the second reads as the first.

    status IN ('draft','validated','rejected') OR validation_run_id IS NOT NULL
        §8 and §9. Registration is gated on validation, and the run that gated
        it is named. A registered version whose decisive run cannot be
        identified is a claim nobody can re-check.

Both are written so that every EXISTING row satisfies them: a `promoted` row
predating this migration would violate them, and there is none — a query
asserts that before the constraints are added, and the migration fails loudly
rather than silently skipping a row.

**The partial unique index is §14 and §31.** At most one `active` deployment per
`(model_key, strategy_key, symbol, timeframe, environment)`. Two administrators
promoting at once produce one winner and one `IntegrityError`; a
`SELECT`-then-`INSERT` would produce two active versions and no error.

Revision ID: 0020_model_registry
Revises: 0019_ai_strategy_integration
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_model_registry"
down_revision: str | Sequence[str] | None = "0019_ai_strategy_integration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

_OLD_STATUSES = "status IN ('draft','validated','promoted','retired')"
_NEW_STATUSES = (
    "status IN ('draft','validated','registered','paper','promoted','rejected',"
    "'rolled_back','retired')"
)
_HAS_DIGEST = "status IN ('draft','validated','rejected') OR artifact_sha256 IS NOT NULL"
_HAS_VALIDATION = "status IN ('draft','validated','rejected') OR validation_run_id IS NOT NULL"

_DEPLOYMENT_STATUSES = "status IN ('active','superseded','rolled_back','stopped')"
_ENVIRONMENTS = "environment IN ('paper','demo','live')"


def upgrade() -> None:
    # --- 1. the new columns on model_versions ----------------------------
    for column in (
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("artifact_bytes", sa.Integer(), nullable=True),
        sa.Column("artifact_kind", sa.String(length=32), nullable=True),
        sa.Column("framework", sa.String(length=32), nullable=True),
        sa.Column("framework_version", sa.String(length=32), nullable=True),
        sa.Column("training_run_id", sa.String(length=36), nullable=True),
        sa.Column("validation_run_id", sa.String(length=36), nullable=True),
        sa.Column("symbol_scope", sa.String(length=32), nullable=True),
        sa.Column("timeframe_scope", sa.String(length=4), nullable=True),
        sa.Column("registered_at", sa.DateTime(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(), nullable=True),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.Column("promoted_by_user_id", sa.String(length=36), nullable=True),
    ):
        op.add_column("model_versions", column)

    # Every existing row must already satisfy the two new gates, or adding them
    # would fail at an arbitrary point. Checked and reported rather than
    # assumed: a migration that half-applies is worse than one that refuses.
    bind = op.get_bind()
    stranded = bind.execute(
        sa.text(
            "SELECT count(*) FROM model_versions "
            "WHERE status NOT IN ('draft','validated','rejected')"
        )
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} model version(s) are already past `validated` and carry no "
            "artifact digest or validation run. L28 gates registration on both, and "
            "back-filling them here would be inventing provenance. Re-register those "
            "versions through the registry after this migration, or set them to "
            "`draft` first and re-run."
        )

    # BARE constraint names throughout. The naming convention prefixes them
    # (`ck_%(table_name)s_%(constraint_name)s`), so passing an already-prefixed
    # one produces `ck_model_versions_ck_model_versions_status` and the drop
    # fails. This is the same defect migrations 0014, 0015 and 0019 had, on a
    # third constraint kind -- which is why the rule is stated without an
    # exception: every constraint kind, bare name, always.
    with op.batch_alter_table("model_versions") as batch:
        batch.drop_constraint("status", type_="check")
        batch.create_check_constraint("status", _NEW_STATUSES)
        batch.create_check_constraint("a_registered_version_carries_its_digest", _HAS_DIGEST)
        batch.create_check_constraint("a_registered_version_names_its_validation", _HAS_VALIDATION)
        # `None` for the name, so the naming convention supplies it -- the fk
        # rule is `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`
        # and does not interpolate a caller-supplied name at all, so passing
        # one would be a name nothing uses.
        batch.create_foreign_key(
            None, "users", ["promoted_by_user_id"], ["id"], ondelete="SET NULL"
        )

    # --- 2. model_deployments --------------------------------------------
    op.create_table(
        "model_deployments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("model_version_id", sa.String(length=36), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("model_version_label", sa.String(length=16), nullable=True),
        sa.Column("strategy_key", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("timeframe", sa.String(length=4), nullable=True),
        # The three scope axes as one NOT NULL string, for the unique index
        # alone. NULLs are DISTINCT in a unique index, so an index over the
        # nullable columns would let any number of rows share the unrestricted
        # scope -- the most common one, so the guarantee would have been absent
        # exactly where it mattered most.
        sa.Column("scope_key", sa.String(length=112), nullable=False),
        sa.Column("environment", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(), nullable=True),
        sa.Column("activated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("previous_version_id", sa.String(length=36), nullable=True),
        sa.Column("config", _JSON, nullable=True),
        sa.CheckConstraint(_DEPLOYMENT_STATUSES, name="status"),
        sa.CheckConstraint(_ENVIRONMENTS, name="environment"),
        sa.CheckConstraint(
            "status = 'active' OR deactivated_at IS NOT NULL",
            name="an_ended_deployment_says_when",
        ),
        # RESTRICT, not CASCADE: deleting a version that something is deployed
        # on must fail loudly. §22 says a version is retired, never deleted, and
        # this is that rule enforced by the database rather than by convention.
        sa.ForeignKeyConstraint(
            ["model_version_id"], ["model_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["activated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_deployments_model_version_id", "model_deployments", ["model_version_id"])
    op.create_index("ix_model_deployments_model_key", "model_deployments", ["model_key"])
    op.create_index(
        "ix_model_deployments_scope", "model_deployments", ["model_key", "environment", "status"]
    )
    op.create_index(
        "uq_model_deployments_one_active_per_scope",
        "model_deployments",
        ["model_key", "scope_key", "environment"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    # --- 3. model_lifecycle_events ---------------------------------------
    op.create_table(
        "model_lifecycle_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model_version_id", sa.String(length=36), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("model_version_label", sa.String(length=16), nullable=True),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("environment", sa.String(length=8), nullable=True),
        sa.Column("validation_run_id", sa.String(length=36), nullable=True),
        sa.Column("deployment_id", sa.String(length=36), nullable=True),
        sa.Column("details", _JSON, nullable=True),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_lifecycle_events_model_version_id", "model_lifecycle_events", ["model_version_id"]
    )
    op.create_index("ix_model_lifecycle_events_model_key", "model_lifecycle_events", ["model_key"])
    op.create_index("ix_model_lifecycle_events_to_status", "model_lifecycle_events", ["to_status"])
    op.create_index("ix_model_lifecycle_events_occurred_at", "model_lifecycle_events", ["occurred_at"])
    op.create_index(
        "ix_model_lifecycle_events_version_at",
        "model_lifecycle_events",
        ["model_version_id", "occurred_at"],
    )
    op.create_index(
        "ix_model_lifecycle_events_model_at",
        "model_lifecycle_events",
        ["model_key", "occurred_at"],
    )


def downgrade() -> None:
    for name in (
        "ix_model_lifecycle_events_model_at",
        "ix_model_lifecycle_events_version_at",
        "ix_model_lifecycle_events_occurred_at",
        "ix_model_lifecycle_events_to_status",
        "ix_model_lifecycle_events_model_key",
        "ix_model_lifecycle_events_model_version_id",
    ):
        op.drop_index(name, table_name="model_lifecycle_events")
    op.drop_table("model_lifecycle_events")

    for name in (
        "uq_model_deployments_one_active_per_scope",
        "ix_model_deployments_scope",
        "ix_model_deployments_model_key",
        "ix_model_deployments_model_version_id",
    ):
        op.drop_index(name, table_name="model_deployments")
    op.drop_table("model_deployments")

    # Narrowing the vocabulary would strand any row holding one of the four new
    # values. Left as the reverse constraint because a downgrade is only ever
    # run against a database that never held them -- the same reasoning
    # migration 0017 records for its two widened columns.
    with op.batch_alter_table("model_versions") as batch:
        batch.drop_constraint("a_registered_version_names_its_validation", type_="check")
        batch.drop_constraint("a_registered_version_carries_its_digest", type_="check")
        batch.drop_constraint("status", type_="check")
        batch.create_check_constraint("status", _OLD_STATUSES)
        batch.drop_constraint("fk_model_versions_promoted_by_user_id_users", type_="foreignkey")

    for name in (
        "promoted_by_user_id",
        "retired_at",
        "promoted_at",
        "registered_at",
        "timeframe_scope",
        "symbol_scope",
        "validation_run_id",
        "training_run_id",
        "framework_version",
        "framework",
        "artifact_kind",
        "artifact_bytes",
        "artifact_sha256",
    ):
        op.drop_column("model_versions", name)
