"""The bot lifecycle gets the states it has been faking (L22).

Two changes, both additive. No column is dropped, no type narrowed, no
existing row made invalid, and no run history is touched.

**`bot_runs.status` gains three states, and one of them fixes a defect.**

`PaperService.pause_bot` set the in-memory status to `paused` and wrote
**`stopping`** to this table, because the table had no value for it. So a
paused bot and a bot shutting down were the same row. After a restart nothing
could tell them apart, and a supervisor reading that row would either resume a
bot somebody had deliberately paused or abandon one that was only
mid-shutdown. Both are wrong; only one is visible.

  * `paused` — configured and deliberately not trading.
  * `recovering` — a supervisor is restarting it right now. Distinct from
    `crashed`, which means nobody is acting: only the first means an answer is
    coming.
  * `disabled` — prevented from running until explicitly re-enabled. Distinct
    from `stopped`, which anyone may start again.

The six existing states keep their own names. The brief calls one `ERROR`;
this project has always called it `crashed`, which is more specific and is
what every existing row says — renaming it would rewrite history to match a
document.

**`bots` gains the per-bot limits and the disable flag.** They live on the bot
rather than in the JSON `config` because a limit that only exists inside an
untyped blob cannot be queried, cannot be constrained, and cannot be shown to
have been applied. `config` keeps everything the runner needs; these five are
the ones a safety review asks about.

Every one is NULLABLE and every one means "not set" when null — a bot with no
`max_daily_trades` has no bot-level trade limit, and the RiskEngine's global
limits still apply. **A bot limit can only restrict**: the manager takes the
more restrictive of the bot's figure and the account's, so a bot asking for
more than its account permits gets the account's.

`is_disabled` is a separate boolean from `is_enabled`, which already exists
and means "this bot may be started". Disabling is a stronger statement made by
an operator, and folding the two would make "somebody turned this off after an
incident" indistinguishable from "this bot has never been switched on".

Revision ID: 0012_bot_lifecycle
Revises: 0011_position_lifecycle
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_bot_lifecycle"
down_revision: str | Sequence[str] | None = "0011_position_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "status IN ('starting','running','stopping','stopped','crashed','halted')"
NEW_STATUSES = (
    "status IN ('starting','running','paused','stopping','stopped','crashed',"
    "'recovering','halted','disabled')"
)


def upgrade() -> None:
    # The naming convention adds the ck_<table>_ prefix, so the bare name goes here.
    op.drop_constraint("status", "bot_runs", type_="check")
    op.create_check_constraint("status", "bot_runs", sa.text(NEW_STATUSES))

    # Per-bot limits. Nullable, and null means "no bot-level limit" -- the
    # RiskEngine's global limits still apply either way.
    op.add_column("bots", sa.Column("max_positions", sa.Integer(), nullable=True))
    op.add_column("bots", sa.Column("max_daily_trades", sa.Integer(), nullable=True))
    op.add_column("bots", sa.Column("max_daily_loss", sa.Numeric(18, 8), nullable=True))
    op.add_column("bots", sa.Column("max_risk_per_trade", sa.Numeric(18, 8), nullable=True))
    op.add_column("bots", sa.Column("cooldown_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "bots",
        sa.Column("is_disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("bots", "is_disabled", server_default=None)
    op.add_column("bots", sa.Column("disabled_reason", sa.Text(), nullable=True))
    op.add_column("bots", sa.Column("disabled_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for column in (
        "disabled_at",
        "disabled_reason",
        "is_disabled",
        "cooldown_seconds",
        "max_risk_per_trade",
        "max_daily_loss",
        "max_daily_trades",
        "max_positions",
    ):
        op.drop_column("bots", column)

    # A run in one of the three new states would violate the old constraint.
    # `paused` and `recovering` both mean the run is still going, and the old
    # vocabulary's word for that is `stopping` -- which is exactly the
    # conflation this migration existed to end, so the downgrade restores it
    # rather than pretending the information can be kept. `disabled` becomes
    # `stopped`: the run is over either way, and the `bots.is_disabled` flag
    # that carried the distinction is being dropped in the same breath.
    op.execute("UPDATE bot_runs SET status = 'stopping' WHERE status IN ('paused','recovering')")
    op.execute("UPDATE bot_runs SET status = 'stopped' WHERE status = 'disabled'")
    op.drop_constraint("status", "bot_runs", type_="check")
    op.create_check_constraint("status", "bot_runs", sa.text(OLD_STATUSES))
