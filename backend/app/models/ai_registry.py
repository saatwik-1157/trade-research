"""model_deployments and model_lifecycle_events: what is active where, and why.

Two tables, and §47 is explicit that no second model or version table may
appear — so neither of these holds a model, a version, an artifact or a metric.
They reference `model_versions`, which has been the one table since L05.

**`model_deployments` answers "which version does this scope resolve to?"**
Separately from `model_versions.status`, and the separation is the point: a
version's status says what the version IS, a deployment says where it is USED.
One version can be active on one strategy and superseded on another, and a
single column on the version could not say that.

**A partial unique index enforces §14.** At most one `active` deployment per
`(model_key, strategy_key, symbol, timeframe, environment)`. Two administrators
promoting at once produce one winner and one `IntegrityError`, which §31 asks
for and which a `SELECT`-then-`INSERT` could not give.

**`model_lifecycle_events` is the audit trail of §19 and §35.** Append-only by
construction — there is no update path in the service and no column that would
invite one. It records both halves of every transition, so "who made v2.3 active
and what was active before" is one row rather than an inference from timestamps.

**Nothing here is ever deleted.** Section 22. A retired version keeps its rows,
because audit, backtesting, trade review, reproducibility and rollback all need
them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import MODE_CHECK, CreatedMixin, IdMixin, JSONType

# Where a deployment applies. The same three modes every execution-side table
# uses, so a paper deployment and a live one can never be pooled by omission.
ENVIRONMENTS = ("paper", "demo", "live")

# What a deployment row says about itself.
#
# `superseded` and `rolled_back` are separate: one means a newer version took
# over in the ordinary way, the other means this one was pulled because
# something was wrong. An operator reading the history a month later needs to
# tell them apart, and a single `inactive` would not let them.
DEPLOYMENT_STATUSES = ("active", "superseded", "rolled_back", "stopped")


class ModelDeployment(IdMixin, CreatedMixin, Base):
    """One version, deployed to one scope, in one environment."""

    __tablename__ = "model_deployments"
    __table_args__ = (
        CheckConstraint("status IN ('" + "','".join(DEPLOYMENT_STATUSES) + "')", name="status"),
        CheckConstraint(MODE_CHECK.replace("mode", "environment"), name="environment"),
        # A deployment that is no longer active says when it stopped being so.
        # Without this, "active" and "ended at a time nobody recorded" are the
        # same row, and the second reads as the first.
        CheckConstraint(
            "status = 'active' OR deactivated_at IS NOT NULL",
            name="an_ended_deployment_says_when",
        ),
        # §14, enforced by the database rather than by a check-then-write.
        #
        # Over `scope_key`, NOT over the nullable scope columns. **NULLs are
        # DISTINCT in a unique index**, so an index over
        # `(strategy_key, symbol, timeframe)` would let any number of rows share
        # the unrestricted scope -- which is the most common one, so the
        # guarantee would have been absent exactly where it was most needed and
        # present everywhere it was easy to test. Found by the test that tried
        # to insert a second active deployment and got no error.
        #
        # `postgresql_where` / `sqlite_where` make it PARTIAL: many superseded
        # rows may share a scope, exactly one active row may.
        Index(
            "uq_model_deployments_one_active_per_scope",
            "model_key",
            "scope_key",
            "environment",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_model_deployments_scope", "model_key", "environment", "status"),
    )

    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Denormalised so the unique index can cover the scope without a join. The
    # version's own model is authoritative; a test asserts they agree.
    model_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_version_label: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # The scope. NULL in any of these means "not restricted on this axis" --
    # which is a recorded absence, not a wildcard somebody typed. Empty strings
    # are deliberately not used: '' and NULL would be two spellings of one fact
    # and the unique index would treat them as different scopes.
    strategy_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timeframe: Mapped[str | None] = mapped_column(String(4), nullable=True)
    environment: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")

    # The three scope axes as one NOT NULL string, for the unique index alone.
    # Derived and written by `registry_service.Scope.key()`, never by a caller.
    # It exists because NULLs are distinct in a unique index: without it, the
    # "one active per scope" rule would not hold for the unrestricted scope.
    # The nullable columns above stay as they are -- they are what a reader and
    # a query use, and "not restricted" must remain expressible as NULL.
    scope_key: Mapped[str] = mapped_column(String(112), nullable=False, default="|||")

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    activated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    activated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What this deployment replaced, so a rollback has something to name. Not a
    # foreign key to `model_deployments` because the row it points at is in the
    # same table and a cycle of RESTRICTs is a migration nobody can run.
    previous_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Inference parameters for THIS deployment: a threshold, allowed regimes.
    # §17 keeps them apart from the artifact and from the strategy's own AI
    # configuration -- three things that change for different reasons.
    config: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


class ModelLifecycleEvent(IdMixin, Base):
    """One status transition. Append-only; §19 and §35."""

    __tablename__ = "model_lifecycle_events"
    __table_args__ = (
        Index("ix_model_lifecycle_events_version_at", "model_version_id", "occurred_at"),
        Index("ix_model_lifecycle_events_model_at", "model_key", "occurred_at"),
    )

    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_version_label: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Both halves. `from_status` is nullable only for the first event of a
    # version's life, where there is genuinely nothing before it.
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Required in the service, nullable here only because a row written by a
    # future migration should not be impossible. Every path in `lifecycle`
    # supplies one.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(String(8), nullable=True)
    validation_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
