"""replay_sessions may be paused (L15).

Purely additive to a vocabulary. `replay_sessions.status` allowed
queued/running/finished/failed/cancelled; pausing is the whole point of an
interactive replay and had nowhere to live, so `paused` is added to the CHECK
constraint.

No column is altered, no row is rewritten and no row can become invalid: every
existing value is still permitted, so this widens rather than narrows. There is
therefore no destructive step and no guard is needed to refuse one.

`backtests` is deliberately left alone. A backtest is a batch run with no
interactive state, and adding `paused` there would offer a state nothing can
reach.

Revision ID: 0007_replay_paused
Revises: 0006_market_bars
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_replay_paused"
down_revision: str | Sequence[str] | None = "0006_market_bars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "status IN ('queued','running','finished','failed','cancelled')"
NEW = "status IN ('queued','running','paused','finished','failed','cancelled')"


def upgrade() -> None:
    # The naming convention adds the ck_<table>_ prefix, so the bare name is
    # what is passed here.
    op.drop_constraint("status", "replay_sessions", type_="check")
    op.create_check_constraint("status", "replay_sessions", sa.text(NEW))


def downgrade() -> None:
    """Narrows the vocabulary again.

    A paused session would violate the old constraint, so any that exist are
    moved to `cancelled` first -- a paused replay that can no longer be
    described as paused is stopped, not silently reported as finished.
    """
    op.execute("UPDATE replay_sessions SET status = 'cancelled' WHERE status = 'paused'")
    # The naming convention adds the ck_<table>_ prefix, so the bare name is
    # what is passed here.
    op.drop_constraint("status", "replay_sessions", type_="check")
    op.create_check_constraint("status", "replay_sessions", sa.text(OLD))
