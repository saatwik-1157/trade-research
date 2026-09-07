"""Order lifecycle events on the existing bus.

The L07 catalogue declared eleven `ORDER_*` types and named L19 as the level
that would produce them. Until now exactly one had a producer
(`ORDER_FILLED`, from the paper service), so ten types were documented,
authorized, scoped and dead. This maps the state machine onto them.

**No second event system.** These are `app.realtime.catalogue.EventType`
values published through the same `Hub` every other producer uses, on the
`account` scope the catalogue already assigns them. Nothing here creates a
channel, a bus or a transport.

**A published event is a report, never a command.** The frontend subscribes to
watch; no consumer of these events may submit, cancel or retry anything. That
is what keeps the realtime layer out of the execution path — a bus that can
trigger a trade is a second way to trade.

**`ORDER_UNKNOWN` is published like any other.** An operator learning that an
order's outcome is uncertain is the entire point of the state; suppressing it
because it looks like an error is how it stops being noticed.
"""

from __future__ import annotations

import logging

from app.oms.order import ManagedOrder
from app.oms.state import OrderStatus
from app.realtime.catalogue import EventType

log = logging.getLogger("app.oms.events")

#: One event type per state the machine can enter. Total by construction: the
#: test asserts every `OrderStatus` has an entry, so a state added later
#: without an event fails the build rather than falling silent.
EVENT_FOR: dict[OrderStatus, EventType] = {
    OrderStatus.intent: EventType.ORDER_CREATED,
    OrderStatus.submitting: EventType.ORDER_UPDATED,
    OrderStatus.submitted: EventType.ORDER_SUBMITTED,
    OrderStatus.accepted: EventType.ORDER_ACKNOWLEDGED,
    OrderStatus.partially_filled: EventType.ORDER_PARTIALLY_FILLED,
    OrderStatus.filled: EventType.ORDER_FILLED,
    OrderStatus.cancel_requested: EventType.ORDER_UPDATED,
    OrderStatus.cancelled: EventType.ORDER_CANCELLED,
    OrderStatus.rejected: EventType.ORDER_REJECTED,
    OrderStatus.expired: EventType.ORDER_CANCELLED,
    OrderStatus.failed: EventType.ORDER_FAILED,
    OrderStatus.unknown: EventType.ORDER_UNKNOWN,
}


def event_for(status: OrderStatus) -> EventType:
    return EVENT_FOR[status]


def payload_for(order: ManagedOrder) -> dict[str, object]:
    """What a subscriber needs, and nothing a subscriber must not have.

    The risk snapshot is included because "why was this allowed" is the
    question an operator asks first. Broker credentials are not reachable from
    a `ManagedOrder` at all, so there is nothing to redact -- the boundary is
    structural rather than a filter someone has to maintain.
    """
    return {
        "order_id": order.id,
        "client_order_id": order.client_order_id,
        "broker_order_id": order.broker_order_id,
        "account_id": order.account_id,
        "strategy_id": order.strategy_id,
        "signal_id": order.signal_id,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "mode": order.mode,
        "status": str(order.status),
        "quantity": str(order.quantity),
        "filled_quantity": str(order.filled_quantity),
        "remaining_quantity": str(order.remaining_quantity),
        "average_fill_price": (
            str(order.average_fill_price) if order.average_fill_price is not None else None
        ),
        "stop_loss": str(order.stop_loss) if order.stop_loss is not None else None,
        "take_profit": str(order.take_profit) if order.take_profit is not None else None,
        "risk_decision_id": order.risk_decision_id,
        "reject_reason": order.reject_reason,
        "error_code": order.error_code,
        "needs_reconciliation": order.needs_reconciliation,
        # A report, not an instruction. Stated in the payload so a consumer
        # that grew a side effect is contradicting the message it acted on.
        "advisory": "a report of what happened; nothing may execute from it",
    }
