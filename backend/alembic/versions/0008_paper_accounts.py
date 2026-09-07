"""paper_accounts gains a lifecycle and its P&L (L16).

The table already carried the money -- `starting_balance`, `balance`,
`equity` -- and one boolean, `is_active`. A boolean cannot distinguish three
states that authorise different operations:

  * `created`  never activated. Not a trading venue yet.
  * `paused`   activated, temporarily not accepting orders. Positions stand.
  * `disabled` stopped by an admin. Only closure follows.

All three read `is_active = false`, so before this migration "why can this
account not trade?" had no answer in the data.

`status` is added with a default of `active` for **existing** rows, chosen from
`is_active` where it is set, because an account that has been trading is
active by observation. New rows default to `created`; that is the model's
default, not the column's, so a backfilled row and a fresh one cannot be
confused.

`is_active` is KEPT and kept in step with `status`. Dropping it would break any
reader written against it for the sake of tidiness, and the level's brief is
explicit that working functionality is not removed.

The P&L columns are added because the account is the thing a user reads, and
recomputing realised P&L from the trade table on every read would give a figure
that could disagree with the balance -- two numbers for one fact.

Purely additive: no column is dropped, no type is narrowed, no existing value
becomes invalid, and every new column has a default so existing rows are valid
the moment they are written.

Revision ID: 0008_paper_accounts
Revises: 0007_replay_paused
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_paper_accounts"
down_revision: str | Sequence[str] | None = "0007_replay_paused"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = "status IN ('created','active','paused','disabled','closed')"

MONEY = sa.Numeric(18, 4)


def upgrade() -> None:
    op.add_column(
        "paper_accounts",
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
    )
    op.add_column(
        "paper_accounts",
        sa.Column("realized_pnl", MONEY, nullable=False, server_default="0"),
    )
    op.add_column(
        "paper_accounts",
        sa.Column("unrealized_pnl", MONEY, nullable=False, server_default="0"),
    )
    op.add_column(
        "paper_accounts",
        sa.Column("commission_paid", MONEY, nullable=False, server_default="0"),
    )
    op.add_column("paper_accounts", sa.Column("reset_at", sa.DateTime(), nullable=True))
    op.add_column(
        "paper_accounts",
        sa.Column("reset_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # An account that was already active is active; one that was not is paused
    # rather than created, because `created` claims it never ran and only the
    # flag can be read here, not the history.
    op.execute(
        "UPDATE paper_accounts SET status = CASE WHEN is_active THEN 'active' ELSE 'paused' END"
    )
    op.create_check_constraint("status", "paper_accounts", sa.text(STATUS))


def downgrade() -> None:
    # The naming convention adds the ck_<table>_ prefix, so the bare name goes here.
    op.drop_constraint("status", "paper_accounts", type_="check")
    for column in (
        "reset_count",
        "reset_at",
        "commission_paid",
        "unrealized_pnl",
        "realized_pnl",
        "status",
    ):
        op.drop_column("paper_accounts", column)
