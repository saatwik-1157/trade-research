"""Durable order state: the `orders`, `order_events` and `executions` tables.

**The ordering is the point.** Brief §18 forbids "broker submit, save later",
because a crash in that window leaves a venue holding an order the platform
has no record of. So the caller's sequence is:

    create()  ->  persist()      # the row exists
    submit()  ->  persist()      # `submitting` is on disk before the send
              ->  place_order    # the venue is called
              ->  persist()      # whatever it said

`persist` is idempotent on the order row and append-only on the event log, so
calling it more often than necessary is safe and calling it less is not.

**Nothing here is a second order model.** The tables are L05's, unchanged
except for the columns migration 0010 added, and `ManagedOrder` is mapped onto
them field by field. There is no ORM relationship traversal and no lazy load:
this module reads and writes explicitly, because a lazy load inside a
lifecycle step is a database call in the middle of a broker interaction.

**Executions are the source of truth for fills.** `orders.filled_quantity` and
`orders.average_fill_price` are written from the `FillBook`, which is itself
built from execution rows on the way back in. So the derived figures can
always be recomputed from the record, and a disagreement between them is
visible rather than hidden.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import BrokerAccount, PaperAccount
from app.models.execution import Execution, Order, OrderEvent
from app.oms.fills import FillRecord
from app.oms.order import ManagedOrder
from app.oms.state import NEEDS_RECONCILIATION, OrderStatus

log = logging.getLogger("app.oms.repository")


def _naive(value: datetime | None) -> datetime | None:
    """The DateTime columns are naive UTC; the lifecycle is aware UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=None)


class OrderRepository:
    """Reads and writes one order's durable state. Holds no session of its own."""

    def __init__(self, symbol_ids: dict[str, str] | None = None) -> None:
        # internal symbol code -> symbols.id. Passed in rather than looked up
        # per call: an order write must not become a symbol query.
        self.symbol_ids = symbol_ids or {}

    # =============================================================== writing

    async def persist(self, db: AsyncSession, order: ManagedOrder) -> Order | None:
        """Write the order and any transitions not yet recorded.

        Returns the row, or None when the symbol cannot be resolved -- which is
        reported rather than raised, because losing the audit trail of an order
        that already exists at a venue is worse than a missing row.
        """
        symbol_id = self.symbol_ids.get(order.symbol)
        row = await db.get(Order, order.id)

        if row is None:
            if symbol_id is None:
                log.error(
                    "cannot persist an order: the symbol is not resolvable",
                    extra={
                        "event": "order_persist_failed",
                        "order_id": order.id,
                        "symbol": order.symbol,
                    },
                )
                return None
            row = Order(
                id=order.id,
                intent_id=order.client_order_id,
                mode=order.mode,
                symbol_id=symbol_id,
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity,
                source="pipeline",
                created_at=_naive(order.created_at),
                **await self._account_column(db, order),
            )
            db.add(row)

        self._apply(row, order)
        await db.flush()
        await self._write_events(db, row, order)
        await self._write_executions(db, row, order)
        return row

    async def _account_column(self, db: AsyncSession, order: ManagedOrder) -> dict[str, str]:
        """Which account column this order belongs in, or `{}` if neither.

        **Added with the L45 C-1 fix, because restart recovery needs it.**
        `persist` never wrote an account, so every stored order had
        `paper_account_id` and `broker_account_id` NULL -- which made
        `_rehydrate` return `account_id=""` and left a recovered order
        belonging to nobody. It also emptied two live queries that filter on
        exactly those columns (`analytics/service.py` and the paper account's
        order list), so per-account order history was silently always empty.

        The row is looked up rather than assumed. Both columns carry a foreign
        key, so writing an id with no row turns a successful order into an
        integrity error at commit -- and losing the record of an order that may
        already sit at a venue is the one outcome this module exists to
        prevent. An unrecognised account is reported and left NULL, which is
        exactly the behaviour that was there before, now only for the case that
        genuinely cannot be resolved.
        """
        account_id = order.account_id
        if not account_id:
            return {}
        paper = order.mode == "paper"
        model = PaperAccount if paper else BrokerAccount
        column = "paper_account_id" if paper else "broker_account_id"
        if await db.get(model, account_id) is None:
            log.warning(
                "an order's account is not a known account row; the order is still "
                "recorded, with no account set",
                extra={
                    "event": "order_account_unresolved",
                    "order_id": order.id,
                    "account_id": account_id,
                    "mode": order.mode,
                },
            )
            return {}
        return {column: account_id}

    def _apply(self, row: Order, order: ManagedOrder) -> None:
        row.status = str(order.status)
        row.broker_order_id = order.broker_order_id
        row.requested_price = order.requested_price
        row.stop_loss = order.stop_loss
        row.take_profit = order.take_profit
        row.signal_id = order.signal_id
        row.risk_decision_id = order.risk_decision_id
        row.time_in_force = order.time_in_force
        row.filled_quantity = order.filled_quantity
        row.average_fill_price = order.average_fill_price
        row.expires_at = _naive(order.expires_at)
        row.submitted_at = _naive(order.submitted_at)
        row.acknowledged_at = _naive(order.acknowledged_at)
        row.filled_at = _naive(order.filled_at)
        row.cancelled_at = _naive(order.cancelled_at)
        row.reject_reason = order.reject_reason
        row.error_code = order.error_code
        row.sizing = order.sizing_snapshot or None

    async def _write_events(self, db: AsyncSession, row: Order, order: ManagedOrder) -> None:
        """Append transitions the log does not have. The log is append-only, so
        this counts what is stored and writes the tail rather than rewriting."""
        stored = (
            await db.scalars(select(OrderEvent.id).where(OrderEvent.order_id == row.id))
        ).all()
        for index, transition in enumerate(order.transitions[len(stored) :], start=len(stored)):
            db.add(
                OrderEvent(
                    order_id=row.id,
                    sequence=index,
                    event_type=str(transition.new)[:24],
                    occurred_at=_naive(transition.at) or datetime.utcnow(),  # noqa: DTZ003
                    comment=transition.reason[:500] or None,
                    retcode=transition.retcode,
                    previous_status=str(transition.previous),
                    new_status=str(transition.new),
                    source=transition.source,
                    broker_order_id=transition.broker_order_id,
                )
            )

    async def _write_executions(self, db: AsyncSession, row: Order, order: ManagedOrder) -> None:
        """Write fills the table does not have, matched by the venue's deal id.

        Matching by id and not by shape: two genuine fills of the same size at
        the same price are a real thing that happens, and de-duplicating them
        by appearance would lose one.
        """
        if not order.book.fills:
            return
        stored = (
            await db.scalars(select(Execution.broker_deal_id).where(Execution.order_id == row.id))
        ).all()
        seen = {d for d in stored if d is not None}
        written = len(stored)
        for index, fill in enumerate(order.book.fills):
            if fill.broker_deal_id is not None and fill.broker_deal_id in seen:
                continue
            if fill.broker_deal_id is None and index < written:
                # No id to match on, so position in the list is the only
                # handle. Conservative: skip what is already covered.
                continue
            db.add(
                Execution(
                    order_id=row.id,
                    broker_deal_id=fill.broker_deal_id,
                    executed_at=_naive(fill.at) or datetime.utcnow(),  # noqa: DTZ003
                    price=fill.price,
                    quantity=fill.quantity,
                    commission=fill.commission,
                    swap=fill.swap,
                    slippage_points=fill.slippage_points,
                    fill_source=fill.fill_source,
                )
            )

    # =============================================================== reading

    async def order_for_intent(self, db: AsyncSession, intent_id: str) -> Order | None:
        """The order this intent already produced, if any. **L45 C-1.**

        `orders.intent_id` is UNIQUE, so there is at most one, and this is the
        one question whose answer survives a restart. `OrderManager.by_intent`
        asks the same thing of a dict that a new process starts empty --
        which is why the in-memory guard passed and the venue got a second
        order.
        """
        return (await db.scalars(select(Order).where(Order.intent_id == intent_id))).first()

    async def load_unresolved(
        self, db: AsyncSession, *, mode: str, symbols: dict[str, str] | None = None
    ) -> list[ManagedOrder]:
        """Every order in a state whose truth is not known locally.

        This is restart recovery's input (§39). It deliberately loads ONLY the
        unresolved ones: an order that reached a terminal state is settled, and
        re-examining it against the venue would be work with a chance of
        changing something that is already true.
        """
        codes = symbols or {v: k for k, v in self.symbol_ids.items()}
        rows = (
            await db.scalars(
                select(Order).where(
                    Order.mode == mode,
                    Order.status.in_([str(s) for s in NEEDS_RECONCILIATION]),
                )
            )
        ).all()
        out: list[ManagedOrder] = []
        for row in rows:
            symbol = codes.get(row.symbol_id)
            if symbol is None:
                log.warning(
                    "an unresolved order references an unknown symbol; it is reported "
                    "rather than skipped silently",
                    extra={"event": "order_recovery_symbol_missing", "order_id": row.id},
                )
                continue
            out.append(await self._rehydrate(db, row, symbol))
        return out

    async def _rehydrate(self, db: AsyncSession, row: Order, symbol: str) -> ManagedOrder:
        order = ManagedOrder(
            id=row.id,
            client_order_id=row.intent_id,
            account_id=row.paper_account_id or row.broker_account_id or "",
            symbol=symbol,
            side=row.side,
            quantity=row.quantity,
            mode=row.mode,
            created_at=row.created_at,
            order_type=row.order_type,
            time_in_force=row.time_in_force,
            status=OrderStatus(row.status),
            broker_order_id=row.broker_order_id,
            requested_price=row.requested_price,
            stop_loss=row.stop_loss,
            take_profit=row.take_profit,
            signal_id=row.signal_id,
            risk_decision_id=row.risk_decision_id,
            sizing_snapshot=dict(row.sizing or {}),
            expires_at=row.expires_at,
            submitted_at=row.submitted_at,
            acknowledged_at=row.acknowledged_at,
            filled_at=row.filled_at,
            cancelled_at=row.cancelled_at,
            reject_reason=row.reject_reason,
            error_code=row.error_code,
        )
        # Fills are rebuilt from the `executions` rows rather than from
        # `orders.filled_quantity`, so the derived figure is re-derived and a
        # disagreement with the stored one becomes visible.
        fills = (
            await db.scalars(
                select(Execution)
                .where(Execution.order_id == row.id)
                .order_by(Execution.executed_at)
            )
        ).all()
        for fill in fills:
            order.book.add(
                FillRecord(
                    quantity=fill.quantity,
                    price=fill.price,
                    at=fill.executed_at,
                    fill_source=fill.fill_source,
                    broker_deal_id=fill.broker_deal_id,
                    commission=fill.commission or Decimal("0"),
                    swap=fill.swap or Decimal("0"),
                    slippage_points=fill.slippage_points,
                )
            )
        if row.filled_quantity != order.filled_quantity:
            log.error(
                "the stored filled quantity disagrees with the executions; the "
                "executions are authoritative and the difference is reported",
                extra={
                    "event": "order_fill_disagreement",
                    "order_id": row.id,
                    "stored": str(row.filled_quantity),
                    "from_executions": str(order.filled_quantity),
                },
            )
        return order
