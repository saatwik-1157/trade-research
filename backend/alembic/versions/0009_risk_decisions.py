"""Risk decisions become queryable, and configuration gets a home (L17).

Two changes, both additive.

**`risk_rules.rule_type` gains `limits`.** The table already had the scope
hierarchy this level needs -- global / broker_account / paper_account /
strategy / symbol, with a priority -- and rule types for a handful of
individual limits. What it had no room for was a *layer*: one row carrying the
several limits a scope sets, which is what the precedence rule combines. The
existing narrow types are KEPT and still valid; `limits` is added beside them.

**`risk_events` gains the columns a decision is looked up by.** The table
already carried `decision`, `reason`, `occurred_at` and a JSON `snapshot`, and
the snapshot still holds the full decision. But "show me this account's
rejections today" over a JSON field is a scan, and `decision_id` needs to be
unique for an order to reference the approval that authorised it. So the five
fields a caller filters or joins on are lifted out into columns.

`decision_id` is nullable because the 0 existing rows have none, and a
backfilled value would be an invented identifier for a decision that was never
given one.

Purely additive: no column is dropped, no type narrowed, no existing value
becomes invalid.

Revision ID: 0009_risk_decisions
Revises: 0008_paper_accounts
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_risk_decisions"
down_revision: str | Sequence[str] | None = "0008_paper_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = (
    "rule_type IN ('max_positions','one_per_symbol','max_daily_loss',"
    "'max_exposure_currency','max_risk_per_trade','kill_switch','mode_gate')"
)
NEW_TYPES = (
    "rule_type IN ('max_positions','one_per_symbol','max_daily_loss',"
    "'max_exposure_currency','max_risk_per_trade','kill_switch','mode_gate','limits')"
)


def upgrade() -> None:
    # The naming convention adds the ck_<table>_ prefix, so the bare name goes here.
    op.drop_constraint("rule_type", "risk_rules", type_="check")
    op.create_check_constraint("rule_type", "risk_rules", sa.text(NEW_TYPES))

    op.add_column("risk_events", sa.Column("decision_id", sa.String(36), nullable=True))
    op.add_column("risk_events", sa.Column("paper_account_id", sa.String(36), nullable=True))
    op.add_column("risk_events", sa.Column("mode", sa.String(8), nullable=True))
    op.add_column("risk_events", sa.Column("request_hash", sa.String(64), nullable=True))
    op.add_column(
        "risk_events",
        sa.Column("configuration_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_risk_events_decision_id", "risk_events", ["decision_id"], unique=True)
    op.create_index("ix_risk_events_paper_account_id", "risk_events", ["paper_account_id"])


def downgrade() -> None:
    op.drop_index("ix_risk_events_paper_account_id", table_name="risk_events")
    op.drop_index("ix_risk_events_decision_id", table_name="risk_events")
    for column in (
        "configuration_version",
        "request_hash",
        "mode",
        "paper_account_id",
        "decision_id",
    ):
        op.drop_column("risk_events", column)
    op.drop_constraint("rule_type", "risk_rules", type_="check")
    op.create_check_constraint("rule_type", "risk_rules", sa.text(OLD_TYPES))
