"""The order lifecycle gets the columns it has been reconstructing (L19).

Four changes, all additive. No column is dropped, no type narrowed, no
existing row made invalid, and no historical order is touched.

**`orders.status` gains four states.** The eight it had could not express four
real outcomes:

  * `submitting` -- persisted BEFORE the venue call, so a crash between the
    send and the response leaves a row on disk saying so. Without it, a
    crashed submit is indistinguishable from a submit that never happened,
    which is the difference between "reconcile" and "safe to send".
  * `cancel_requested` -- "we asked" and "the venue agreed" were the same
    state. Assuming a cancellation succeeded is how a position nobody is
    watching stays open.
  * `expired` -- reached only when the venue confirms it.
  * `failed` -- the request never reached the venue. Deliberately NOT a
    synonym for `rejected`: a `failed` order is safe to re-send, a `rejected`
    one is not, and an `unknown` one must be reconciled first. Collapsing them
    loses exactly the distinction that decides whether a retry is safe.

**`orders` gains fill accounting and provenance.** `filled_quantity` starts at
0 for every existing row, which is correct rather than convenient: those rows
are imported history whose executions are already in `executions`, and the
service recomputes the figure from them. `average_fill_price` stays NULL for
the same reason -- an invented average would look exactly like a measured one.

`risk_decision_id` is nullable because the imported rows predate the Risk
Engine, and a backfilled id would be a reference to a decision nobody made.

**`orders` gains a filled-quantity CHECK.** A venue cannot have filled more
than was asked for, and a database that permits it is a database where an
overfill becomes history.

**`order_events` gains the fields an audit trail needs.** `previous_status`,
`new_status`, `source` and `broker_order_id` were reconstructible from the
sequence, which is not the same as recorded: a reader had to assume no event
was missing to know what a transition was from.

`sequence` is the one that turned out to be load-bearing. `occurred_at` cannot
order the trail — a submit that completes inside a single clock reading
produces four transitions with identical timestamps, and the primary key is a
uuid, so without a sequence the four are unorderable and the trail cannot be
read back. Existing rows keep NULL, which says "not recorded" rather than
inventing an order for them.

Revision ID: 0010_oms_lifecycle
Revises: 0009_risk_decisions
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_oms_lifecycle"
down_revision: str | Sequence[str] | None = "0009_risk_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = (
    "status IN ('intent','submitted','accepted','partially_filled','filled',"
    "'rejected','cancelled','unknown')"
)
NEW_STATUSES = (
    "status IN ('intent','submitting','submitted','accepted','partially_filled',"
    "'filled','cancel_requested','cancelled','rejected','expired','failed','unknown')"
)
TIF = "time_in_force IN ('gtc','ioc','fok','day')"
FILLED_QTY = "filled_quantity >= 0 AND filled_quantity <= quantity"


def upgrade() -> None:
    # The naming convention adds the ck_<table>_ prefix, so the bare name goes here.
    op.drop_constraint("status", "orders", type_="check")
    op.create_check_constraint("status", "orders", sa.text(NEW_STATUSES))

    # Fill accounting. `server_default` covers the existing rows; the model
    # default covers new ones. The default is dropped afterwards so the
    # application, not the database, decides what a new order starts at.
    op.add_column(
        "orders",
        sa.Column("filled_quantity", sa.Numeric(18, 8), nullable=False, server_default="0"),
    )
    op.alter_column("orders", "filled_quantity", server_default=None)
    op.add_column("orders", sa.Column("average_fill_price", sa.Numeric(18, 8), nullable=True))

    # Provenance and lifecycle stamps.
    op.add_column("orders", sa.Column("risk_decision_id", sa.String(36), nullable=True))
    op.add_column(
        "orders",
        sa.Column("time_in_force", sa.String(4), nullable=False, server_default="gtc"),
    )
    op.alter_column("orders", "time_in_force", server_default=None)
    op.add_column("orders", sa.Column("expires_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("acknowledged_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("filled_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("cancelled_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("reject_reason", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("error_code", sa.String(32), nullable=True))

    op.create_check_constraint("tif", "orders", sa.text(TIF))
    op.create_check_constraint("filled_qty", "orders", sa.text(FILLED_QTY))
    op.create_index("ix_orders_risk_decision_id", "orders", ["risk_decision_id"])

    # The audit trail.
    op.add_column("order_events", sa.Column("sequence", sa.Integer(), nullable=True))
    op.add_column("order_events", sa.Column("previous_status", sa.String(20), nullable=True))
    op.add_column("order_events", sa.Column("new_status", sa.String(20), nullable=True))
    op.add_column("order_events", sa.Column("source", sa.String(16), nullable=True))
    op.add_column("order_events", sa.Column("broker_order_id", sa.String(64), nullable=True))


def downgrade() -> None:
    for column in ("broker_order_id", "source", "new_status", "previous_status", "sequence"):
        op.drop_column("order_events", column)

    op.drop_index("ix_orders_risk_decision_id", table_name="orders")
    op.drop_constraint("filled_qty", "orders", type_="check")
    op.drop_constraint("tif", "orders", type_="check")
    for column in (
        "error_code",
        "reject_reason",
        "cancelled_at",
        "filled_at",
        "acknowledged_at",
        "expires_at",
        "time_in_force",
        "risk_decision_id",
        "average_fill_price",
        "filled_quantity",
    ):
        op.drop_column("orders", column)

    # Any order sitting in one of the four new states would violate the old
    # constraint. They are moved to `unknown` rather than guessed at: `unknown`
    # is the state whose whole meaning is "this must be established against the
    # venue", which is exactly true of them after a downgrade.
    op.execute(
        "UPDATE orders SET status = 'unknown' WHERE status IN "
        "('submitting','cancel_requested','expired','failed')"
    )
    op.drop_constraint("status", "orders", type_="check")
    op.create_check_constraint("status", "orders", sa.text(OLD_STATUSES))
