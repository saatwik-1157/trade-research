"""backtests, backtest_trades, replay_sessions, paper_orders, paper_positions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import CreatedMixin, IdMixin, JSONType, Money, Price, Qty, Ratio

RUN_STATUS = "status IN ('queued','running','finished','failed','cancelled')"
# A replay is interactive and can be paused; a batch backtest cannot, so the
# vocabulary is widened only where the state is reachable (migration 0007).
REPLAY_STATUS = "status IN ('queued','running','paused','finished','failed','cancelled')"


class Backtest(IdMixin, CreatedMixin, Base):
    """One run of the research harness. The verdict is stored verbatim."""

    __tablename__ = "backtests"
    __table_args__ = (
        CheckConstraint(
            "engine IN ('rule_backtest','rule_search','exit_search','bracket_sweep',"
            "'score_backtest','patterns','replay')",
            name="engine",
        ),
        CheckConstraint(RUN_STATUS, name="status"),
    )

    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="SET NULL"), nullable=True
    )
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    timeframe: Mapped[str | None] = mapped_column(String(8), nullable=True)
    universe: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    params: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(260), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BacktestTrade(IdMixin, Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (CheckConstraint("side IN ('long','short')", name="side"),)

    backtest_id: Mapped[str] = mapped_column(
        ForeignKey("backtests.id", ondelete="CASCADE"), index=True
    )
    symbol_id: Mapped[str | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )
    candidate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    exit_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    pnl_points: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    r_multiple: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)


class ReplaySession(IdMixin, CreatedMixin, Base):
    __tablename__ = "replay_sessions"
    __table_args__ = (CheckConstraint(REPLAY_STATUS, name="status"),)

    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="SET NULL"), nullable=True
    )
    paper_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="SET NULL"), nullable=True
    )
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    universe: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    from_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    to_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    speed: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    cursor_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


class PaperOrder(IdMixin, Base):
    """Simulator-side detail for an orders row with mode='paper'."""

    __tablename__ = "paper_orders"

    paper_account_id: Mapped[str] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), unique=True, index=True
    )
    replay_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("replay_sessions.id", ondelete="SET NULL"), nullable=True
    )
    simulated_fill_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    simulated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    spread_charged: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    bar_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaperPosition(IdMixin, Base):
    """Simulator-side mark for a positions row with mode='paper'."""

    __tablename__ = "paper_positions"

    paper_account_id: Mapped[str] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True
    )
    position_id: Mapped[str] = mapped_column(
        ForeignKey("positions.id", ondelete="CASCADE"), unique=True, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    last_mark_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    floating_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    last_marked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
