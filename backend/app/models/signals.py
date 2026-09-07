"""webhook_events, signals."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import MODE_CHECK, IdMixin, JSONType, Ratio


class WebhookEvent(IdMixin, Base):
    """One inbound alert, redacted, with its idempotency key."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint("auth_strength IN ('strong','weak','none')", name="auth_strength"),
        CheckConstraint("status IN ('accepted','rejected','duplicate')", name="status"),
    )

    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="tradingview")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_strength: Mapped[str] = mapped_column(String(8), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL", use_alter=True, name="fk_webhook_signal"),
        nullable=True,
    )


class Signal(IdMixin, Base):
    """The contract from the gateway through the AI layer to the Risk Engine."""

    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint("source IN ('strategy','tradingview','manual')", name="source"),
        CheckConstraint("direction IN ('buy','sell','flat')", name="direction"),
        CheckConstraint(MODE_CHECK, name="mode"),
        CheckConstraint("auth_strength IN ('strong','weak','internal')", name="auth_strength"),
        CheckConstraint(
            "status IN ('new','approved','vetoed','expired','executed')", name="status"
        ),
    )

    signal_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="SET NULL"), nullable=True
    )
    webhook_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("webhook_events.id", ondelete="SET NULL"), nullable=True
    )
    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    signal_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    auth_strength: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    ai_model_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True
    )
    meta: Mapped[dict | None] = mapped_column("metadata", JSONType, nullable=True)
