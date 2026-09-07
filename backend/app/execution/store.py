"""Durable order state for the execution pipeline. **The fix for L45 C-1.**

The pipeline used to create orders in memory and never write one. That single
omission disabled four separate protections at once, which is why it survived
six audits:

* `orders.intent_id` is UNIQUE, and a constraint on a row nobody inserts
  cannot prevent anything.
* `OrderRepository.load_unresolved` reads the `orders` table, so restart
  recovery had nothing to load.
* `recovery/reconciliation.unresolved_orders` reads the same table, so it
  reported `unresolved=0` and **safe mode never latched**.
* Every remaining guard -- `pipeline.seen`, `OrderManager.by_intent`,
  `guard_resend` -- is process-local, and a restart empties all three.

The measured consequence: a signal whose venue state was never established was
refused on a second pass in the same process and **accepted** after a restart.
Whether that becomes two positions at the venue depends on whether the first
send landed, which is unknowable by definition -- which is exactly why the rule
"never retry an unknown order" exists.

**This module is not a second order model.** It is the same
`OrderRepository` the API order route has always used, given to the pipeline so
the automated path is as durable as the manual one. The asymmetry between those
two paths WAS the defect.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.accounts import BrokerAccount
from app.models.market import Symbol
from app.oms.order import ManagedOrder
from app.oms.repository import OrderRepository
from app.oms.state import NEEDS_RECONCILIATION, OrderStatus, can_resend
from app.positions.ingest import record_fill

log = logging.getLogger("app.execution.store")


class OrderNotRecorded(Exception):
    """The order could not be written down, so it must not be transmitted.

    Raised only from `record()` and only before a send. §18's ordering exists
    for this case: an order at a venue that the platform has no record of is
    the failure every other guard here is built to avoid.
    """


@dataclass(frozen=True)
class PriorOrder:
    """What the database already knows about an intent. Read-only by design.

    The pipeline decides with this; it never mutates through it. `status` is
    the stored string rather than an `OrderStatus`, so a row written by a
    newer schema cannot crash the guard that is meant to refuse.
    """

    id: str
    intent_id: str
    status: str
    #: The venue may be holding it. Nothing may be sent for this intent.
    needs_reconciliation: bool
    #: A fresh order for this intent would be safe -- the previous one is
    #: terminal AND terminal in a way that means nothing reached the venue.
    resendable: bool


@runtime_checkable
class OrderStore(Protocol):
    """The durable half of the execution pipeline.

    Two questions, both of which must outlive the process:

    * `prior_order` -- has this intent already produced an order?
    * `record` -- write this order's current state down.
    """

    async def prior_order(self, intent_id: str) -> PriorOrder | None: ...

    async def record(self, order: ManagedOrder) -> None: ...


def _prior_from(row: object) -> PriorOrder:
    status = str(getattr(row, "status", ""))
    try:
        parsed: OrderStatus | None = OrderStatus(status)
    except ValueError:
        # A status this build does not know is treated as UNRESOLVED, never as
        # resendable. Failing closed on an unrecognised row is the only safe
        # reading: the alternative is sending a second order because a newer
        # deployment wrote a state this one cannot parse.
        parsed = None
    return PriorOrder(
        id=str(getattr(row, "id", "")),
        intent_id=str(getattr(row, "intent_id", "")),
        status=status,
        needs_reconciliation=parsed is None or parsed in NEEDS_RECONCILIATION,
        resendable=parsed is not None and can_resend(parsed),
    )


class DatabaseOrderStore:
    """`OrderStore` over the real tables, with its own short-lived sessions.

    A session per call rather than one held open: the pipeline runs inside a
    broker interaction, and a transaction held across a venue round-trip is a
    lock held across a network call somebody else's timeout controls.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        # Injected for tests that drive a store without a `symbols` table.
        symbol_ids: dict[str, str] | None = None,
    ) -> None:
        self.sessions = sessions
        self._symbol_ids: dict[str, str] = dict(symbol_ids or {})

    async def _resolve_symbol(self, db: AsyncSession, code: str) -> dict[str, str]:
        """code -> symbols.id, cached. An order write must not become a query
        on every pass, and the mapping does not change under a running process.
        """
        if code not in self._symbol_ids:
            found = (await db.scalars(select(Symbol).where(Symbol.code == code))).first()
            if found is not None:
                self._symbol_ids[code] = found.id
        return {code: self._symbol_ids[code]} if code in self._symbol_ids else {}

    async def prior_order(self, intent_id: str) -> PriorOrder | None:
        async with self.sessions() as db:
            row = await OrderRepository().order_for_intent(db, intent_id)
        return None if row is None else _prior_from(row)

    async def record(self, order: ManagedOrder) -> None:
        async with self.sessions() as db:
            symbols = await self._resolve_symbol(db, order.symbol)
            repo = OrderRepository(symbols)
            row = await repo.persist(db, order)
            if row is None:
                # `persist` returns None when the symbol is unresolvable. It
                # reports rather than raises, because for an order that already
                # exists at a venue a missing row beats an exception. Here
                # NOTHING has been sent yet, so the correct answer is the
                # opposite: refuse, and never reach the venue at all.
                raise OrderNotRecorded(
                    f"order {order.id} for intent {order.client_order_id} could not be "
                    f"recorded: symbol {order.symbol!r} does not resolve. Nothing was "
                    "sent."
                )
            # The position a confirmed broker fill implies, in the SAME session
            # as the order it came from. The manual route at `/v1/orders` has
            # done this since L70d; the pipeline had not, so a signal that
            # filled at a venue produced an order row and no position -- the very
            # gap that left `PositionManager` unable to manage what the platform
            # opened, still standing on the path that actually trades.
            #
            # Returns None for paper, for anything not confirmed filled, and for
            # a fill carrying no venue identifier. Nothing is sent here; the fill
            # already happened.
            await record_fill(
                db,
                order,
                symbol_id=symbols.get(order.symbol, ""),
                broker_account_id=await self._broker_account_id(db, order),
                source="pipeline",
            )
            await db.commit()

    async def _broker_account_id(self, db: AsyncSession, order: ManagedOrder) -> str | None:
        """The `broker_accounts.id` this order's account refers to, or None.

        On the pipeline path `ManagedOrder.account_id` already IS that durable
        id -- `route_for` reads it off the bot's `broker_account_id` -- so this
        confirms the row exists rather than trusting the string. A foreign key
        pointed at a row that is not there is the mistake `orders.signal_id`
        made, and confirming costs one indexed read.
        """
        account_id = getattr(order, "account_id", None)
        if not account_id:
            return None
        found = await db.get(BrokerAccount, str(account_id))
        return found.id if found is not None else None


def store_for(
    sessions: async_sessionmaker[AsyncSession],
) -> DatabaseOrderStore:
    """The store the deployed pipeline uses. One function so there is one
    answer to "what makes the automated path durable", and one place to find
    it."""
    return DatabaseOrderStore(sessions)


#: Typing convenience for callers that hand the pipeline a plain function.
RecordHook = Callable[[ManagedOrder], Awaitable[None]]

__all__ = [
    "DatabaseOrderStore",
    "OrderNotRecorded",
    "OrderStore",
    "PriorOrder",
    "RecordHook",
    "store_for",
]
