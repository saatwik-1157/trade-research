"""Trade attribution and lifecycle on `trades` (L31).

**No new table, and nothing existing is altered destructively.**

`trades` has held the completed round trip since L05. L31 does not replace it,
does not shadow it and does not add a second history — section 3 and section 53
both say to audit before building, and the audit found the record already here
with 252 rows in it. What it could not do was say *why* the trade happened, so
this migration adds the references that answer that:

    status                  section 15. `closed` for a finished episode;
                            `reconciliation_required` when the platform and the
                            venue disagree, which is never resolved by guessing.

    broker_account_id       section 30. Two columns, never one nullable
    paper_account_id        `account_id`. Carried on the trade rather than
                            reached through the position, because a position row
                            can be deleted and the historical trade must still
                            say which account it belonged to.

    order_id                sections 6, 8, 9 and 10. Every one is a REFERENCE to
    signal_id               the row that already holds the fact. Section 31 says
    ai_decision_id          not to duplicate Order, Signal or AIInference, and a
    risk_event_id           reference cannot drift from what it points at, which
    bot_id                  a copied column eventually does.

    exit_reason             section 17. L21's vocabulary, beside -- not instead
                            of -- the broker's numeric `close_reason`. They are
                            different facts and can disagree.

    requested_entry_price   sections 12 and 16. `CLAUDE.md` records the live
    entry_slippage_points   trade whose 279-point gap stayed invisible for three
                            days because the log kept the requested price as the
                            entry. Stored, because section 32 makes a completed
                            trade a historical fact and the orders behind it can
                            be archived.

    fees                    section 18. Distinct from `commission`: MT5 reports
                            both, and folding one into the other makes the total
                            right and each part wrong.

    currency                section 19. NULL means the account currency was not
                            recorded -- a gap, not a default.

    data_quality            section 44. NULL means the checks have not run; `[]`
                            means they ran and found nothing. Different facts.

**Every column is nullable.** The 252 imported rows keep meaning exactly what
they meant: `status` takes a server default of `closed`, which is true of every
one of them, and every other column stays NULL rather than being backfilled with
a value nobody measured.

Three indexes:

    UNIQUE (position_id) WHERE position_id IS NOT NULL
        Section 27. One journal row per position episode, as a database
        guarantee rather than a check the writer has to remember -- a duplicate
        fill or close event must not produce a second trade.

        NULLs are DISTINCT in a unique index, which migration 0020 learned the
        hard way. Here that is the WANTED behaviour: the imported rows carry no
        `position_id`, they are not position episodes, and a constraint that
        collapsed them into one would destroy the record.

    (paper_account_id, closed_at) and (broker_account_id, closed_at)
        Section 46. The journal is read filtered by account and ordered by date,
        and it is the table that grows without bound.

Revision ID: 0022_trade_journal
Revises: 0021_model_monitoring
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_trade_journal"
down_revision: str | None = "0021_model_monitoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (name, type, nullable) for every column this migration adds.
COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("broker_account_id", sa.String(length=36)),
    ("paper_account_id", sa.String(length=36)),
    ("order_id", sa.String(length=36)),
    ("signal_id", sa.String(length=36)),
    ("ai_decision_id", sa.String(length=36)),
    ("risk_event_id", sa.String(length=36)),
    ("bot_id", sa.String(length=36)),
    ("exit_reason", sa.String(length=32)),
    ("requested_entry_price", sa.Numeric(18, 8)),
    ("entry_slippage_points", sa.Numeric(12, 6)),
    ("fees", sa.Numeric(18, 4)),
    ("currency", sa.String(length=8)),
)

#: (constraint name, local column, referred table). Bare names: the naming
#: convention interpolates the rest, and passing a pre-prefixed one produces
#: `fk_trades_fk_trades_...`. Three migrations have now made that mistake.
FOREIGN_KEYS: tuple[tuple[str, str], ...] = (
    ("broker_account_id", "broker_accounts"),
    ("paper_account_id", "paper_accounts"),
    ("order_id", "orders"),
    ("signal_id", "signals"),
    ("ai_decision_id", "ai_decisions"),
    ("risk_event_id", "risk_events"),
    ("bot_id", "bots"),
)


def upgrade() -> None:
    for name, kind in COLUMNS:
        op.add_column("trades", sa.Column(name, kind, nullable=True))

    op.add_column(
        "trades",
        sa.Column(
            "data_quality",
            sa.JSON().with_variant(
                sa.dialects.postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )

    # `status` is NOT NULL with a server default, because every existing row is
    # a closed round trip and saying so is a measurement rather than a guess.
    # The server default stays on the column: a writer that forgets it produces
    # a correct row rather than an integrity error, and the CHECK below is what
    # stops a wrong one.
    op.add_column(
        "trades",
        sa.Column("status", sa.String(length=24), nullable=False, server_default="closed"),
    )
    op.create_check_constraint(
        "status",
        "trades",
        "status IN ('open','partially_closed','closed','reconciliation_required','unknown')",
    )

    for column, referred in FOREIGN_KEYS:
        op.create_foreign_key(None, "trades", referred, [column], ["id"], ondelete="SET NULL")

    op.create_index(
        "uq_trades_position_id",
        "trades",
        ["position_id"],
        unique=True,
        postgresql_where=sa.text("position_id IS NOT NULL"),
        sqlite_where=sa.text("position_id IS NOT NULL"),
    )
    op.create_index("ix_trades_paper_account_closed", "trades", ["paper_account_id", "closed_at"])
    op.create_index("ix_trades_broker_account_closed", "trades", ["broker_account_id", "closed_at"])


def downgrade() -> None:
    op.drop_index("ix_trades_broker_account_closed", table_name="trades")
    op.drop_index("ix_trades_paper_account_closed", table_name="trades")
    op.drop_index("uq_trades_position_id", table_name="trades")

    for column, referred in FOREIGN_KEYS:
        op.drop_constraint(f"fk_trades_{column}_{referred}", "trades", type_="foreignkey")

    # BARE name. The naming convention interpolates the rest, and a
    # pre-prefixed one produces `ck_trades_ck_trades_status`. Fourth time this
    # repository has hit it, and the first on a DROP rather than a CREATE: the
    # rule is every constraint kind, both directions, bare name, always.
    op.drop_constraint("status", "trades", type_="check")
    op.drop_column("trades", "status")
    op.drop_column("trades", "data_quality")
    for name, _kind in reversed(COLUMNS):
        op.drop_column("trades", name)
