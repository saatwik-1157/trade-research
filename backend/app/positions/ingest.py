"""The `positions` row a broker fill implies.

Until now only `app/paper/service.py` constructed a `Position`. A demo or live
fill produced an order row and a position at the venue that the platform had no
record of, which meant three things, all observed on 2026-09-07 the first time
the platform sent a real order:

  * `PositionManager` could not manage it -- no stop move, no trailing, no
    break-even, no time exit, no emergency exit.
  * `POST /v1/positions/{id}/close` could not close it, because that route
    starts from a `positions` row.
  * Reconciliation reported every venue position as `unexpected_at_broker`
    against `internal_positions: 0`.

**This module is the missing write and nothing else.** It creates or updates one
row from an order that the venue has confirmed. It holds no adapter, sends
nothing, decides nothing about whether a trade should exist, and is called only
after the OMS already has a fill in hand. A test parses the package to prove it
imports no broker and no order manager.

Three rules carry it.

**1. Only a CONFIRMED fill.** A position is written from `filled_quantity` and
`average_fill_price`, which the OMS sets from what the venue reported. An order
that is `submitting`, `unknown` or rejected produces nothing here: writing a
position for an order whose outcome nobody knows is how the platform ends up
believing in a position that does not exist, which is the failure reconciliation
exists to catch.

**2. Paper is not touched.** `app/paper/service.py` already writes its own rows
and doubling them would double every exposure figure the platform computes. This
runs for `demo` and `live` only.

**3. Idempotent on the venue's identifier.** A repeated call for the same
`broker_position_id` updates that row rather than inserting a second. The OMS
persists an order more than once by design -- before the send and again after --
and a fill that arrives twice must not become two positions.

**Every id it writes is a foreign key, and it writes none it was not given.**
`broker_account_id` points at `broker_accounts`, `strategy_version_id` at
`strategy_versions`, `symbol_id` at `symbols`. A caller that has no real row for
one passes `None`; nothing here derives an id from a value that merely looks
like one. That rule exists because the sibling mistake -- `orders.signal_id` set
to an idempotency key -- survived 174 tests, since SQLite does not enforce
foreign keys and PostgreSQL does.

**What this does NOT do**, deliberately, because each is a decision rather than
a translation: it does not net two fills on one symbol into a single position,
it does not close a position, and it does not reconcile. Netting in particular
is venue-dependent -- MetaTrader hedging accounts hold two opposing positions
where a netting account holds one -- so the platform records what the venue gave
it a ticket for and leaves the interpretation to reconciliation.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.models.execution import Position

#: Modes whose fills come from a venue. `paper` is absent on purpose -- see
#: rule 2. A mode that is not here produces no row at all rather than a row
#: with a mode nobody checks.
BROKER_MODES = frozenset({"demo", "live"})

#: Order states that carry a fill worth recording. `unknown` is not among them:
#: it means the venue's answer was not understood, and a position written from
#: one is a belief with nothing behind it.
FILLED_STATES = frozenset({"filled", "partially_filled"})

_SIDE = {"buy": "long", "sell": "short"}


def naive_utc(value):  # noqa: ANN001, ANN202 - datetime | None both ways
    """Strip the timezone before a datetime reaches a column.

    Every datetime column in this schema is `timezone=False`, and the repository
    states that convention in one place: `app.auth.models.utcnow` is
    `datetime.now(UTC).replace(tzinfo=None)`. Code that builds an aware time for
    its comparisons -- which is right -- has to convert before it writes.

    PostgreSQL raises on an aware value:

        asyncpg.exceptions.DataError: invalid input for query argument
        (can't subtract offset-naive and offset-aware datetimes)

    SQLite accepts it silently. So this is another defect the whole test suite
    could not see, and it was found three times in one afternoon on 2026-09-07 --
    in the reconciler and twice in the position manager -- once anything
    actually reconciled or closed against a real venue.
    """
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


#: Kept for readers of the original name inside this module.
_naive = naive_utc


async def record_fill(
    db: AsyncSession,
    order,  # noqa: ANN001 - ManagedOrder, kept loose so this imports no OMS
    *,
    symbol_id: str,
    broker_account_id: str | None = None,
    strategy_version_id: str | None = None,
    source: str = "pipeline",
) -> Position | None:
    """Write the position an order's confirmed fill implies, or return None.

    Returns the row without committing: the caller owns the transaction, which
    is what lets the order row and the position it implies land together rather
    than as two writes that can disagree.
    """
    mode = str(getattr(order, "mode", "") or "")
    if mode not in BROKER_MODES:
        return None

    status = str(getattr(order, "status", "") or "")
    if status not in FILLED_STATES:
        return None

    quantity: Decimal = getattr(order, "filled_quantity", Decimal("0")) or Decimal("0")
    entry = getattr(order, "average_fill_price", None)
    if quantity <= 0 or entry is None:
        # Accepted with no fill price is not a confirmation, and the adapter
        # says so by parking the order `unknown`. Nothing to record.
        return None

    # For a market fill MetaTrader gives the position the ticket it gave the
    # order, so `broker_order_id` IS the venue's position identifier here. That
    # is true of this venue rather than of venues in general, which is why the
    # column it lands in is named for what it holds.
    venue_id = getattr(order, "broker_order_id", None)
    if not venue_id:
        # No identifier means nothing can ever be matched against the venue, and
        # an unmatched position is worse than an absent one: reconciliation
        # would report it as missing at the broker forever.
        return None
    venue_id = str(venue_id)

    side = _SIDE.get(str(getattr(order, "side", "")).lower())
    if side is None:
        return None

    existing = await db.scalar(
        select(Position).where(
            Position.broker_position_id == venue_id,
            Position.mode == mode,
        )
    )
    opened = _naive(getattr(order, "filled_at", None)) or utcnow()

    if existing is not None:
        # A re-persist, or a second fill on the same ticket. Update what the
        # venue has changed and leave `initial_quantity` alone -- it records
        # what opened, and `quantity` records what is open now. Overwriting it
        # would erase the distinction the close accounting depends on.
        existing.quantity = quantity
        existing.entry_price = entry
        if quantity > existing.initial_quantity:
            existing.initial_quantity = quantity
        existing.stop_loss = getattr(order, "stop_loss", None)
        existing.take_profit = getattr(order, "take_profit", None)
        return existing

    row = Position(
        mode=mode,
        broker_account_id=broker_account_id,
        symbol_id=symbol_id,
        broker_position_id=venue_id,
        side=side,
        quantity=quantity,
        initial_quantity=quantity,
        closed_quantity=Decimal("0"),
        entry_price=entry,
        # What the platform ASKED for. The venue's own levels are read back
        # into `broker_stop_loss` / `broker_take_profit` by reconciliation --
        # keeping the two apart is what makes "the venue moved our stop"
        # something the platform can notice rather than overwrite.
        stop_loss=getattr(order, "stop_loss", None),
        take_profit=getattr(order, "take_profit", None),
        status="open",
        opened_at=opened,
        source=source,
        # Passed in, never read off the order. `order.strategy_id` is whatever
        # the caller put there -- for the manual route, a free-form string --
        # while this column is a FOREIGN KEY to `strategy_versions`. Writing an
        # id that is not one fails on PostgreSQL and passes on SQLite, which is
        # precisely how `orders.signal_id` went unnoticed through 174 tests.
        strategy_version_id=strategy_version_id,
    )
    db.add(row)
    return row


async def open_internal_positions(db: AsyncSession, *, mode: str) -> list[Position]:
    """Every open position the platform believes it holds in one mode.

    Used by reconciliation, which until now was passed a hard-coded empty list
    because there was nothing to pass.
    """
    rows = await db.scalars(
        select(Position).where(
            Position.mode == mode,
            Position.status.in_(("open", "partially_closed", "opening")),
        )
    )
    return list(rows)
