"""portfolio_snapshots, journal_entries, trade_tags."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import MODE_CHECK, IdMixin, JSONType, Money, Ratio, TimestampMixin


class PortfolioSnapshot(IdMixin, Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (CheckConstraint(MODE_CHECK, name="mode"),)

    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    broker_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    paper_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    balance: Mapped[Decimal] = mapped_column(Money, nullable=False)
    equity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    margin: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    free_margin: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exposure: Mapped[dict | None] = mapped_column(JSONType, nullable=True)  # by currency
    drawdown_pct: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)


class JournalEntry(IdMixin, TimestampMixin, Base):
    __tablename__ = "journal_entries"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trade_id: Mapped[str | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL"), nullable=True, index=True
    )
    position_id: Mapped[str | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ai_review: Mapped[dict | None] = mapped_column(JSONType, nullable=True)  # L33


class TradeTag(IdMixin, Base):
    __tablename__ = "trade_tags"
    __table_args__ = (UniqueConstraint("trade_id", "tag", name="trade_tag"),)

    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), index=True)
    tag: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
