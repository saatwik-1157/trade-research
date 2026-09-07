"""strategies, strategy_versions, strategy_parameters."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import IdMixin, JSONType, TimestampMixin


class Strategy(IdMixin, TimestampMixin, Base):
    __tablename__ = "strategies"
    __table_args__ = (CheckConstraint("tier IN ('research_only','live_demo')", name="tier"),)

    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="research_only")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StrategyVersion(IdMixin, TimestampMixin, Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="strategy_version"),
        CheckConstraint("status IN ('draft','validated','retired')", name="status"),
    )

    strategy_id: Mapped[str] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    code_ref: Mapped[str] = mapped_column(String(200), nullable=False)  # module:callable
    config: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    validation_report_ref: Mapped[str | None] = mapped_column(String(260), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")


class StrategyParameter(IdMixin, Base):
    __tablename__ = "strategy_parameters"
    __table_args__ = (UniqueConstraint("strategy_version_id", "name", name="version_param"),)

    strategy_version_id: Mapped[str] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)  # int|float|str|bool|json
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSONType, nullable=True
    )
