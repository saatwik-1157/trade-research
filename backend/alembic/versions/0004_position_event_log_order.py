"""position_events gets an ordered primary key

Revision ID: 0004_position_event_log_order
Revises: 0003_symbol_specs
Create Date: 2026-09-02

Why this drops and recreates a table, which the migration rules otherwise
forbid:

`position_events` is an append-only audit log, and it was created at L05 with
a UUID primary key. A UUID provides no order, and `occurred_at` cannot supply
one either because several events in a single pass share the same instant by
construction - an exit decision and the fill that answers it are the same
moment. A log that cannot be replayed in the order it was written is not an
audit log.

Changing a primary key's type in place would need a USING clause and would
leave the old values meaningless anyway. The table is EMPTY in every
environment: it was introduced at L05 and nothing has written to it, because
the position manager that writes it is being added in this same level. The
downgrade restores the previous shape exactly.

Verified empty before this migration was written; the upgrade refuses to run
if that is ever untrue, rather than discarding rows someone else wrote.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_position_event_log_order"
down_revision: str | None = "0003_symbol_specs"
branch_labels: str | None = None
depends_on: str | None = None


def _refuse_if_not_empty() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT count(*) FROM position_events")).scalar_one()
    if count:
        raise RuntimeError(
            f"position_events holds {count} rows; this migration recreates the table and "
            "would discard them. Export them and re-import under the new key instead."
        )


def upgrade() -> None:
    _refuse_if_not_empty()
    op.drop_index(op.f("ix_position_events_occurred_at"), table_name="position_events")
    op.drop_index(op.f("ix_position_events_position_id"), table_name="position_events")
    op.drop_table("position_events")

    op.create_table(
        "position_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("position_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
            name=op.f("fk_position_events_position_id_positions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_events")),
    )
    op.create_index(
        op.f("ix_position_events_occurred_at"), "position_events", ["occurred_at"], unique=False
    )
    op.create_index(
        op.f("ix_position_events_position_id"), "position_events", ["position_id"], unique=False
    )


def downgrade() -> None:
    _refuse_if_not_empty()
    op.drop_index(op.f("ix_position_events_position_id"), table_name="position_events")
    op.drop_index(op.f("ix_position_events_occurred_at"), table_name="position_events")
    op.drop_table("position_events")

    op.create_table(
        "position_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("position_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["positions.id"],
            name=op.f("fk_position_events_position_id_positions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_events")),
    )
    op.create_index(
        op.f("ix_position_events_occurred_at"), "position_events", ["occurred_at"], unique=False
    )
    op.create_index(
        op.f("ix_position_events_position_id"), "position_events", ["position_id"], unique=False
    )
