"""Partial fills and partial closes, without double-counting either.

Sections 13 and 14, and they are the arithmetic the level lives or dies on.

**Partial fills: the entry is already weighted, and it is L21's.** `Position`
maintains `entry_price` as the volume-weighted average across fills — a position
filled 0.40 at 1.1000 and 0.60 at 1.1050 carries 1.1030 — and `initial_quantity`
records what it opened at. Recomputing that here from `executions` would be a
second derivation of one fact, and §31 says to reference rather than duplicate.
So the journal reads it.

**Partial closes: the exit is weighted here, because nothing else holds it.**
Each close writes a `position_events` row carrying its own fill price, closed
quantity and booked P&L. The trade's single `exit_price` is the volume-weighted
average of those fills:

    exit = Σ(fill_i × qty_i) / Σ(qty_i)

**Realized P&L is summed from the closes, never recomputed from the average.**
§14 says not to double-count, and the subtle way to double-count is to book
`(weighted_exit − weighted_entry) × total_quantity` *as well as* the per-close
figures. They are close but not equal whenever the entry itself was weighted,
and only one of them is what the account actually received. `Position.realized_pnl`
is the running total L21 booked at each close, and it is the authoritative one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")

#: The `position_events` types that mean size came off. Both are needed: L21
#: writes `partially_closed` while size remains and `closed` for the last one,
#: and a reader that took only the second would miss every scale-out.
CLOSING = ("partially_closed", "closed")


@dataclass(frozen=True)
class Close:
    """One confirmed reduction of a position, as the event recorded it."""

    at: datetime
    quantity: Decimal
    fill_price: Decimal
    reason: str
    realized_running: Decimal | None
    broker_deal_id: str | None
    fill_source: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "quantity": str(self.quantity),
            "fill_price": str(self.fill_price),
            "reason": self.reason,
            "realized_running": (
                None if self.realized_running is None else str(self.realized_running)
            ),
            "broker_deal_id": self.broker_deal_id,
            "fill_source": self.fill_source,
        }


def _decimal(value: Any) -> Decimal | None:
    """A JSON payload value as a Decimal, or None. Never a float.

    The payloads are written with `str(...)` precisely so the number survives
    the round trip; parsing back through `float` would undo that, and a price
    that is off in the eighth decimal is a P&L that does not reconcile.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def closes_from(events: list[Any]) -> list[Close]:
    """Every confirmed close on one position, oldest first.

    A closing event with no fill price is SKIPPED, not defaulted. L21 records
    one when the venue confirmed without a price, and it deliberately parks the
    position as `unknown` rather than booking a number — a close valued at a
    price nobody reported is the fabrication §51 forbids, and it would move the
    weighted average.
    """
    found: list[Close] = []
    for event in events:
        if event.event_type not in CLOSING:
            continue
        payload = event.payload or {}
        price = _decimal(payload.get("fill_price"))
        quantity = _decimal(payload.get("closed_quantity"))
        if price is None or quantity is None or quantity <= ZERO:
            continue
        found.append(
            Close(
                at=event.occurred_at,
                quantity=quantity,
                fill_price=price,
                reason=str(payload.get("reason") or ""),
                realized_running=_decimal(payload.get("realized_pnl")),
                broker_deal_id=payload.get("broker_deal_id"),
                fill_source=payload.get("fill_source"),
            )
        )
    return sorted(found, key=lambda close: close.at)


@dataclass(frozen=True)
class Exit:
    """The single exit a trade reports, derived from however many closes."""

    price: Decimal | None
    quantity: Decimal
    at: datetime | None
    closes: int
    reason: str
    complete: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": None if self.price is None else str(self.price),
            "quantity": str(self.quantity),
            "at": self.at.isoformat() if self.at else None,
            "closes": self.closes,
            "reason": self.reason,
            "complete": self.complete,
            "note": self.note
            or (
                "the exit price is the VOLUME-WEIGHTED average of every confirmed "
                "close. Realized P&L is summed from the closes and never recomputed "
                "from this average -- doing both is how a partial close gets counted "
                "twice."
            ),
        }


def exit_of(closes: list[Close]) -> Exit:
    """Weighted exit price, total quantity, last timestamp, and the reason.

    **The reason is the LAST close's**, because that is the one that finished
    the trade. A position scaled out at a target and then stopped out on the
    remainder exited at the stop, and reporting the first reason would describe
    a trade that did not happen.
    """
    if not closes:
        return Exit(
            price=None,
            quantity=ZERO,
            at=None,
            closes=0,
            reason="",
            complete=False,
            note="no confirmed close carried a fill price, so there is no exit to report",
        )
    quantity = sum((close.quantity for close in closes), ZERO)
    weighted = sum((close.fill_price * close.quantity for close in closes), ZERO)
    return Exit(
        price=weighted / quantity,
        quantity=quantity,
        at=closes[-1].at,
        closes=len(closes),
        reason=closes[-1].reason,
        complete=True,
    )


def realized_from(closes: list[Close], booked: Decimal | None) -> tuple[Decimal | None, str]:
    """The realized figure to record, and where it came from.

    `Position.realized_pnl` is authoritative: L21 booked it at each close on the
    size that actually closed, at the price the venue actually filled. It is
    preferred over anything derived here, and §18 says to prefer the
    authoritative record.

    The per-close running totals are the fallback, and they are the SAME
    numbers — the last one is the running total. What is never done is adding
    the two, or recomputing `(exit - entry) x quantity` on top: those agree only
    when the entry was never weighted, and disagreeing quietly is worse than
    either.
    """
    if booked is not None:
        return booked, "positions.realized_pnl, booked by L21 at each confirmed close"
    running = [close.realized_running for close in closes if close.realized_running is not None]
    if running:
        return running[-1], "the last close event's running realized total"
    return None, (
        "no realized figure was recorded. NOT zero: zero would say the trade broke "
        "even, and what happened is that nobody booked it."
    )


__all__ = ["CLOSING", "Close", "Exit", "closes_from", "exit_of", "realized_from"]
