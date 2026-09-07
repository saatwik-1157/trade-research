"""risk_rules, risk_events, capital_reservations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import IdMixin, JSONType, Money, Qty, TimestampMixin


class RiskRule(IdMixin, TimestampMixin, Base):
    __tablename__ = "risk_rules"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('global','broker_account','paper_account','strategy','symbol')",
            name="scope",
        ),
        CheckConstraint(
            "rule_type IN ('max_positions','one_per_symbol','max_daily_loss',"
            "'max_exposure_currency','max_risk_per_trade','kill_switch','mode_gate',"
            "'limits')",
            name="rule_type",
        ),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    scope_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_type: Mapped[str] = mapped_column(String(24), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class RiskEvent(IdMixin, Base):
    """Every Risk Engine decision, including approvals. A veto is an event."""

    __tablename__ = "risk_events"
    __table_args__ = (CheckConstraint("decision IN ('approve','veto','halt')", name="decision"),)

    rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("risk_rules.id", ondelete="SET NULL"), nullable=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    order_id: Mapped[str | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    decision: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # The full decision, including every check and its detail. The columns
    # below are the ones a caller filters or joins on, lifted out of it --
    # "this account's rejections today" over a JSON field is a scan.
    snapshot: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # Unique, so an order can reference the approval that authorised it and
    # exactly one decision answers. Nullable because decisions recorded before
    # L17 have none, and inventing one would name a decision nobody made.
    decision_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    paper_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    configuration_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


#: What a reservation can be. **L54/L55.**
#:
#: A reservation exists between "risk approved this" and "the venue settled
#: it". Both ends matter: `CONSUMED` and `RELEASED` free the budget, `EXPIRED`
#: frees it because the approval aged out, and `PENDING`/`ACTIVE` hold it.
RESERVATION_STATUSES = (
    "pending",
    "active",
    "consumed",
    "released",
    "expired",
    "cancelled",
    "rejected",
)

#: The statuses that still hold budget. Everything else has given it back.
RESERVATION_HOLDING = ("pending", "active")


class CapitalReservation(IdMixin, TimestampMixin, Base):
    """Risk budget an approval has claimed and no fill has consumed yet.

    **Why this table exists.** `RiskService` has held reservations since L17
    and holds them in a process-local dict. That is correct for the race it was
    built for -- two concurrent orders evaluated against one snapshot -- and it
    is empty after a restart. An approval that reserved budget and had not yet
    filled when the process died releases nothing, because there is nothing
    left to release; the next process starts believing the whole budget is
    free.

    That is the same shape as the L45 C-1 defect: a guard whose state does not
    outlive the process that made it.

    **This table does not replace the in-memory reservations.** The dict stays
    as the fast path inside one process; this is the record that survives a
    restart and the one a second process could read. Two derivations of one
    fact can disagree, so the dict is rebuilt FROM this table on startup rather
    than kept beside it.

    **Idempotent by `intent_id`.** The unique constraint is what makes a
    retried signal reserve once rather than twice -- the same backstop
    `orders.intent_id` provides for orders, for the same reason.
    """

    __tablename__ = "capital_reservations"
    __table_args__ = (
        CheckConstraint("status IN ('" + "','".join(RESERVATION_STATUSES) + "')", name="status"),
        # Risk and exposure are claims on a budget; a negative claim would
        # ADD budget, which is the one direction this table must not permit.
        CheckConstraint("risk_amount >= 0", name="risk_amount"),
        CheckConstraint("exposure >= 0", name="exposure"),
        CheckConstraint("positions >= 0", name="positions"),
        # One reservation per intent. A retried signal reserves once.
        Index("ux_capital_reservations_intent", "intent_id", unique=True),
        # The query the allocator runs: what does this account currently hold?
        Index("ix_capital_reservations_account_status", "account_id", "status"),
        Index("ix_capital_reservations_expires", "expires_at"),
    )

    #: The risk decision that created it, so a reservation traces to a verdict.
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    #: The signal or idempotency key. UNIQUE -- this is the idempotency guard.
    intent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    bot_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    #: The money at risk this reservation claims.
    risk_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    #: The notional it claims.
    exposure: Mapped[Decimal] = mapped_column(Qty, nullable=False, default=0)
    positions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    #: Why it stopped holding budget. NULL while it still holds.
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: The order it became, once it became one.
    order_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
