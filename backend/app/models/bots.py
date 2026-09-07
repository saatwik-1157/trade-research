"""bots, bot_runs, bot_events."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import MODE_CHECK, IdMixin, JSONType, Money, TimestampMixin

# The bot run lifecycle. Six predate L22 and keep their own names -- the brief
# calls one `ERROR`, this project has always called it `crashed`, which is more
# specific and is what every existing row says.
#
# Three were added at L22, and `paused` fixes a defect: `pause_bot` wrote
# `stopping` here because there was no value for it, so a paused bot and a bot
# shutting down were the same row and a restart could not tell them apart.
BOT_RUN_STATUSES = (
    "starting",
    "running",
    "paused",
    "stopping",
    "stopped",
    "crashed",
    "recovering",
    "halted",
    "disabled",
)


class Bot(IdMixin, TimestampMixin, Base):
    __tablename__ = "bots"
    __table_args__ = (CheckConstraint(MODE_CHECK, name="mode"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="SET NULL"), nullable=True
    )
    broker_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )
    paper_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="SET NULL"), nullable=True
    )
    config: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ---------------------------------------------------------------- L22
    # Per-bot limits. On the row rather than inside `config` because a limit
    # that exists only in an untyped blob cannot be queried, constrained, or
    # shown to have been applied.
    #
    # Every one is nullable and null means NOT SET -- a bot with no
    # `max_daily_trades` has no bot-level trade limit, and the RiskEngine's
    # global limits still apply. A bot limit can only ever RESTRICT: the
    # manager takes the more restrictive of the bot's figure and the
    # account's, so a bot asking for more than its account permits gets the
    # account's.
    max_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_daily_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_daily_loss: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_risk_per_trade: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    cooldown_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---------------------------------------------------------------- L45 F-1
    #: Whether this bot's signals may take their stop and target from the
    #: ALERT rather than from the platform. **False by default and per bot.**
    #:
    #: `fixed_risk` sizing needs a stop distance and does not invent one, and
    #: nothing on the webhook path produces a bracket -- an ATR-derived one
    #: cannot be computed while `market_bars` covers one instrument. The alert's own
    #: suggestion is the only bracket that exists today, and it is quarantined
    #: under `meta["advisory_ignored"]` on purpose.
    #:
    #: Obeying it must therefore be a decision somebody makes: under
    #: `fixed_risk` a TIGHTER stop produces a LARGER position, so this hands an
    #: external sender an input that moves size upward. `max_risk_per_trade`
    #: still bounds the money at risk and the RiskEngine still evaluates the
    #: order.
    use_alert_bracket: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Separate from `is_enabled`, which means "this bot may be started".
    # Disabling is a stronger statement an operator makes, and folding the two
    # would make "somebody turned this off after an incident" and "this bot
    # has never been switched on" the same fact.
    is_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BotRun(IdMixin, Base):
    __tablename__ = "bot_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "','".join(BOT_RUN_STATUSES) + "')",
            name="status",
        ),
    )

    bot_id: Mapped[str] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="starting", index=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    host: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


class BotEvent(IdMixin, Base):
    __tablename__ = "bot_events"
    __table_args__ = (CheckConstraint("level IN ('info','warning','error')", name="level"),)

    bot_run_id: Mapped[str] = mapped_column(
        ForeignKey("bot_runs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
