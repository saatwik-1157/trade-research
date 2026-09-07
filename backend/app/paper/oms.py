"""The paper OMS: order lifecycle, idempotency, and the Risk Engine's veto.

Two rules carry this module, and both are enforced by types rather than by
discipline.

**1. An order cannot be created without an `Approval`.** `submit` takes one as
its first positional argument, and `Approval` is constructible only by
`RiskEngine.approve` on an approval -- it has no other constructor path in the
codebase. So "nothing reaches the OMS without passing Risk" is not a check
somebody has to remember to write; there is no way to call `submit` without
holding the Risk Engine's own token. `test_the_oms_cannot_be_called_without_an_approval`
asserts it, and `test_risk_cannot_be_bypassed_by_forging_an_approval` asserts
that a veto verdict cannot be smuggled inside one.

**2. The same intent produces one order, ever.** `intent_id` is the idempotency
key -- it is already `unique=True` on the `orders` table, so the database
enforces what this module enforces in memory. A repeat submission returns the
*existing* order and marks the attempt a duplicate; it never creates a second
one and never raises, because a retry after a timeout is normal behaviour and
turning it into an error would push callers toward retry loops.

The order states are the ones the `orders` table already has. This is not a
second OMS with a private vocabulary: `ORDER_STATUSES` is imported from the
model, and a test asserts every state this module can reach is in it.

`unknown` is a real state and nothing retries out of it. An operation whose
outcome is uncertain is reconciled, never re-sent -- the same rule the live
executor follows, for the same reason: retrying an uncertain fill can double
it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.oms.state import (
    OPEN,
    TERMINAL,
    TRANSITIONS,
    IllegalOrderTransition,
    OrderStatus,
    check_transition,
)
from app.paper.clock import now_utc
from app.paper.execution import Fill, PaperExecution, Reference
from app.paper.router import ExecutionMode, assert_paper_execution
from app.risk.engine import Approval

log = logging.getLogger("app.paper.oms")


# The state machine lives in `app/oms/state.py` as of L19 and is imported
# rather than restated. It was defined here, worked, and was extracted so the
# broker-facing lifecycle could use THE SAME machine: two machines that
# disagree about whether `accepted -> cancelled` is legal is how a cancel
# succeeds in paper and corrupts an order in demo. Every name below was
# importable from this module before and still is.
__all__ = [
    "OPEN",
    "TERMINAL",
    "TRANSITIONS",
    "IllegalOrderTransition",
    "OrderRefused",
    "OrderStatus",
    "PaperOMS",
    "PaperOrder",
    "Submission",
    "check_transition",
]


class OrderRefused(Exception):
    """The OMS declined to create or fill an order, with the reason."""


@dataclass
class PaperOrder:
    """One simulated order. `execution_mode` is on it and never inferred."""

    id: str
    intent_id: str
    account_id: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    created_at: datetime
    status: OrderStatus = OrderStatus.intent
    requested_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    signal_id: str | None = None
    strategy_id: str | None = None
    bot_id: str | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    fill: Fill | None = None
    reason: str = ""
    risk_snapshot: dict[str, object] = field(default_factory=dict)
    sizing_snapshot: dict[str, object] = field(default_factory=dict)
    execution_mode: str = str(ExecutionMode.paper)
    events: list[tuple[datetime, str, str]] = field(default_factory=list)

    def move(self, wanted: OrderStatus, detail: str = "", at: datetime | None = None) -> None:
        check_transition(self.status, wanted)
        self.status = wanted
        self.events.append((at or now_utc(), str(wanted), detail))

    def as_dict(self) -> dict[str, object]:
        return {
            "order_id": self.id,
            "intent_id": self.intent_id,
            "paper_account_id": self.account_id,
            "execution_mode": self.execution_mode,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": str(self.quantity),
            "requested_price": (
                str(self.requested_price) if self.requested_price is not None else None
            ),
            "execution_price": str(self.fill.price) if self.fill else None,
            "stop_loss": str(self.stop_loss) if self.stop_loss is not None else None,
            "take_profit": str(self.take_profit) if self.take_profit is not None else None,
            "status": str(self.status),
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "bot_id": self.bot_id,
            "created_at": self.created_at.isoformat(),
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "fees": str(self.fill.commission) if self.fill else None,
            "slippage_points": str(self.fill.slippage_points) if self.fill else None,
            "reason": self.reason,
            "risk": self.risk_snapshot,
            "sizing": self.sizing_snapshot,
            "events": [
                {"at": at.isoformat(), "status": status, "detail": detail}
                for at, status, detail in self.events
            ],
        }


@dataclass(frozen=True)
class Submission:
    """The result of a submit: the order, and whether it already existed."""

    order: PaperOrder
    duplicate: bool

    @property
    def created(self) -> bool:
        return not self.duplicate


class PaperOMS:
    """Order management for one paper venue. Holds no broker of any kind."""

    def __init__(self, execution: PaperExecution) -> None:
        self.execution = execution
        self.orders: dict[str, PaperOrder] = {}
        self.by_intent: dict[str, PaperOrder] = {}

    # ------------------------------------------------------------- submitting

    def submit(
        self,
        approval: Approval,
        *,
        order_id: str,
        intent_id: str,
        account_id: str,
        order_type: str = "market",
        bot_id: str | None = None,
        at: datetime | None = None,
    ) -> Submission:
        """Create a paper order from a Risk Engine approval.

        `approval` is positional and required. There is no keyword default and
        no overload without it, so an order that Risk did not approve cannot be
        constructed through this method -- which is the whole guarantee.
        """
        if not isinstance(approval, Approval):
            raise OrderRefused(
                "submit requires a risk.Approval; an order the Risk Engine has not "
                "approved cannot be created"
            )
        # An Approval is only built on an approving verdict, but checking here
        # too means a hand-built one still cannot carry a veto through.
        if not approval.verdict.approved:
            raise OrderRefused(
                f"the approval carries a {approval.verdict.decision} verdict, not an "
                "approval; refusing to create an order from it"
            )
        proposal = approval.proposal
        assert_paper_execution(proposal.mode)

        # The approval must be for THIS order, and must still stand. Without
        # these two lines risk could approve order A while order B -- a
        # different size, a different stop -- is the one that gets submitted.
        now = at or now_utc()
        if approval.expired(now):
            raise OrderRefused(
                f"the approval expired at {approval.expires_at}; risk must evaluate "
                "again, because the portfolio it was computed against has moved"
            )
        # L45 C-4: the real order fields, not the approval's own. `binds` on
        # `bound_fields()` compares the approval to itself and can never fail.
        # A paper order is always paper -- `assert_paper_execution` above has
        # already refused anything else -- so that is the mode passed here, and
        # it is the mode the order will actually carry.
        if not approval.binds_order(
            account_id=account_id,
            order_type=order_type,
            mode=str(ExecutionMode.paper),
        ):
            raise OrderRefused(
                f"the approval does not bind to this order. It was issued for "
                f"account {approval.proposal.account_id!r} as a market order; this "
                f"order is account {account_id!r}, a {order_type!r} order. A change "
                "to the account, order type, symbol, side, volume, price, stop, "
                "target or strategy requires a fresh risk evaluation."
            )

        existing = self.by_intent.get(intent_id)
        if existing is not None:
            # A repeat of the same intent. One logical order, not two. This is
            # normal after a timeout, so it is reported rather than raised.
            log.info(
                "duplicate paper order intent",
                extra={
                    "event": "paper_order_duplicate",
                    "intent_id": intent_id,
                    "order_id": existing.id,
                    "paper_account_id": account_id,
                    "execution_mode": str(ExecutionMode.paper),
                },
            )
            return Submission(existing, duplicate=True)

        order = PaperOrder(
            id=order_id,
            intent_id=intent_id,
            account_id=account_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=approval.approved_volume,
            order_type=order_type,
            created_at=now,
            requested_price=proposal.entry_price,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            signal_id=proposal.signal_id,
            strategy_id=proposal.strategy_id,
            bot_id=bot_id,
            risk_snapshot={
                "decision": str(approval.verdict.decision),
                "approved_volume": str(approval.approved_volume),
                "approved_at": approval.approved_at.isoformat(),
                "engine": approval.engine,
                "not_enforced": list(approval.verdict.not_enforced),
            },
        )
        order.move(OrderStatus.submitted, "risk approved", now)
        order.submitted_at = now
        self.orders[order.id] = order
        self.by_intent[intent_id] = order
        return Submission(order, duplicate=False)

    # -------------------------------------------------------------- filling

    def execute(
        self,
        order: PaperOrder,
        reference: Reference,
        *,
        tick_size: Decimal | None = None,
        price_precision: int | None = None,
        at: datetime | None = None,
    ) -> PaperOrder:
        """Fill an order through the paper execution provider.

        A fill that cannot be computed **rejects** the order with the reason.
        It does not park it as `unknown`: this provider is in-process and
        deterministic, so a failure here is a known refusal, not an uncertain
        outcome. `unknown` is reserved for operations that genuinely may have
        happened, and inventing one would train the reconciler on noise.
        """
        if order.status is not OrderStatus.submitted:
            raise IllegalOrderTransition(
                f"only a submitted order can be executed, not a {order.status} one"
            )
        now = at or now_utc()
        order.move(OrderStatus.accepted, "accepted by the paper venue", now)
        try:
            fill = self.execution.fill(
                mode=ExecutionMode.paper,
                side=order.side,
                quantity=order.quantity,
                reference=reference,
                tick_size=tick_size,
                price_precision=price_precision,
                at=now,
            )
        except Exception as exc:  # noqa: BLE001 - the reason is recorded, not swallowed
            order.move(OrderStatus.rejected, f"{type(exc).__name__}: {exc}"[:200], now)
            order.reason = f"{type(exc).__name__}: {exc}"[:300]
            return order
        order.fill = fill
        order.filled_at = now
        order.move(OrderStatus.filled, f"filled at {fill.price}", now)
        return order

    def reject(self, order: PaperOrder, reason: str, at: datetime | None = None) -> PaperOrder:
        order.move(OrderStatus.rejected, reason[:200], at)
        order.reason = reason[:300]
        return order

    def cancel(self, order: PaperOrder, reason: str = "", at: datetime | None = None) -> PaperOrder:
        order.move(OrderStatus.cancelled, reason[:200] or "cancelled", at)
        order.reason = reason[:300] or "cancelled"
        return order

    def mark_unknown(
        self, order: PaperOrder, reason: str, at: datetime | None = None
    ) -> PaperOrder:
        """Park an order whose outcome is genuinely uncertain. Nothing retries it."""
        order.move(OrderStatus.unknown, reason[:200], at)
        order.reason = reason[:300]
        log.warning(
            "paper order in an unknown state; parked for reconciliation, not retried",
            extra={
                "event": "paper_order_unknown",
                "order_id": order.id,
                "intent_id": order.intent_id,
                "paper_account_id": order.account_id,
                "execution_mode": str(ExecutionMode.paper),
            },
        )
        return order

    # ------------------------------------------------------------- reading

    def open_orders(self, account_id: str | None = None) -> list[PaperOrder]:
        return [
            o
            for o in self.orders.values()
            if o.status in OPEN and (account_id is None or o.account_id == account_id)
        ]

    def for_account(self, account_id: str) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.account_id == account_id]
