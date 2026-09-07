"""roles, broker_accounts, mt5_connections, paper_accounts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import Role
from app.db.base import Base
from app.models.base import CreatedMixin, IdMixin, Money, TimestampMixin


class RoleRow(Base):
    """Reference table behind users.role. Seeded with the three roles."""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(16), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)


ROLE_SEED: list[dict[str, object]] = [
    {
        "name": Role.user.value,
        "rank": 0,
        "description": "Read results: dashboards, journal, analytics",
    },
    {
        "name": Role.trader.value,
        "rank": 1,
        "description": "Brokers, strategies, bots, orders, AI models",
    },
    {"name": Role.admin.value, "rank": 2, "description": "Users, roles, platform controls"},
]


async def seed_roles(db: AsyncSession) -> None:
    for row in ROLE_SEED:
        if await db.get(RoleRow, row["name"]) is None:
            db.add(RoleRow(**row))
    await db.commit()


class BrokerAccount(IdMixin, TimestampMixin, Base):
    """A broker account known to the platform.

    No credential lives here. MT5 is reached over IPC to a logged-in
    terminal; `login` is the broker's account number, an identifier, and
    `account_mode` is what the broker reports, not what we hope.
    """

    __tablename__ = "broker_accounts"
    __table_args__ = (
        CheckConstraint("account_mode IN ('demo','live')", name="account_mode"),
        CheckConstraint("broker IN ('mt5','fake')", name="broker"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    broker: Mapped[str] = mapped_column(String(16), nullable=False)
    account_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    login: Mapped[str | None] = mapped_column(String(64), nullable=True)
    server: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class MT5Connection(IdMixin, TimestampMixin, Base):
    """Connection state of the MT5 terminal behind a broker account."""

    __tablename__ = "mt5_connections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected','connecting','connected','reconciling','error')",
            name="status",
        ),
    )

    broker_account_id: Mapped[str] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE"), index=True
    )
    terminal_path: Mapped[str | None] = mapped_column(String(260), nullable=True)
    host: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="disconnected")
    trade_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    build: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


# The paper account lifecycle. `is_active` cannot express it: created, paused
# and disabled all read false while authorising different operations.
PAPER_STATUSES = ("created", "active", "paused", "disabled", "closed")
PAPER_STATUS_CHECK = "status IN ('" + "','".join(PAPER_STATUSES) + "')"


class PaperAccount(IdMixin, TimestampMixin, Base):
    """Internal simulator account. Money here is not money.

    Separate from `broker_accounts` and `mt5_connections` by table, not by a
    flag, so a paper balance and a broker balance cannot be pooled by a query
    that forgot to filter. `orders`, `positions` and `bots` reference this by
    `paper_account_id` and carry `mode='paper'` as well.
    """

    __tablename__ = "paper_accounts"
    __table_args__ = (CheckConstraint(PAPER_STATUS_CHECK, name="status"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    starting_balance: Mapped[Decimal] = mapped_column(Money, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Money, nullable=False)
    equity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    # Carried on the account rather than recomputed from `trades` on every
    # read: two derivations of one fact can disagree, and then neither is
    # trustworthy.
    realized_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    commission_paid: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    # KEPT. Existing readers depend on it, and it is maintained in step with
    # `status` rather than removed for tidiness.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reset_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


__all__ = [
    "BrokerAccount",
    "CreatedMixin",
    "MT5Connection",
    "PAPER_STATUSES",
    "PaperAccount",
    "ROLE_SEED",
    "RoleRow",
    "seed_roles",
]
