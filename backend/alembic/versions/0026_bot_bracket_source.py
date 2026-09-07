"""bots.use_alert_bracket: the one link the TradingView chain still lacked (L45 F-1).

**One column added to one table. Nothing created, nothing dropped, no row
deleted, and no existing behaviour changed** -- the column defaults to false,
which is exactly what every bot does today.

L45's F-1 finding was that the TradingView chain had never been able to
complete. Four of its five missing links were facts the platform already held
and simply never recorded, and those were fixed by writing them into the
signal's `meta`. The fifth is not a fact; it is a policy, and it is this:

    `fixed_risk` sizing needs a STOP DISTANCE and does not invent one.

Nothing on the webhook path produces a bracket. The stop and target an alert
suggests are quarantined under `meta["advisory_ignored"]` on purpose --
`meta["stop_loss"]` is meant to be the PLATFORM'S bracket, a distinction
`test_the_advisory_from_an_alert_is_carried_and_not_obeyed` pins -- so a signal
reaching sizing is refused with "stop distance is required and must be
positive".

**Why the alert's own bracket, and why it must be opted into.** An ATR-derived
bracket is the better answer and it cannot be computed: `market_bars` covers one instrument,
so there is no volatility series to derive one from. That leaves the alert's
suggestion as the only bracket that exists at all today.

Obeying it silently would be wrong twice over. It would collapse a quarantine
the design built deliberately, and under `fixed_risk` a TIGHTER stop produces a
LARGER position -- so an external sender would gain an input that moves size
upward, which is the direction that matters.

So it is per-bot, explicit, and **false by default**. An operator turns it on
for one bot at a time, and the signal records `bracket_source` so a trade can
always be traced to where its stop came from. `max_risk_per_trade` still bounds
the money at risk, the RiskEngine still evaluates the resulting order and can
veto it, and `TRADING_MODE`/`LIVE_TRADING` are untouched.

**A boolean column rather than a key in `bots.config`.** `app/bots/limits.py`
states the rule this follows: a limit that exists only in an untyped blob
cannot be queried, constrained, or shown to have been applied. Where the stop
on a live order came from is exactly the kind of thing somebody will need to
query after an incident.

**No down-migration data loss.** The downgrade drops the column it added and
nothing else. A bot that had opted in loses that one flag -- and loses it in
the safe direction, back to the platform refusing to trade without a bracket.

Revision ID: 0026_bot_bracket_source
Revises: 0025_admin_audit
Create Date: 2026-09-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_bot_bracket_source"
down_revision: str | None = "0025_admin_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A server default so rows that already exist get false rather than NULL.
    # A NULL here would be a third state -- "nobody decided" -- and the only
    # safe reading of it is false anyway, so it is written down instead of
    # inferred on every read.
    #
    # `sa.false()`, not `sa.text("0")`. SQLite accepts the integer and
    # PostgreSQL refuses it:
    #
    #     column "use_alert_bracket" is of type boolean but default
    #     expression is of type integer
    #
    # The migration tests skip without a scratch PostgreSQL, so the first
    # thing to catch this was applying it to the running database. `sa.false()`
    # renders the right literal for whichever dialect is asked.
    op.add_column(
        "bots",
        sa.Column(
            "use_alert_bracket",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("bots", "use_alert_bracket")
