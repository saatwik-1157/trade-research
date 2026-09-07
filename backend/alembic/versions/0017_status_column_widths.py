"""Two status columns are too narrow for the values they already declare.

**Found by running the platform against PostgreSQL for the first time.** SQLite
does not enforce `VARCHAR(n)`; PostgreSQL does. So every test passed, and both
of these would have failed the first time a real deployment reached the state.

    positions.status       VARCHAR(8)  <- 'partially_closed' is 16, 'reconciling' 11
    training_runs.status   VARCHAR(16) <- 'validation_pending' is 18

`positions.status` is the serious one. L21 added `partially_closed` and
`reconciling` to a column sized when the vocabulary was `open`, `closed` and
`unknown`. A partial close or a reconciliation pass would have raised
`StringDataRightTruncationError` in production — on the position-management
path, which is the one that matters most — while the whole test suite stayed
green because it runs on SQLite.

`training_runs.status` is the same defect introduced at L25, and it is how this
one was found: an end-to-end training run against the real database failed at
the `recording` stage.

`tests/test_models.py::test_every_status_value_fits_its_column` now walks every
CHECK constraint of the form `col IN (...)` against its column's declared
length, so this class cannot recur silently on any table.

**Widening only.** No value is truncated, no row is rewritten, and no
constraint changes. `USING` is unnecessary because every existing value already
fits the old, narrower type.

Revision ID: 0017_status_column_widths
Revises: 0016_money_precision
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_status_column_widths"
down_revision: str | Sequence[str] | None = "0016_money_precision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "positions",
        "status",
        existing_type=sa.String(length=8),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.alter_column(
        "training_runs",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Narrowing again would truncate any row already holding one of the values
    # this migration exists to allow. Left as the reverse alter because a
    # downgrade is only ever run against a database that never held them.
    op.alter_column(
        "training_runs",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.alter_column(
        "positions",
        "status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
