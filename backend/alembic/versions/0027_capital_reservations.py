"""capital_reservations: risk budget that survives a restart (L54/L55).

**One table added. Nothing altered, nothing dropped, no row deleted.**

`RiskService` has held reservations since L17 and holds them in a process-local
dict. That is correct for the race it was built for -- two concurrent orders
evaluated against one portfolio snapshot both seeing the same headroom -- and
it is empty after a restart.

An approval that reserved budget and had not yet filled when the process died
releases nothing, because there is nothing left to release. The next process
starts believing the whole budget is free, while the venue may be holding the
order the reservation was for.

That is the same shape as the L45 C-1 defect this project fixed the same week:
**a guard whose state does not outlive the process that made it.** L54 step 6
says it in one line -- "do not rely solely on in-memory state".

**This does not replace the dict.** The dict stays as the fast path inside one
process; this is the record that survives a restart, and the dict is rebuilt
FROM it rather than kept beside it. Two derivations of one fact can disagree,
and then neither is trustworthy.

**`intent_id` is UNIQUE**, which is what makes a retried signal reserve once
rather than twice -- the same backstop `orders.intent_id` already provides for
orders, for the same reason and with the same enforcement.

**The CHECK constraints are the interesting part.** `risk_amount >= 0` and
`exposure >= 0` exist because a negative claim on a budget would ADD budget.
That is the one direction this table must never permit, and a constraint is a
better guarantee of it than every caller remembering.

Revision ID: 0027_capital_reservations
Revises: 0026_bot_bracket_source
Create Date: 2026-09-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_capital_reservations"
down_revision: str | None = "0026_bot_bracket_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("pending", "active", "consumed", "released", "expired", "cancelled", "rejected")


def upgrade() -> None:
    op.create_table(
        "capital_reservations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("intent_id", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False, server_default="paper"),
        sa.Column("strategy_id", sa.String(length=64), nullable=True),
        sa.Column("bot_id", sa.String(length=36), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("risk_amount", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("exposure", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("positions", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="active"),
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("order_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        # SHORT names. The metadata naming convention is
        # `ck_%(table_name)s_%(constraint_name)s`, so a name that already
        # carries the table prefix comes out doubled --
        # `ck_capital_reservations_ck_capital_reservations_exposure` -- and
        # then the migration and the ORM model disagree about what the
        # constraint is called. Migration 0025 records the same defect from the
        # other end, where a doubled name made a downgrade fail.
        sa.CheckConstraint("status IN ('" + "','".join(STATUSES) + "')", name="status"),
        # A negative claim would ADD budget. Constrained rather than trusted.
        sa.CheckConstraint("risk_amount >= 0", name="risk_amount"),
        sa.CheckConstraint("exposure >= 0", name="exposure"),
        sa.CheckConstraint("positions >= 0", name="positions"),
    )
    # One reservation per intent: a retried signal reserves once.
    op.create_index(
        "ux_capital_reservations_intent", "capital_reservations", ["intent_id"], unique=True
    )
    op.create_index(
        "ix_capital_reservations_account_status",
        "capital_reservations",
        ["account_id", "status"],
    )
    op.create_index(
        "ix_capital_reservations_expires", "capital_reservations", ["expires_at"]
    )
    op.create_index(
        "ix_capital_reservations_decision_id", "capital_reservations", ["decision_id"]
    )
    op.create_index(
        "ix_capital_reservations_strategy_id", "capital_reservations", ["strategy_id"]
    )
    op.create_index("ix_capital_reservations_bot_id", "capital_reservations", ["bot_id"])
    op.create_index("ix_capital_reservations_symbol", "capital_reservations", ["symbol"])
    op.create_index("ix_capital_reservations_order_id", "capital_reservations", ["order_id"])


def downgrade() -> None:
    op.drop_table("capital_reservations")
