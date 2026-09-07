"""notifications engine: deliveries, preferences, and the columns L34 needs.

**One table is altered and two are created. Nothing is dropped and no row is
deleted.**

`notifications` has existed since 0002 and has been written by exactly one
caller -- `app.monitoring.monitor.ModelMonitor._persist`, since L29. Those rows
are KEPT. They carry `user_id NULL`, which under L34 means "a platform record
with no recipient", and since every read in the notifications API is scoped to
the signed-in user they can never surface as somebody's notification. Deleting
them would destroy the only record those monitoring runs left.

Three things happen to that table:

    ADD nine columns
        All nullable. Every existing row is valid the moment they exist, so
        there is no backfill and no window in which the table is inconsistent.
        `updated_at` is the one exception -- see below.

    WIDEN the severity constraint
        0002 allowed `info | warning | critical`, lowercase. L34's vocabulary
        is five words, uppercase, defined once in
        `app.notifications.contract.Severity` and read from there by the model,
        so a value the code can produce and the schema rejects cannot exist.
        The existing three rows-worth of vocabulary is upper-cased in place by
        this migration; `_persist` was changed in the same commit to write the
        new words. Case, not meaning: `warning` and `WARNING` are the same
        severity, and the downgrade lowers them back.

    ADD `UNIQUE (user_id, dedup_key)` and three indexes
        Section 31: a redelivered event resolves to the existing row rather
        than writing a second one, and the guarantee is the database's rather
        than the worker's to remember. Both columns are NULL on every pre-L34
        row and SQL treats NULLs as distinct, so those rows neither collide
        with each other nor constrain anything -- the same "NULLs are distinct"
        behaviour that migration 0020 hit as a trap is what makes this safe.

`updated_at` is added NOT NULL with a server default so existing rows get a
value without a separate backfill pass. The default stays on the column: the
model supplies the value on every insert, and leaving it means an INSERT that
somehow omits it fails on the application's clock rather than on a NULL.

`notification_deliveries` and `notification_preferences` are new. Both take
their CHECK vocabularies from the same enums the models do.

Revision ID: 0024_notifications
Revises: 0023_trade_reviews
Create Date: 2026-09-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_notifications"
down_revision: str | None = "0023_trade_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEVERITIES = ("INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")
CATEGORIES = (
    "TRADING",
    "RISK",
    "PORTFOLIO",
    "BOTS",
    "STRATEGIES",
    "AI",
    "MONITORING",
    "BROKER",
    "SYSTEM",
    "SECURITY",
)
CHANNELS = ("IN_APP", "EMAIL", "DISCORD")
DELIVERY_STATUSES = ("PENDING", "PROCESSING", "DELIVERED", "FAILED", "RETRYING", "SKIPPED")
ENVIRONMENTS = ("backtest", "paper", "demo", "live", "unknown")


def _in(column: str, values: Sequence[str]) -> str:
    return column + " IN ('" + "','".join(values) + "')"


def upgrade() -> None:
    # ------------------------------------------------------------ notifications
    op.add_column("notifications", sa.Column("event_id", sa.String(length=64), nullable=True))
    op.add_column("notifications", sa.Column("category", sa.String(length=16), nullable=True))
    op.add_column("notifications", sa.Column("environment", sa.String(length=8), nullable=True))
    op.add_column("notifications", sa.Column("entity_type", sa.String(length=24), nullable=True))
    op.add_column("notifications", sa.Column("entity_id", sa.String(length=64), nullable=True))
    op.add_column("notifications", sa.Column("read_at", sa.DateTime(), nullable=True))
    op.add_column("notifications", sa.Column("dedup_key", sa.String(length=32), nullable=True))
    op.add_column("notifications", sa.Column("condition_key", sa.String(length=32), nullable=True))
    op.add_column(
        "notifications",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Case, not meaning. Done before the new constraint so no existing row is
    # in violation for the instant between the two statements.
    op.execute("UPDATE notifications SET severity = upper(severity)")

    with op.batch_alter_table("notifications") as batch:
        # `op.f(...)`, because the name is ALREADY the final one. Migration 0002
        # created it as `op.f("ck_notifications_severity")`, so that is what is
        # in the database -- and without `op.f` here the metadata naming
        # convention `ck_%(table_name)s_%(constraint_name)s` prefixes it a
        # second time and this looks for
        # `ck_notifications_ck_notifications_severity`, which does not exist.
        #
        # SQLite hides it: `batch_alter_table` rebuilds the table, so a drop of
        # a constraint that is not found does not raise. **PostgreSQL does not**,
        # and this migration therefore could not be applied to a real database
        # -- the whole L34 upgrade path was broken on the only engine that
        # matters. Found at L40 by running `test_migrations.py` against the
        # Compose Postgres for the first time; the suite had been skipping those
        # three tests for want of `TEST_DATABASE_URL`.
        batch.drop_constraint(op.f("ck_notifications_severity"), type_="check")
        batch.create_check_constraint("severity", _in("severity", SEVERITIES))
        batch.create_check_constraint(
            "category", "category IS NULL OR " + _in("category", CATEGORIES)
        )
        batch.create_check_constraint(
            "environment", "environment IS NULL OR " + _in("environment", ENVIRONMENTS)
        )
        batch.create_unique_constraint(
            "one_notification_per_event_per_user", ["user_id", "dedup_key"]
        )

    op.create_index("ix_notifications_event_id", "notifications", ["event_id"])
    op.create_index("ix_notifications_event_type", "notifications", ["event_type"])
    op.create_index("ix_notifications_category", "notifications", ["category"])
    op.create_index("ix_notifications_environment", "notifications", ["environment"])
    # Section 14. The unread count runs on every page load; this is what keeps
    # it an index-only count rather than a scan of the user's whole history.
    op.create_index("ix_notifications_user_unread", "notifications", ["user_id", "read_at"])
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"])
    op.create_index(
        "ix_notifications_condition",
        "notifications",
        ["user_id", "condition_key", "created_at"],
    )

    # ------------------------------------------------- notification_deliveries
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("notification_id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="PENDING"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
        # Bare constraint names: the naming convention interpolates the prefix,
        # and several migrations in this repository have shipped a doubled one.
        sa.CheckConstraint(_in("channel", CHANNELS), name="channel"),
        sa.CheckConstraint(_in("status", DELIVERY_STATUSES), name="status"),
        sa.CheckConstraint("attempt_count >= 0", name="attempts_are_not_negative"),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_deliveries_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        # Section 31. One row per (notification, channel): the same notification
        # cannot be emailed twice because a worker retried the wrong thing.
        sa.UniqueConstraint("notification_id", "channel", name="one_delivery_per_channel"),
    )
    op.create_index(
        "ix_notification_deliveries_notification_id",
        "notification_deliveries",
        ["notification_id"],
    )
    op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"])
    # The worker's own query: what is still owed, worst first.
    op.create_index(
        "ix_notification_deliveries_queue",
        "notification_deliveries",
        ["status", "priority", "next_attempt_at"],
    )

    # ------------------------------------------------ notification_preferences
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_severity", sa.String(length=8), nullable=False, server_default="INFO"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.CheckConstraint(_in("category", CATEGORIES), name="category"),
        sa.CheckConstraint(_in("channel", CHANNELS), name="channel"),
        sa.CheckConstraint(_in("min_severity", SEVERITIES), name="min_severity"),
        # Section 16, carried into the schema: the in-app channel cannot be
        # switched off, so a row saying otherwise cannot be written -- not by
        # the API, and not by anything else holding a connection.
        sa.CheckConstraint("channel <> 'IN_APP' OR enabled", name="in_app_cannot_be_disabled"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "category", "channel", name="one_preference_per_pair"),
    )
    op.create_index("ix_notification_preferences_user_id", "notification_preferences", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_preferences_user_id", table_name="notification_preferences")
    op.drop_table("notification_preferences")

    op.drop_index("ix_notification_deliveries_queue", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_status", table_name="notification_deliveries")
    op.drop_index(
        "ix_notification_deliveries_notification_id", table_name="notification_deliveries"
    )
    op.drop_table("notification_deliveries")

    op.drop_index("ix_notifications_condition", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_environment", table_name="notifications")
    op.drop_index("ix_notifications_category", table_name="notifications")
    op.drop_index("ix_notifications_event_type", table_name="notifications")
    op.drop_index("ix_notifications_event_id", table_name="notifications")

    # Rows written by L34 carry severities 0002 did not allow. They are lowered
    # to the nearest word the old constraint accepts before it is restored --
    # SUCCESS becomes info and ERROR becomes critical, which is the closest
    # meaning the three-word vocabulary can express. A downgrade cannot be
    # lossless here; it can be honest about what it collapses.
    op.execute("UPDATE notifications SET severity = 'INFO' WHERE severity = 'SUCCESS'")
    op.execute("UPDATE notifications SET severity = 'CRITICAL' WHERE severity = 'ERROR'")
    op.execute("UPDATE notifications SET severity = lower(severity)")

    with op.batch_alter_table("notifications") as batch:
        # Same reasoning as the upgrade: these four names are final. The three
        # check constraints were created by the upgrade above through the
        # convention, so they are in the database as `ck_notifications_<name>`.
        # NOT wrapped in `op.f`, unlike the three below: the "uq" convention
        # is `uq_%(table_name)s_%(column_0_name)s` and carries no
        # `%(constraint_name)s` token, so an explicitly named unique
        # constraint keeps its literal name. Verified against
        # `pg_constraint`, not assumed -- the name in the database is
        # `one_notification_per_event_per_user`.
        batch.drop_constraint("one_notification_per_event_per_user", type_="unique")
        batch.drop_constraint(op.f("ck_notifications_environment"), type_="check")
        batch.drop_constraint(op.f("ck_notifications_category"), type_="check")
        batch.drop_constraint(op.f("ck_notifications_severity"), type_="check")
        batch.create_check_constraint("severity", "severity IN ('info','warning','critical')")

    op.drop_column("notifications", "updated_at")
    op.drop_column("notifications", "condition_key")
    op.drop_column("notifications", "dedup_key")
    op.drop_column("notifications", "read_at")
    op.drop_column("notifications", "entity_id")
    op.drop_column("notifications", "entity_type")
    op.drop_column("notifications", "environment")
    op.drop_column("notifications", "category")
    op.drop_column("notifications", "event_id")
