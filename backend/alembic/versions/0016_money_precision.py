"""Three Money columns get the scale their models declare (L25 verification).

**Found by running the migrations against a real PostgreSQL for the first
time.** `tests/test_migrations.py` asserts zero autogenerate drift between the
models and the schema the migrations build, and it is skipped without a
disposable database — so from L21 until now it had never actually run. The
first execution reported three columns:

    bots.max_daily_loss        NUMERIC(18,8) -> Numeric(18,4)
    bots.max_risk_per_trade    NUMERIC(18,8) -> Numeric(18,4)
    positions.realized_pnl     NUMERIC(18,8) -> Numeric(18,4)

All three are declared `Money` in the models (`Numeric(18, 4)`), and all three
were added as `Numeric(18, 8)` by `0011_position_lifecycle` and
`0012_bot_lifecycle`. `Money` and `Price`/`Qty` are different types in
`app/models/base.py` for a reason — money carries four decimal places and a
price eight — and a column that stores eight where its model expects four is
the same class of mistake as the unit errors this project documents elsewhere:
harmless until something compares two of them.

**Corrected forward rather than by editing history.** The two migrations that
introduced it have never been applied anywhere — this repository's development
database is at `0009_risk_decisions` — so rewriting them would have worked. It
is still the wrong move: a migration that has been published is a fact about
what some database may already contain, and correcting forward is right whether
or not that is true here.

**Lossless in practice.** Both columns were introduced by the migrations that
got the type wrong, so nothing has ever written to them. On a populated column
PostgreSQL rounds on the cast, which is the semantic `Money` intends anyway.

Revision ID: 0016_money_precision
Revises: 0015_training_jobs
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_money_precision"
down_revision: str | Sequence[str] | None = "0015_training_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(18, 4)
WRONG = sa.Numeric(18, 8)

COLUMNS: tuple[tuple[str, str], ...] = (
    ("bots", "max_daily_loss"),
    ("bots", "max_risk_per_trade"),
    ("positions", "realized_pnl"),
)


def upgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(table, column, existing_type=WRONG, type_=MONEY, existing_nullable=True)


def downgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(table, column, existing_type=MONEY, type_=WRONG, existing_nullable=True)
