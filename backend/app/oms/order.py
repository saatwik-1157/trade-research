"""The managed order: one record, every execution mode.

`ManagedOrder` is what the OMS moves through the state machine. It is a plain
record — it holds no adapter, opens no connection and decides nothing. The
lifecycle rules live in `service.py`, the legal moves in `state.py`, the fill
arithmetic in `fills.py`, and this is the thing they operate on.

**Two identities, never interchangeable.**

  * `client_order_id` is ours and is the idempotency key. It IS the intent id,
    which IS the signal key, so "the same alert arrived twice" and "the same
    order was submitted twice" are one fact with one guard rather than two
    mechanisms that can disagree. `orders.intent_id` is `unique=True`, so the
    database enforces what this class enforces in memory.
  * `broker_order_id` is the venue's and arrives only when the venue speaks.
    It is what reconciliation matches on. Treating one as the other is how a
    reconciliation sweep matches nothing and concludes an order is missing.

**Every transition is recorded before it is believed.** `move()` appends an
`OrderTransition` carrying the previous state, the new state, who caused it
and why. The audit trail is the sequence of those, not a reconstruction from
the current state — a reader should not have to assume no event was lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.oms.fills import FillBook, FillRecord
from app.oms.state import NEEDS_RECONCILIATION, OPEN, TERMINAL, OrderStatus, check_transition

#: Who caused a transition. A state change with no author cannot be audited.
SOURCES = ("pipeline", "operator", "broker", "reconciliation", "recovery", "expiry")


@dataclass(frozen=True)
class OrderTransition:
    """One entry in the audit trail. Append-only; nothing rewrites one."""

    at: datetime
    previous: OrderStatus
    new: OrderStatus
    source: str
    reason: str = ""
    broker_order_id: str | None = None
    retcode: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "previous_status": str(self.previous),
            "new_status": str(self.new),
            "source": self.source,
            "reason": self.reason,
            "broker_order_id": self.broker_order_id,
            "retcode": self.retcode,
        }


@dataclass
class ManagedOrder:
    """One order's whole life. Mode is on it and is never inferred."""

    id: str
    client_order_id: str
    account_id: str
    symbol: str
    side: str  # "buy" | "sell"
    quantity: Decimal
    mode: str  # "paper" | "demo" | "live"
    created_at: datetime
    order_type: str = "market"
    time_in_force: str = "gtc"
    status: OrderStatus = OrderStatus.intent
    broker_order_id: str | None = None
    broker: str | None = None
    requested_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    signal_id: str | None = None
    strategy_id: str | None = None
    bot_id: str | None = None
    #: The approval that authorised this order. An order with no decision id is
    #: an order nobody can trace to a risk evaluation.
    risk_decision_id: str | None = None
    risk_snapshot: dict[str, object] = field(default_factory=dict)
    sizing_snapshot: dict[str, object] = field(default_factory=dict)
    expires_at: datetime | None = None
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    reject_reason: str | None = None
    error_code: str | None = None
    transitions: list[OrderTransition] = field(default_factory=list)
    book: FillBook = field(init=False)

    def __post_init__(self) -> None:
        self.book = FillBook(requested=self.quantity)

    # ------------------------------------------------------------- derived

    @property
    def filled_quantity(self) -> Decimal:
        return self.book.filled_quantity

    @property
    def remaining_quantity(self) -> Decimal:
        return self.book.remaining_quantity

    @property
    def average_fill_price(self) -> Decimal | None:
        return self.book.average_price

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def open(self) -> bool:
        return self.status in OPEN

    @property
    def needs_reconciliation(self) -> bool:
        return self.status in NEEDS_RECONCILIATION

    # ---------------------------------------------------------- transitions

    def move(
        self,
        wanted: OrderStatus,
        *,
        at: datetime,
        source: str,
        reason: str = "",
        retcode: int | None = None,
    ) -> None:
        """Move to a new state, recording where it came from and who did it.

        The transition is checked first, so an illegal move raises and leaves
        the order exactly as it was. A half-applied state change is worse than
        a refused one: the record would then describe an order that never
        existed.
        """
        check_transition(self.status, wanted)
        if source not in SOURCES:
            raise ValueError(f"unknown transition source {source!r}; expected one of {SOURCES}")
        previous = self.status
        self.status = wanted
        self.transitions.append(
            OrderTransition(
                at=at,
                previous=previous,
                new=wanted,
                source=source,
                reason=reason[:500],
                broker_order_id=self.broker_order_id,
                retcode=retcode,
            )
        )
        self._stamp(wanted, at)

    def _stamp(self, status: OrderStatus, at: datetime) -> None:
        """Record WHEN a milestone happened, once. A stamp is never rewritten:
        the first time an order was acknowledged is a fact, and a later partial
        fill does not change it."""
        if status is OrderStatus.submitted and self.submitted_at is None:
            self.submitted_at = at
        elif status is OrderStatus.accepted and self.acknowledged_at is None:
            self.acknowledged_at = at
        elif status is OrderStatus.filled and self.filled_at is None:
            self.filled_at = at
        elif status is OrderStatus.cancelled and self.cancelled_at is None:
            self.cancelled_at = at

    # --------------------------------------------------------------- fills

    def record_fill(self, fill: FillRecord, *, at: datetime, source: str = "broker") -> bool:
        """Apply one execution report and move to the state it implies.

        Returns False when the deal was already recorded — execution reports
        arrive more than once (a reconnect replays them, a reconciliation
        sweep re-reads history), and applying one twice is a position twice
        the size it should be. That is a normal event, so it is reported
        rather than raised.

        The state is DERIVED from the book, never asserted by the caller: an
        order is filled when the venue has filled all of it, and partially
        filled when it has filled some. A caller that could pass the state in
        could mark an order filled that was not.
        """
        if self.book.already_recorded(fill.broker_deal_id):
            return False
        self.book.add(fill)  # refuses an overfill rather than truncating it
        complete = self.book.complete
        wanted = OrderStatus.filled if complete else OrderStatus.partially_filled
        detail = (
            f"{fill.quantity} at {fill.price} ({self.book.filled_quantity}/{self.quantity} filled)"
        )
        self.move(wanted, at=at, source=source, reason=detail)
        return True

    # ------------------------------------------------------------- reading

    def as_dict(self) -> dict[str, object]:
        def s(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        def t(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "order_id": self.id,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "account_id": self.account_id,
            "strategy_id": self.strategy_id,
            "signal_id": self.signal_id,
            "bot_id": self.bot_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "mode": self.mode,
            "broker": self.broker,
            "status": str(self.status),
            "quantity": str(self.quantity),
            "filled_quantity": str(self.filled_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "average_fill_price": s(self.average_fill_price),
            "requested_price": s(self.requested_price),
            "stop_loss": s(self.stop_loss),
            "take_profit": s(self.take_profit),
            "risk_decision_id": self.risk_decision_id,
            "risk": self.risk_snapshot,
            "sizing": self.sizing_snapshot,
            "created_at": t(self.created_at),
            "submitted_at": t(self.submitted_at),
            "acknowledged_at": t(self.acknowledged_at),
            "filled_at": t(self.filled_at),
            "cancelled_at": t(self.cancelled_at),
            "expires_at": t(self.expires_at),
            "reject_reason": self.reject_reason,
            "error_code": self.error_code,
            "needs_reconciliation": self.needs_reconciliation,
            "events": [e.as_dict() for e in self.transitions],
            "fills": self.book.as_dict(),
        }
