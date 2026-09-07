"""audit_logs: an environment column and the indexes section 35 filters on (L36).

**One table altered. Nothing is created, nothing is dropped, no row is
deleted.**

L36 audited the existing administration surface and found `audit_logs` already
carried who, what, which resource, when, from where and the request id, written
by `app.core.audit.record` since L04 and read by `GET /v1/admin/audit-logs`
since L06. Section 31 asks for an `admin_audit_logs` table; section 51 says not
to create duplicates. This table IS the admin audit trail, so it is extended
rather than shadowed by a second one with a better name -- a platform with two
audit tables has two partial answers to "what happened".

Two things are added:

    environment
        Section 35 lists it as a FILTER. A key inside the `details` JSON is not
        one -- filtering on it would be a scan of every row in a table that
        grows without bound. NULL means "not about a trading environment": a
        role change is neither paper nor live, and stamping it would say
        something untrue. Same rule as `notifications.environment` (0024).

    two composite indexes
        (actor_user_id, occurred_at) and (action, occurred_at). The trail is
        read newest-first, narrowed by actor or by action, and both were index
        scans on one column followed by a sort.

`resource_type` also gains its own index: it is the third filter the route
offers and was the only one without one.

**What is deliberately NOT here.** No `severity`. An audit row records that
something happened; grading how bad it was is a monitoring judgement, it would
be assigned by the same code that writes the row, and a field whose value is
always whatever the writer felt at the time is not a filter anybody can trust.
Section 35 lists it; L36 declines it and says so rather than adding a column
that would be uniformly "info".

**No down-migration data loss.** The downgrade drops what the upgrade added and
nothing else. Rows written with an environment lose that one field; every other
column, and every row, survives.

Revision ID: 0025_admin_audit
Revises: 0024_notifications
Create Date: 2026-09-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_admin_audit"
down_revision: str | None = "0024_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENVIRONMENTS = ("backtest", "paper", "demo", "live", "unknown")


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("environment", sa.String(length=8), nullable=True))
    with op.batch_alter_table("audit_logs") as batch:
        batch.create_check_constraint(
            "environment",
            "environment IS NULL OR environment IN ('" + "','".join(ENVIRONMENTS) + "')",
        )
    op.create_index("ix_audit_logs_environment", "audit_logs", ["environment"])
    op.create_index("ix_audit_logs_resource_type", "audit_logs", ["resource_type"])
    op.create_index("ix_audit_logs_actor_occurred", "audit_logs", ["actor_user_id", "occurred_at"])
    op.create_index("ix_audit_logs_action_occurred", "audit_logs", ["action", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_action_occurred", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_occurred", table_name="audit_logs")
    op.drop_index("ix_audit_logs_resource_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_environment", table_name="audit_logs")
    with op.batch_alter_table("audit_logs") as batch:
        # `op.f(...)`: the name is already final. Without it the metadata
        # convention `ck_%(table_name)s_%(constraint_name)s` prefixes it a
        # second time and this looks for
        # `ck_audit_logs_ck_audit_logs_environment`. The same defect as
        # migration 0024, found in the same L40 run and for the same reason:
        # SQLite rebuilds the table in batch mode and never notices, and
        # PostgreSQL refuses.
        batch.drop_constraint(op.f("ck_audit_logs_environment"), type_="check")
    op.drop_column("audit_logs", "environment")
