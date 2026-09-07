"""Positions gain the states and the history a partial close needs (L21).

Three changes, all additive. No column is dropped, no type narrowed, no
existing row made invalid, and no historical position is touched.

**`positions.status` gains four states.** The three it had could not express
four real situations:

  * `opening` -- an order is in flight and nothing has confirmed. A position
    that exists locally before the venue confirms is a position that may not
    exist at all, and `open` claimed it did.
  * `partially_closed` -- some of it was taken off. Distinct from `open`
    because the remaining size is no longer the size that was risk-sized.
  * `closing` -- a close was requested and the venue has not confirmed it.
    "We asked" and "it is closed" are different facts, and collapsing them is
    how a position nobody is watching stays open.
  * `reconciling` -- being settled against the venue right now. Deliberately
    distinct from `unknown`: `unknown` means nobody is looking, this means
    somebody is, and only the second means an answer is coming.

**`positions` gains the history a partial close cuts from.**
`quantity` is what is OPEN now; `initial_quantity` and `closed_quantity` are
what it was and what has gone. They are separate columns because a partial
close must not overwrite the original: "70 open" and "100 opened, 30 closed"
are different facts, and only the second can be audited.

Existing rows are backfilled with `initial_quantity = quantity` and
`closed_quantity = 0`, which is exactly true of a position that has never been
partially closed -- and every existing row is one, because the mechanism did
not exist.

**`positions` gains what the VENUE says its protective levels are.**
`broker_stop_loss` and `broker_take_profit` are separate from `stop_loss` and
`take_profit`, which are what this platform INTENDS. A stop we asked for and a
stop the venue is holding are different facts; a silent disagreement between
them is a position running unprotected while the record says otherwise, and
surfacing that is most of what L21 exists to do. They start NULL, which says
"never read from the venue" rather than inventing agreement.

`realized_pnl` is nullable and starts NULL for the same reason. Unrealised
P&L is deliberately NOT a column: it is a function of a price that changes
every tick, and a stored one is wrong the moment it is written.

Revision ID: 0011_position_lifecycle
Revises: 0010_oms_lifecycle
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_position_lifecycle"
down_revision: str | Sequence[str] | None = "0010_oms_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "status IN ('open','closed','unknown')"
NEW_STATUSES = (
    "status IN ('opening','open','partially_closed','closing','closed','unknown','reconciling')"
)
CLOSED_QTY = "closed_quantity >= 0 AND closed_quantity <= initial_quantity"


def upgrade() -> None:
    # The naming convention adds the ck_<table>_ prefix, so the bare name goes here.
    op.drop_constraint("status", "positions", type_="check")
    op.create_check_constraint("status", "positions", sa.text(NEW_STATUSES))

    op.add_column(
        "positions",
        sa.Column("initial_quantity", sa.Numeric(18, 8), nullable=False, server_default="0"),
    )
    op.add_column(
        "positions",
        sa.Column("closed_quantity", sa.Numeric(18, 8), nullable=False, server_default="0"),
    )
    # Backfill: every existing row is a position that has never been partially
    # closed, because the mechanism did not exist. So its initial size IS its
    # current size, and that is a measurement rather than an assumption.
    op.execute("UPDATE positions SET initial_quantity = quantity")
    op.alter_column("positions", "initial_quantity", server_default=None)
    op.alter_column("positions", "closed_quantity", server_default=None)

    op.add_column("positions", sa.Column("realized_pnl", sa.Numeric(18, 8), nullable=True))
    op.add_column("positions", sa.Column("broker_stop_loss", sa.Numeric(18, 8), nullable=True))
    op.add_column("positions", sa.Column("broker_take_profit", sa.Numeric(18, 8), nullable=True))
    op.add_column("positions", sa.Column("broker_synced_at", sa.DateTime(), nullable=True))

    op.create_check_constraint("closed_qty", "positions", sa.text(CLOSED_QTY))


def downgrade() -> None:
    op.drop_constraint("closed_qty", "positions", type_="check")
    for column in (
        "broker_synced_at",
        "broker_take_profit",
        "broker_stop_loss",
        "realized_pnl",
        "closed_quantity",
        "initial_quantity",
    ):
        op.drop_column("positions", column)

    # A position in one of the four new states would violate the old
    # constraint. `opening`, `closing` and `reconciling` all mean "the venue's
    # answer is not settled", which is exactly what `unknown` means, so they
    # go there. `partially_closed` is genuinely open, and goes to `open` --
    # the remaining quantity is already in `quantity`, so nothing is lost
    # except the fact that it was cut down, which the column being dropped
    # carried anyway.
    op.execute(
        "UPDATE positions SET status = 'unknown' "
        "WHERE status IN ('opening','closing','reconciling')"
    )
    op.execute("UPDATE positions SET status = 'open' WHERE status = 'partially_closed'")
    op.drop_constraint("status", "positions", type_="check")
    op.create_check_constraint("status", "positions", sa.text(OLD_STATUSES))
