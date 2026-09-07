"""Position lifecycle events on the existing bus.

The L07 catalogue declared three `POSITION_*` types and named L21 as the level
that would produce them. Until now one had a producer (`POSITION_CLOSED`, from
the paper service), so two were documented, scoped, authorized and dead. This
maps what the manager actually does onto them.

**No second event system.** These are `app.realtime.catalogue.EventType`
values on the `account` scope the catalogue already assigns, published through
the same `Hub` every other producer uses. Nothing here creates a channel, a
bus or a transport.

**A published event is a report, never a command.** No consumer of these may
close, modify or reconcile anything — a bus that can move a stop is a second
way to move a stop, and the whole point of L21's priority rules is that there
is one.

**Queued, not published inline.** The manager collects events and a caller
drains them AFTER the state is durable. That is the only order in which an
event cannot describe something that was not saved, and it keeps a slow or
broken bus out of the middle of a venue interaction.

**The mismatch is an event too.** A stop this platform believes in that the
venue is not holding is published as `POSITION_UPDATED` carrying
`protection_gap`, because an operator learning about it late is the failure
the whole mechanism exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.models.execution import Position
from app.realtime.catalogue import EventType

log = logging.getLogger("app.positions.events")

#: What happened to a position, and which catalogue type reports it. Total by
#: construction over the manager's own vocabulary, so a new kind added without
#: an event fails the build rather than falling silent.
EVENT_FOR: dict[str, EventType] = {
    "opened": EventType.POSITION_OPENED,
    "stop_modified": EventType.POSITION_UPDATED,
    "protection_set": EventType.POSITION_UPDATED,
    "protection_mismatch": EventType.POSITION_UPDATED,
    "partially_closed": EventType.POSITION_UPDATED,
    "reconciled": EventType.POSITION_UPDATED,
    "reconcile_inconclusive": EventType.POSITION_UPDATED,
    "close_rejected": EventType.POSITION_UPDATED,
    # An outcome nobody is looking at yet. Reported like any other, because
    # suppressing it because it looks like an error is how it stops being
    # noticed.
    "close_unknown": EventType.POSITION_UPDATED,
    "closed": EventType.POSITION_CLOSED,
    "reconcile_closed": EventType.POSITION_CLOSED,
}


def event_for(kind: str) -> EventType | None:
    """The catalogue type for a recorded change, or None if it is not published.

    `None` rather than a default: an entry nobody has decided about should be
    invisible on the bus rather than arriving as the wrong type.
    """
    return EVENT_FOR.get(kind)


@dataclass(frozen=True)
class PositionEventOut:
    """One lifecycle event, ready to publish."""

    type: str
    payload: dict[str, object]
    position_id: str


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def payload_for(row: Position, kind: str, detail: dict[str, object]) -> dict[str, object]:
    """What a subscriber needs, and nothing it must not have.

    Both the intended protective levels and the venue's are included, because
    a subscriber shown only one could not tell a protected position from one
    that merely believes it is. Nothing here can reach a credential: a
    `Position` row holds none.
    """
    return {
        "position_id": row.id,
        "broker_position_id": row.broker_position_id,
        "account_id": row.broker_account_id or row.paper_account_id,
        "symbol_id": row.symbol_id,
        "strategy_version_id": row.strategy_version_id,
        "mode": row.mode,
        "side": row.side,
        "status": row.status,
        # Open now, beside what it opened at and what has gone.
        "quantity": str(row.quantity),
        "initial_quantity": _money(row.initial_quantity),
        "closed_quantity": _money(row.closed_quantity),
        "entry_price": str(row.entry_price),
        # What this platform INTENDS.
        "stop_loss": _money(row.stop_loss),
        "take_profit": _money(row.take_profit),
        # What the VENUE last said, and when. None for `broker_synced_at`
        # means never read, which is not the same as agreed.
        "broker_stop_loss": _money(row.broker_stop_loss),
        "broker_take_profit": _money(row.broker_take_profit),
        "broker_synced_at": (row.broker_synced_at.isoformat() if row.broker_synced_at else None),
        "realized_pnl": _money(row.realized_pnl),
        "change": kind,
        "detail": detail,
        "advisory": "a report of what happened; nothing may execute from it",
    }
