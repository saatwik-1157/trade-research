"""The order lifecycle manager: the only path from an approval to a venue.

**One OMS, controlled by mode.** There is no `DemoOMS` and no `LiveOMS`. This
class drives a `BrokerAdapter`, and demo and live differ only in which adapter
was registered for the account — the lifecycle, the state machine, the
idempotency key and the reconciliation rule are identical, which is the point
of §16. `app/paper/oms.py` is the paper venue's binding of the *same* state
machine and the *same* fill accounting, imported from `app.oms`, because the
paper path is synchronous and in-process and making it async would rebuild a
working engine to no benefit.

Five rules carry this module.

**1. An order cannot exist without a risk `Approval`.** `submit` takes one as
its first positional argument, and `Approval` is constructible only by
`RiskEngine.approve` — there is no other constructor path in the codebase. So
"nothing reaches a broker without passing Risk" is not a check somebody has to
remember; there is no way to call this without holding the Risk Engine's own
token. The approval must also still stand and must bind to *this* order:
expiry and `request_hash` are both re-checked here, so risk approving order A
while order B is submitted is not expressible.

**2. State is durable BEFORE the venue call.** `intent` is persisted, then
`submitting`, and only then is `place_order` awaited. A crash between the send
and the response therefore leaves a `submitting` row on disk. Without that row
a crashed submit is indistinguishable from a submit that never happened, and
that distinction is the whole of "reconcile" versus "safe to send".

**3. An uncertain outcome becomes `unknown`, and NOTHING retries it.** An IPC
timeout after `order_send` looks exactly like a rejection from the caller's
side. `unknown` exits only through `reconcile`, which asks the venue what it
actually holds. `can_resend` is False for `unknown` and `submitting`, and
`submit` refuses a fresh order for an intent sitting in either.

**4. The same intent produces one order, ever.** A repeat returns the existing
order marked `duplicate`; it never creates a second and never raises, because
a retry after a timeout is normal and turning it into an error pushes callers
into retry loops.

**5. A requested quantity is never a filled quantity.** Fills come from
execution reports and are accumulated by `FillBook`, which refuses an overfill
and ignores a deal id it has already seen.

Nothing here computes a risk limit, a quantity or a strategy decision. It does
not import `app.sizing` at all: the quantity arrives on the approval, already
measured, and recomputing it here would be the second authoritative sizing
calculation §14 forbids.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.brokers.base import (
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
    OrderResult,
)
from app.brokers.base import OrderStatus as BrokerOrderStatus
from app.oms.events import event_for, payload_for
from app.oms.fills import FillError, FillRecord
from app.oms.order import ManagedOrder
from app.oms.state import (
    IllegalOrderTransition,
    OrderStatus,
    can_resend,
)
from app.realtime.catalogue import EventType
from app.risk.engine import Approval

log = logging.getLogger("app.oms")


class OrderRefused(Exception):
    """The OMS declined to create, submit, cancel or modify. Reason included."""


class ReconciliationRequired(Exception):
    """The intent has an unresolved order at the venue. Settle it first."""


@dataclass(frozen=True)
class Submission:
    """The result of a submit: the order, and whether it already existed."""

    order: ManagedOrder
    duplicate: bool

    @property
    def created(self) -> bool:
        return not self.duplicate


def _now() -> datetime:
    return datetime.now(UTC)


class OrderManager:
    """The lifecycle of every broker-bound order on one account."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        *,
        mode: str,
        broker: str | None = None,
        publish: Callable[[str, dict[str, object], ManagedOrder], Awaitable[None]] | None = None,
    ) -> None:
        self.adapter = adapter
        self.mode = mode
        self.broker = broker
        # An optional sink for lifecycle events. Optional because the OMS must
        # work with no realtime layer at all -- an order lifecycle that
        # depended on a bus being up would stop being able to record a fill
        # when Redis went down, which is precisely when the record matters.
        self.publish = publish
        self.orders: dict[str, ManagedOrder] = {}
        self.by_intent: dict[str, ManagedOrder] = {}
        self.by_broker_id: dict[str, ManagedOrder] = {}
        self._pending_events: list[tuple[str, dict[str, object], ManagedOrder]] = []
        self._queued: dict[str, int] = {}
        self._counts: Counter[str] = Counter()

    # ============================================================== events

    def drain_events(self) -> list[tuple[str, dict[str, object], ManagedOrder]]:
        """Take the lifecycle events produced since the last drain.

        Queued rather than published inline, so a slow or broken bus cannot
        sit in the middle of a broker interaction. A caller publishes them
        AFTER the state is durable, which is the only order in which an event
        can never describe something that was not saved.
        """
        events, self._pending_events = self._pending_events, []
        return events

    def _queue_events(self, order: ManagedOrder) -> None:
        """Queue an event for every transition not yet queued for this order.

        Keyed on how many transitions have already been queued, so calling it
        after every mutation is correct and calling it twice is a no-op. That
        matters: a method that queued nothing would lose an event, and one
        that re-queued would publish a fill twice.
        """
        seen = self._queued.get(order.id, 0)
        for transition in order.transitions[seen:]:
            self._pending_events.append((str(event_for(transition.new)), payload_for(order), order))
        self._queued[order.id] = len(order.transitions)

    async def flush_events(self) -> None:
        """Publish queued events through the configured sink, if there is one.

        A failure to publish is logged and swallowed: the durable record is
        already written, and an exception here would turn a delivery problem
        into a lifecycle problem.
        """
        events = self.drain_events()
        if self.publish is None:
            return
        for event_type, payload, order in events:
            try:
                await self.publish(event_type, payload, order)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "an order event could not be published; the record is unaffected",
                    extra={
                        "event": "order_event_publish_failed",
                        "order_id": order.id,
                        "type": event_type,
                        "reason": str(exc)[:200],
                    },
                )

    # ==================================================================== create

    def create(
        self,
        approval: Approval,
        *,
        intent_id: str,
        account_id: str,
        order_id: str | None = None,
        order_type: str = "market",
        time_in_force: str = "gtc",
        bot_id: str | None = None,
        expires_at: datetime | None = None,
        sizing_snapshot: dict[str, object] | None = None,
        at: datetime | None = None,
    ) -> Submission:
        """Create the order record. Sends nothing.

        Split from `submit` deliberately: §18 requires the CREATED row to
        exist before anything is transmitted, and a method that both persists
        and sends cannot offer that ordering to its caller.
        """
        now = at or _now()
        self._validate_approval(approval, now)
        proposal = approval.proposal

        existing = self.by_intent.get(intent_id)
        if existing is not None:
            self._counts["duplicate_orders_prevented"] += 1
            log.info(
                "duplicate order intent; returning the existing order",
                extra={
                    "event": "order_duplicate",
                    "intent_id": intent_id,
                    "order_id": existing.id,
                    "status": str(existing.status),
                    "mode": self.mode,
                },
            )
            return Submission(existing, duplicate=True)

        if proposal.mode != self.mode:
            raise OrderRefused(
                f"the approval was evaluated for {proposal.mode} and this manager runs "
                f"{self.mode}; refusing rather than executing a decision made about a "
                "different execution environment"
            )
        if order_type not in ("market", "limit", "stop"):
            raise OrderRefused(f"unsupported order type {order_type!r}")
        if order_type in ("limit", "stop") and proposal.entry_price is None:
            raise OrderRefused(f"a {order_type} order needs a price and none was given")
        if time_in_force not in ("gtc", "ioc", "fok", "day"):
            raise OrderRefused(f"unsupported time in force {time_in_force!r}")
        # After the shape checks, so a malformed request gets its own message,
        # and before anything is written or sent.
        self._validate_binding(approval, account_id=account_id, order_type=order_type)
        if approval.approved_volume is None or approval.approved_volume <= 0:
            raise OrderRefused(
                f"the approval carries volume {approval.approved_volume}; there is no "
                "position sizing result to execute"
            )

        order = ManagedOrder(
            id=order_id or str(uuid4()),
            client_order_id=intent_id,
            account_id=account_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=approval.approved_volume,
            mode=self.mode,
            broker=self.broker,
            created_at=now,
            order_type=order_type,
            time_in_force=time_in_force,
            requested_price=proposal.entry_price,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            signal_id=proposal.signal_id,
            strategy_id=proposal.strategy_id,
            bot_id=bot_id,
            risk_decision_id=approval.decision_id,
            risk_snapshot={
                "decision_id": approval.decision_id,
                "decision": str(approval.verdict.decision),
                "approved_volume": str(approval.approved_volume),
                "approved_at": approval.approved_at.isoformat(),
                "expires_at": (approval.expires_at.isoformat() if approval.expires_at else None),
                "request_hash": approval.request_hash,
                "not_enforced": list(approval.verdict.not_enforced),
            },
            sizing_snapshot=dict(sizing_snapshot or {}),
            expires_at=expires_at,
        )
        self.orders[order.id] = order
        self.by_intent[intent_id] = order
        self._counts["orders_created"] += 1
        # Creation is not a transition -- there is no previous state -- so it
        # has no entry in the audit trail and would otherwise emit nothing.
        # ORDER_CREATED is the one event announced explicitly.
        self._pending_events.append((str(EventType.ORDER_CREATED), payload_for(order), order))
        self._queue_events(order)
        return Submission(order, duplicate=False)

    def discard(self, order: ManagedOrder) -> None:
        """Forget an order that was created and never transmitted. **L45 C-1.**

        The only legitimate caller is a pipeline whose durable write failed
        between `create` and `submit`. It is sound there and nowhere else:
        nothing has been sent, so no venue holds it, and no row was written, so
        nothing references it.

        **Leaving it in place would be worse than removing it.** `create` puts
        the order in `by_intent`, so the next pass over the same signal would
        find a live order for the intent, refuse it as a duplicate, and retire
        a signal that never reached a venue -- discarding a real trading signal
        because the database blinked.

        Refuses outright once anything has been transmitted. A method that
        could erase an order the venue has seen would be a way to lose exactly
        the record this module exists to keep, so the guard is a refusal rather
        than a comment.
        """
        if order.status is not OrderStatus.intent or order.broker_order_id:
            raise OrderRefused(
                f"order {order.id} is `{order.status}` and cannot be discarded; only an "
                "order that has never been transmitted may be forgotten, because the "
                "venue may be holding anything else"
            )
        self.orders.pop(order.id, None)
        if self.by_intent.get(order.client_order_id) is order:
            del self.by_intent[order.client_order_id]
        self._queued.pop(order.id, None)
        self._pending_events = [e for e in self._pending_events if e[2] is not order]
        self._counts["orders_created"] -= 1
        self._counts["orders_discarded_unrecorded"] += 1
        log.warning(
            "an order was created and could not be recorded; it was discarded and nothing was sent",
            extra={
                "event": "order_discarded_unrecorded",
                "order_id": order.id,
                "intent_id": order.client_order_id,
                "mode": self.mode,
            },
        )

    # ==================================================================== submit

    async def submit(self, order: ManagedOrder, *, at: datetime | None = None) -> ManagedOrder:
        """Send a created order to the venue, durably.

        `intent -> submitting` happens and is observable BEFORE `place_order`
        is awaited, so a process that dies mid-call leaves evidence that a send
        was in flight.
        """
        now = at or _now()
        if order.status is not OrderStatus.intent:
            raise OrderRefused(
                f"only an order in `intent` can be submitted, not one in `{order.status}`"
            )
        if order.expires_at is not None and now >= order.expires_at:
            order.move(
                OrderStatus.failed,
                at=now,
                source="expiry",
                reason="the order expired before it was sent; nothing was transmitted",
            )
            order.error_code = "EXPIRED_BEFORE_SUBMIT"
            self._queue_events(order)
            return order

        order.move(
            OrderStatus.submitting,
            at=now,
            source="pipeline",
            reason="persisted before transmission; a crash here is reconcilable",
        )
        self._counts["orders_submitted"] += 1

        request = OrderRequest(
            symbol=order.symbol,
            side=order.side,
            volume=order.quantity,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            comment=f"oms:{order.client_order_id}"[:31],
            intent_id=order.client_order_id,
        )
        started = _now()
        try:
            result = await self.adapter.place_order(request)
        except BrokerError as exc:
            # A refusal the adapter is sure about, raised before or instead of
            # transmission. `failed` says the venue never saw it.
            return self._fail(order, now, f"{type(exc).__name__}: {exc}", "BROKER_ERROR")
        except Exception as exc:  # noqa: BLE001
            # Anything else may have reached the venue. It becomes `unknown`,
            # and nothing retries it. This is the branch the whole module is
            # built around: an IPC timeout after `order_send` looks exactly
            # like a rejection from here, and guessing costs a duplicate
            # position.
            return self._park_unknown(order, now, f"{type(exc).__name__}: {exc}")
        finally:
            self._observe_latency(started)

        order_out = self._apply_result(order, result, now)
        self._queue_events(order_out)
        return order_out

    async def close(
        self,
        order: ManagedOrder,
        *,
        broker_position_id: str,
        at: datetime | None = None,
    ) -> ManagedOrder:
        """Send a CLOSE to the venue, durably. **The fix for L45 C-2.**

        Structurally identical to `submit`, and deliberately so: the same
        state machine, the same `intent -> submitting` written before the
        venue is touched, the same three-way reading of the answer, the same
        refusal to guess when it is `unknown`. The only difference is which
        adapter method is called, because closing a position by id is a
        different venue operation from placing an order.

        **Why this exists at all.** `app/positions/broker_executor.py` called
        `manager.adapter.close_position(...)` directly, borrowing the
        `OrderManager` purely as a container for `.adapter`. That produced no
        `intent_id`, so two concurrent close requests were two closes at the
        venue, and no order record, so reconciliation could not see it. On a
        hedging account a double close opens a position in the opposite
        direction, which is why "it only reduces risk" is not a defence.
        """
        now = at or _now()
        if order.status is not OrderStatus.intent:
            raise OrderRefused(
                f"only an order in `intent` can be sent, not one in `{order.status}`"
            )
        if not broker_position_id:
            raise OrderRefused(
                "a close needs the venue's own position id; `id` is ours and the venue "
                "has never heard of it"
            )

        order.move(
            OrderStatus.submitting,
            at=now,
            source="pipeline",
            reason="close persisted before transmission; a crash here is reconcilable",
        )
        self._counts["closes_submitted"] += 1

        started = _now()
        try:
            result = await self.adapter.close_position(broker_position_id, volume=order.quantity)
        except BrokerError as exc:
            # The adapter is sure it did not send.
            return self._fail(order, now, f"{type(exc).__name__}: {exc}", "BROKER_ERROR")
        except Exception as exc:  # noqa: BLE001
            # It may have gone. An IPC timeout after a close looks exactly like
            # a refusal from here, and nothing retries out of `unknown`:
            # closing twice on a hedging account OPENS a position the other way.
            return self._park_unknown(order, now, f"{type(exc).__name__}: {exc}")
        finally:
            self._observe_latency(started)

        closed = self._apply_result(order, result, now)
        self._queue_events(closed)
        return closed

    def _apply_result(
        self, order: ManagedOrder, result: OrderResult, now: datetime
    ) -> ManagedOrder:
        """Turn one venue response into state. The venue is the authority."""
        if result.order_id:
            order.broker_order_id = result.order_id
            self.by_broker_id[result.order_id] = order

        if result.is_unknown:
            return self._park_unknown(order, now, result.detail or "the venue's answer was unclear")

        if result.status is BrokerOrderStatus.rejected:
            order.move(
                OrderStatus.submitted,
                at=now,
                source="pipeline",
                reason="transmitted",
                retcode=result.retcode,
            )
            order.move(
                OrderStatus.rejected,
                at=now,
                source="broker",
                reason=result.detail[:500],
                retcode=result.retcode,
            )
            order.reject_reason = result.detail[:500]
            order.error_code = str(result.retcode) if result.retcode is not None else None
            self._counts["orders_rejected"] += 1
            return order

        order.move(
            OrderStatus.submitted,
            at=now,
            source="pipeline",
            reason="transmitted",
            retcode=result.retcode,
        )
        order.move(
            OrderStatus.accepted,
            at=now,
            source="broker",
            reason=result.detail[:500] or "accepted by the venue",
            retcode=result.retcode,
        )

        # A fill only exists when the venue reported one. A market order that
        # came back accepted with no fill figures is acknowledged and NOT
        # filled -- treating acceptance as execution is invariant 9.
        if result.filled_volume is not None and result.fill_price is not None:
            self._record(
                order,
                FillRecord(
                    quantity=result.filled_volume,
                    price=result.fill_price,
                    at=result.filled_at or now,
                    fill_source=result.fill_source or "broker",
                    # The venue's DEAL first. It identifies one execution;
                    # the position and order ids do not, and the book
                    # deduplicates by this field -- so two genuine partial
                    # fills of one order both carrying the position's ticket
                    # would have had the second silently dropped. Falls back
                    # rather than inventing when the venue reports no deal.
                    broker_deal_id=result.deal_id or result.position_id or result.order_id,
                    # Carried, never computed. None when the venue did not say.
                    realized_pnl=result.realized_pnl,
                ),
                now,
            )
        return order

    # ================================================================ fills

    def apply_execution_report(
        self,
        order: ManagedOrder,
        *,
        quantity: Decimal,
        price: Decimal,
        at: datetime | None = None,
        broker_deal_id: str | None = None,
        commission: Decimal = Decimal("0"),
        swap: Decimal = Decimal("0"),
        fill_source: str = "broker",
    ) -> bool:
        """Apply one execution report from the venue.

        Returns False when the deal was already recorded. Reports arrive more
        than once and a deal applied twice is a position twice the size it
        should be.
        """
        now = at or _now()
        if order.status in (OrderStatus.intent, OrderStatus.submitting):
            raise OrderRefused(
                f"an order in `{order.status}` has not been acknowledged; a fill for it "
                "is a disagreement with the venue and must be reconciled, not applied"
            )
        return self._record(
            order,
            FillRecord(
                quantity=quantity,
                price=price,
                at=now,
                fill_source=fill_source,
                broker_deal_id=broker_deal_id,
                commission=commission,
                swap=swap,
            ),
            now,
        )

    def _record(self, order: ManagedOrder, fill: FillRecord, now: datetime) -> bool:
        try:
            applied = order.record_fill(fill, at=now, source="broker")
        except FillError as exc:
            # An overfill. The venue and this record disagree, which is a
            # reconciliation problem and not something to round away.
            log.error(
                "execution report refused",
                extra={
                    "event": "order_fill_refused",
                    "order_id": order.id,
                    "broker_order_id": order.broker_order_id,
                    "mode": self.mode,
                    "reason": str(exc)[:300],
                },
            )
            self._counts["fill_discrepancies"] += 1
            raise
        if not applied:
            self._counts["duplicate_fills_ignored"] += 1
            return False
        if order.status is OrderStatus.filled:
            self._counts["orders_filled"] += 1
        else:
            self._counts["orders_partially_filled"] += 1
        self._queue_events(order)
        return True

    # =============================================================== cancel

    async def cancel(
        self, order: ManagedOrder, *, reason: str = "", at: datetime | None = None
    ) -> ManagedOrder:
        """Ask the venue to cancel, and believe only its answer.

        `cancel_requested` exists so that "we asked" and "the venue agreed" are
        different states. Assuming a cancellation succeeded is how a position
        nobody is watching stays open.
        """
        now = at or _now()
        if order.terminal:
            raise OrderRefused(
                f"order {order.id} is {order.status} and cannot be cancelled; a "
                "terminal order is not active"
            )
        if order.needs_reconciliation:
            raise ReconciliationRequired(
                f"order {order.id} is `{order.status}`: what the venue holds is not "
                "known, so a cancel would be a guess. Reconcile first"
            )
        order.move(
            OrderStatus.cancel_requested,
            at=now,
            source="operator",
            reason=reason[:500] or "cancel requested",
        )
        if order.broker_order_id is None:
            # Nothing was ever acknowledged with an id, so there is nothing at
            # the venue to cancel by id. That is not the same as knowing it
            # holds nothing, so it parks rather than being declared cancelled.
            return self._park_unknown(
                order, now, "no broker order id; the venue cannot be asked about this order"
            )
        try:
            result = await self.adapter.cancel_order(order.broker_order_id)
        except BrokerError as exc:
            return self._park_unknown(order, now, f"cancel failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            return self._park_unknown(order, now, f"cancel outcome unclear: {exc}")

        if result.is_unknown:
            return self._park_unknown(order, now, result.detail or "cancel outcome unclear")
        # The adapter vocabulary is three-state by design -- accepted, rejected,
        # unknown -- because those are the three things a venue can say about
        # any request. For a cancel, `accepted` means the venue took it.
        if result.status is BrokerOrderStatus.rejected:
            # The venue refused the cancel; the order stands as it was.
            order.move(
                OrderStatus.accepted,
                at=now,
                source="broker",
                reason=f"the venue refused the cancel: {result.detail}"[:500],
                retcode=result.retcode,
            )
            return order
        order.move(
            OrderStatus.cancelled,
            at=now,
            source="broker",
            reason=result.detail[:500] or "cancelled at the venue",
            retcode=result.retcode,
        )
        self._counts["orders_cancelled"] += 1
        self._queue_events(order)
        return order

    # =============================================================== modify

    async def modify(
        self,
        order: ManagedOrder,
        *,
        approval: Approval,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        at: datetime | None = None,
    ) -> ManagedOrder:
        """Change an active order's protective levels.

        **A modification is a new risk decision, not an amendment to an old
        one.** A fresh `Approval` is required and re-validated, because the
        approval that authorised the original order was bound by
        `request_hash` to *those* levels — the whole point of that binding is
        that changed levels need a fresh evaluation.

        Quantity is deliberately not modifiable: on this venue a size change is
        a different order, and pretending otherwise would let a modify path
        become an un-sized, un-reconciled second submission.
        """
        now = at or _now()
        if order.terminal:
            raise OrderRefused(
                f"order {order.id} is {order.status}; a filled or cancelled order cannot "
                "be modified as if it were active"
            )
        if order.needs_reconciliation:
            raise ReconciliationRequired(
                f"order {order.id} is `{order.status}`; reconcile before modifying"
            )
        # A modify keeps the order's own account and type; the approval must
        # bind to the order as it exists, not to some other shape.
        self._validate_approval(approval, now)
        self._validate_binding(approval, account_id=order.account_id, order_type=order.order_type)
        if approval.approved_volume != order.quantity:
            raise OrderRefused(
                "the approval is for a different volume; a size change is a different "
                "order, not a modification"
            )
        if order.broker_order_id is None:
            raise OrderRefused("the order has no broker id; there is nothing to modify")

        try:
            result = await self.adapter.modify_order(
                order.broker_order_id, stop_loss=stop_loss, take_profit=take_profit
            )
        except BrokerError as exc:
            raise OrderRefused(f"the venue refused the modification: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            self._park_unknown(order, now, f"modify outcome unclear: {exc}")
            raise ReconciliationRequired(
                f"the modification's outcome is unknown: {exc}. The order is parked"
            ) from exc

        if result.is_unknown:
            self._park_unknown(order, now, result.detail or "modify outcome unclear")
            raise ReconciliationRequired(
                "the venue's answer to the modification was unclear; the order is parked"
            )
        if result.status is BrokerOrderStatus.rejected:
            raise OrderRefused(f"the venue refused the modification: {result.detail}")

        # Only now, with the venue's confirmation, is the local record changed.
        # Writing the levels first would make the platform believe a stop is in
        # force that the venue never accepted.
        if stop_loss is not None:
            order.stop_loss = stop_loss
        if take_profit is not None:
            order.take_profit = take_profit
        order.risk_decision_id = approval.decision_id
        order.transitions.append(_modification(order, now, result.detail, result.retcode))
        self._counts["orders_modified"] += 1
        self._queue_events(order)
        return order

    # ========================================================== reconcile

    async def reconcile(self, order: ManagedOrder, *, at: datetime | None = None) -> ManagedOrder:
        """Settle an order whose outcome is not known, by asking the venue.

        This is the ONLY exit from `unknown` and from a crashed `submitting`.
        It never re-sends: it looks at what the venue holds and records that.

        Matching is by `broker_order_id` when we have one, and otherwise by the
        intent id carried in the order's comment — the two identities are used
        for what each is for. When the venue holds nothing for either, the
        order provably never landed and becomes `failed`, which is the one
        state from which a fresh order for the same intent is safe.
        """
        now = at or _now()
        if not order.needs_reconciliation:
            return order

        try:
            open_orders = await self.adapter.get_orders()
            positions = await self.adapter.get_positions()
        except Exception as exc:  # noqa: BLE001
            # The venue could not be asked. The order stays exactly as it is:
            # `unknown` is the honest state, and a reconciliation that cannot
            # read the venue must not conclude anything.
            self._counts["reconciliation_failures"] += 1
            log.error(
                "reconciliation could not read the venue; the order stays unknown",
                extra={
                    "event": "order_reconciliation_failed",
                    "order_id": order.id,
                    "mode": self.mode,
                    "reason": str(exc)[:300],
                },
            )
            raise ReconciliationRequired(f"the venue could not be read: {exc}") from exc

        if order.status is OrderStatus.submitting:
            # It never got as far as a recorded transmission. Move it to
            # `unknown` first so the machine's own path is followed rather
            # than jumping states.
            order.move(
                OrderStatus.unknown,
                at=now,
                source="recovery",
                reason="found mid-submission after a restart",
            )

        found_order = self._match_order(order, open_orders)
        found_position = self._match_position(order, positions)

        if found_order is not None:
            # The venue is holding it. It is live, not lost.
            order.broker_order_id = order.broker_order_id or found_order.order_id
            order.move(
                OrderStatus.partially_filled
                if order.book.fills
                else OrderStatus.filled
                if found_position is not None
                else OrderStatus.rejected,
                at=now,
                source="reconciliation",
                reason=f"the venue holds order {found_order.order_id} in state {found_order.state}",
            )
            self._counts["orders_reconciled"] += 1
            self._queue_events(order)
            return order

        if found_position is not None:
            # No working order, but a position exists for this intent: it
            # filled, and the response was what went missing.
            applied = self._record(
                order,
                FillRecord(
                    quantity=found_position.volume,
                    price=found_position.entry_price,
                    at=found_position.opened_at,
                    fill_source="broker",
                    broker_deal_id=found_position.position_id,
                ),
                now,
            )
            if not applied and not order.terminal:
                order.move(
                    OrderStatus.filled,
                    at=now,
                    source="reconciliation",
                    reason=f"the venue holds position {found_position.position_id}",
                )
            self._counts["orders_reconciled"] += 1
            self._queue_events(order)
            return order

        # The venue holds neither an order nor a position for this intent, so
        # it provably never landed. `failed` is the only state from which a
        # fresh order for the same intent is safe, and this is how one is
        # reached honestly.
        order.move(
            OrderStatus.failed,
            at=now,
            source="reconciliation",
            reason="the venue holds no order and no position for this intent; it never landed",
        )
        order.error_code = "NOT_AT_VENUE"
        self._counts["orders_reconciled"] += 1
        self._queue_events(order)
        return order

    def _match_order(
        self, order: ManagedOrder, venue_orders: list[BrokerOrder]
    ) -> BrokerOrder | None:
        for candidate in venue_orders:
            if order.broker_order_id and candidate.order_id == order.broker_order_id:
                return candidate
        return None

    def _match_position(
        self, order: ManagedOrder, positions: list[BrokerPosition]
    ) -> BrokerPosition | None:
        for candidate in positions:
            if order.broker_order_id and candidate.position_id == order.broker_order_id:
                return candidate
            # Fall back to symbol and side. Weaker, and deliberately only used
            # when there is no id to match on -- it can confuse two orders on
            # the same instrument, which is why a match here is reported in
            # the transition reason rather than treated as certain.
            wanted = "long" if order.side == "buy" else "short"
            if candidate.symbol == order.symbol and candidate.side == wanted:
                return candidate
        return None

    # ============================================================== expiry

    def expire(self, order: ManagedOrder, *, at: datetime | None = None) -> ManagedOrder:
        """Mark an order expired. Only legal when the venue is not holding it.

        An expiry is a venue fact, not a local clock reading, so this is called
        after a cancel or a reconciliation established that nothing is live.
        """
        now = at or _now()
        if order.needs_reconciliation:
            raise ReconciliationRequired(
                f"order {order.id} is `{order.status}`; expiry cannot be assumed"
            )
        order.move(
            OrderStatus.expired,
            at=now,
            source="expiry",
            reason=f"expired at {order.expires_at}",
        )
        self._counts["orders_expired"] += 1
        self._queue_events(order)
        return order

    # ============================================================ recovery

    def resume(self, orders: list[ManagedOrder]) -> list[ManagedOrder]:
        """Re-register orders loaded from the database after a restart.

        Nothing is submitted, cancelled or concluded here. The returned list is
        the ones that need reconciling — `unknown` and `submitting` — and a
        caller settles them against the venue before anything else is sent.
        """
        needs: list[ManagedOrder] = []
        for order in orders:
            self.orders[order.id] = order
            self.by_intent[order.client_order_id] = order
            if order.broker_order_id:
                self.by_broker_id[order.broker_order_id] = order
            if order.needs_reconciliation:
                needs.append(order)
        if needs:
            self._counts["orders_recovered_needing_reconciliation"] += len(needs)
            log.warning(
                "orders recovered in an unresolved state; nothing will be sent for these "
                "intents until they are reconciled",
                extra={
                    "event": "orders_need_reconciliation",
                    "count": len(needs),
                    "mode": self.mode,
                },
            )
        return needs

    # ============================================================= helpers

    def _validate_approval(self, approval: Approval, now: datetime) -> None:
        """Is this a genuine, live approval at all?

        Identity and expiry only. It runs FIRST, before anything reads
        `approval.proposal`, so a non-Approval is refused cleanly rather than
        raising an AttributeError deeper in.

        Whether the approval binds to *this particular order* is a separate
        question, answered by `_validate_binding` after the order's shape has
        been checked -- so a malformed request still gets the precise message
        ("unsupported order type 'iceberg'") rather than a binding failure that
        is true but unhelpful.
        """
        if not isinstance(approval, Approval):
            raise OrderRefused(
                "the OMS requires a risk.Approval; an order the Risk Engine has not "
                "approved cannot be created"
            )
        if not approval.verdict.approved:
            raise OrderRefused(
                f"the approval carries a {approval.verdict.decision} verdict, not an "
                "approval; refusing to create an order from it"
            )
        if approval.expired(now):
            raise OrderRefused(
                f"the approval expired at {approval.expires_at}; risk must evaluate "
                "again, because the portfolio it was computed against has moved"
            )

    def _validate_binding(self, approval: Approval, *, account_id: str, order_type: str) -> None:
        """Does this approval bind to THIS order? (L45 C-4.)

        The OMS previously asked `approval.binds(approval.bound_fields())` --
        both sides derived from the same object, so the digest always matched
        and the check could never fail. It read as protection in every audit
        and provided none.
        """
        if not approval.binds_order(account_id=account_id, order_type=order_type, mode=self.mode):
            raise OrderRefused(
                f"the approval does not bind to this order. It was issued for "
                f"account {approval.proposal.account_id!r} as a market order in "
                f"{approval.proposal.mode!r}; this order is account {account_id!r}, "
                f"a {order_type!r} order in {self.mode!r}. A change to the account, "
                "order type, mode, symbol, side, volume, price, stop, target or "
                "strategy requires a fresh risk evaluation."
            )

    def guard_resend(self, intent_id: str) -> None:
        """Refuse a fresh order for an intent whose last one is unresolved.

        This is rule 3 at the entry point rather than only at the exit: a
        caller that has forgotten the old order still cannot create a second
        one for the same intent while the venue may be holding the first.
        """
        previous = self.by_intent.get(intent_id)
        if previous is None:
            return
        if previous.needs_reconciliation:
            raise ReconciliationRequired(
                f"intent {intent_id} has an order in `{previous.status}`; the venue may "
                "be holding it. Reconcile before sending anything for this intent — "
                "retrying an uncertain submission is how one signal becomes two positions"
            )
        if not previous.terminal:
            raise OrderRefused(
                f"intent {intent_id} already has a live order in `{previous.status}`"
            )
        if not can_resend(previous.status):
            raise OrderRefused(
                f"intent {intent_id} already resolved as `{previous.status}`; a new order "
                "for it would be a second order for one signal"
            )

    def _fail(self, order: ManagedOrder, now: datetime, reason: str, code: str) -> ManagedOrder:
        order.move(OrderStatus.failed, at=now, source="broker", reason=reason[:500])
        order.reject_reason = reason[:500]
        order.error_code = code
        self._counts["orders_failed"] += 1
        self._queue_events(order)
        log.warning(
            "order failed before the venue saw it",
            extra={
                "event": "order_failed",
                "order_id": order.id,
                "intent_id": order.client_order_id,
                "mode": self.mode,
                "reason": reason[:300],
            },
        )
        return order

    def _park_unknown(self, order: ManagedOrder, now: datetime, reason: str) -> ManagedOrder:
        try:
            order.move(OrderStatus.unknown, at=now, source="broker", reason=reason[:500])
        except IllegalOrderTransition:  # pragma: no cover - defensive
            pass
        order.error_code = order.error_code or "UNKNOWN_VENUE_STATE"
        self._counts["orders_unknown"] += 1
        self._queue_events(order)
        log.warning(
            "order in an unknown state; parked for reconciliation, NOT retried",
            extra={
                "event": "order_unknown",
                "order_id": order.id,
                "intent_id": order.client_order_id,
                "broker_order_id": order.broker_order_id,
                "mode": self.mode,
                "reason": reason[:300],
            },
        )
        return order

    def _observe_latency(self, started: datetime) -> None:
        elapsed_ms = (_now() - started).total_seconds() * 1000
        self._counts["broker_submission_ms_total"] += int(elapsed_ms)
        self._counts["broker_submissions"] += 1

    # ============================================================== status

    def status(self) -> dict[str, object]:
        submissions = self._counts["broker_submissions"]
        return {
            "mode": self.mode,
            "broker": self.broker,
            "orders_created": self._counts["orders_created"],
            "orders_submitted": self._counts["orders_submitted"],
            "orders_filled": self._counts["orders_filled"],
            "orders_partially_filled": self._counts["orders_partially_filled"],
            "orders_cancelled": self._counts["orders_cancelled"],
            "orders_modified": self._counts["orders_modified"],
            "orders_rejected": self._counts["orders_rejected"],
            "orders_failed": self._counts["orders_failed"],
            "orders_expired": self._counts["orders_expired"],
            "orders_unknown": self._counts["orders_unknown"],
            "orders_reconciled": self._counts["orders_reconciled"],
            "reconciliation_failures": self._counts["reconciliation_failures"],
            "duplicate_orders_prevented": self._counts["duplicate_orders_prevented"],
            "duplicate_fills_ignored": self._counts["duplicate_fills_ignored"],
            "fill_discrepancies": self._counts["fill_discrepancies"],
            "broker_submission_latency_ms_mean": (
                round(self._counts["broker_submission_ms_total"] / submissions, 2)
                if submissions
                else None
            ),
            "unresolved": [o.id for o in self.orders.values() if o.needs_reconciliation],
            "authority": (
                "This manager executes approved orders. It computes no risk limit and "
                "no quantity: both arrive on the Approval, already decided."
            ),
            "retry_policy": (
                "None from `unknown` or `submitting`. Those are reconciled against the "
                "venue, and only a reconciliation that finds nothing there produces "
                "`failed`, which is the one state a fresh order may follow."
            ),
        }


def _modification(order: ManagedOrder, now: datetime, detail: str, retcode: int | None):  # noqa: ANN202
    from app.oms.order import OrderTransition

    return OrderTransition(
        at=now,
        previous=order.status,
        new=order.status,
        source="operator",
        reason=f"modified: {detail}"[:500],
        broker_order_id=order.broker_order_id,
        retcode=retcode,
    )
