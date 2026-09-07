"""symbols, symbol_mappings, market_bars.

The unit class matters more than it looks: pooling "points" across symbols
whose point sizes differ 59x is the metals error this project keeps finding.

Where the contract spec lives is the other decision worth stating. Tick
value, volume step and trading hours are properties of a symbol *at a
broker*, not of the instrument: two brokers quote different tick values and
different minimum lots for "EURUSD", and one of them may not list it at all.
So the spec columns sit on the mapping row, and `symbols` holds only what is
true of the instrument everywhere.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import utcnow
from app.db.base import Base
from app.models.base import IdMixin, JSONType, Money, Price, Qty, TimestampMixin


class Symbol(IdMixin, TimestampMixin, Base):
    __tablename__ = "symbols"
    __table_args__ = (
        CheckConstraint(
            "asset_class IN ('fx','index','metal','crypto','equity','other')", name="asset_class"
        ),
        CheckConstraint("unit_class IN ('points','percent')", name="unit_class"),
    )

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    base_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quote_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    digits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    point_size: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    unit_class: Mapped[str] = mapped_column(String(8), nullable=False, default="points")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SymbolMapping(IdMixin, TimestampMixin, Base):
    __tablename__ = "symbol_mappings"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('mt5','tradingview','ccxt','yfinance','simulator')", name="provider"
        ),
        UniqueConstraint("provider", "provider_symbol", name="provider_symbol"),
        UniqueConstraint("symbol_id", "provider", name="symbol_provider"),
        CheckConstraint(
            "spec_source IS NULL OR spec_source IN ('mt5_terminal','manual','provider_api')",
            name="spec_source",
        ),
    )

    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Contract spec, as this provider defines it. -----------------------
    # Every one is nullable and every one is REFUSED rather than defaulted
    # when a caller needs it and it is absent: a lot size computed from a
    # guessed tick value is a real order for the wrong amount. This is
    # tools/mt5_paper.lot_for_risk's rule, moved to where the data lives.
    contract_size: Mapped[Decimal | None] = mapped_column(Qty, nullable=True)
    tick_size: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    tick_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    minimum_volume: Mapped[Decimal | None] = mapped_column(Qty, nullable=True)
    maximum_volume: Mapped[Decimal | None] = mapped_column(Qty, nullable=True)
    volume_step: Mapped[Decimal | None] = mapped_column(Qty, nullable=True)
    price_precision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume_precision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trading_hours: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    # Provenance. A spec read from a terminal and a spec typed by hand are
    # not the same evidence, and the difference has to survive in the row.
    spec_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    spec_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MarketBar(Base):
    """One normalized OHLCV candle, as one provider served it.

    Why the table exists at all, given MT5 and yfinance both hold history: to
    make ingestion **idempotent** and replay **deterministic**. A provider
    retry or a reconnect replays bars, and the unique key below is what makes
    the second copy a no-op rather than a duplicate candle. Replay (L15) also
    needs a series that does not change under it between runs, which a live
    provider cannot promise.

    It is a cache with provenance, not a second source of truth. `provider` is
    part of the key precisely so two providers' versions of "EURUSD H1" sit
    side by side and are never averaged: they are different measurements of
    different books, and the project already has a measured example of what
    pooling unlike quantities does to a result.

    Every nullable column is nullable because the *provider* may not supply it,
    and stays NULL rather than defaulting. A recorded spread of 0 is an
    unrecorded spread, not a free trade -- `cost_profile.py` measured that
    averaging those zeros in halves the apparent cost of trading -- so an
    absent spread is NULL here and the availability travels with it.
    """

    __tablename__ = "market_bars"
    __table_args__ = (
        # The identity of a bar. This is the idempotency guarantee: an
        # ingestion that runs twice writes the same rows once.
        UniqueConstraint(
            "provider", "provider_symbol", "timeframe", "bar_time", name="market_bar_identity"
        ),
        CheckConstraint(
            "provider IN ('mt5','tradingview','ccxt','yfinance','simulator')", name="provider"
        ),
        CheckConstraint(
            "timeframe IN ('M1','M5','M15','M30','H1','H4','D1','W1')", name="timeframe"
        ),
        # The four relationships that must hold for the row to be a candle at
        # all. Enforced in the database as well as in `app.marketdata`, because
        # a bar that reaches the table by another path is still a bar.
        CheckConstraint(
            "high >= low AND high >= open AND high >= close AND low <= open AND low <= close",
            name="ohlc_consistent",
        ),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="prices_positive"),
        CheckConstraint("volume IS NULL OR volume >= 0", name="volume_not_negative"),
        CheckConstraint("spread IS NULL OR spread >= 0", name="spread_not_negative"),
        Index("ix_market_bars_series", "provider", "provider_symbol", "timeframe", "bar_time"),
    )

    # BigInteger on PostgreSQL; Integer on SQLite, which only autoincrements a
    # column declared exactly INTEGER PRIMARY KEY. Same variant PositionEvent uses.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    # Nullable: a provider may serve a symbol the platform has not mapped yet,
    # and refusing to store it would lose data over a mapping gap.
    symbol_id: Mapped[str | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True, index=True
    )
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    # The bar's OPEN time, UTC. A provider that stamps closes is converted by
    # its adapter; nothing downstream guesses which convention a row uses.
    bar_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open: Mapped[Decimal] = mapped_column(Price, nullable=False)
    high: Mapped[Decimal] = mapped_column(Price, nullable=False)
    low: Mapped[Decimal] = mapped_column(Price, nullable=False)
    close: Mapped[Decimal] = mapped_column(Price, nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(Qty, nullable=True)
    spread: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    spread_availability: Mapped[str] = mapped_column(
        String(16), nullable=False, default="not_available"
    )
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
