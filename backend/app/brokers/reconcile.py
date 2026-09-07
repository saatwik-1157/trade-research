"""Reconciliation: compare what we believe against what the venue holds.

**This module reports. It never repairs.** Every function here returns a
finding; not one of them writes, closes, opens or cancels anything. That is the
whole design, and the reason is the failure it guards against: the natural
instinct on discovering a position the platform does not know about is to close
it, and the natural instinct on discovering an internal position the venue does
not have is to re-send it. Both instincts are wrong. The first closes a trade
somebody may have opened by hand; the second is exactly how a crash between
send and log becomes two positions.

So the output is a `Reconciliation` an operator or a later level reads, and the
resolution of each finding is a deliberate act elsewhere.

**The venue is authoritative for broker-side state.** Where the two disagree
about whether a position exists, the venue is right and our record is stale or
wrong. Where they disagree about *why* a position exists — which strategy,
which intent — only our record knows, and the venue cannot be asked. That
asymmetry is why a mismatch is never resolved by overwriting one side with the
other wholesale.

An `unknown` order is the case that must be settled before another order is
sent for the same intent. `unresolved_unknowns` names them, and nothing here
retries one: an IPC timeout after `order_send` looks exactly like a rejection
from the caller's side, and this project has a live example of a figure the
tool chose being logged as a figure the server confirmed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only; importing at runtime would be circular
    from app.brokers.base import BrokerOrder, BrokerPosition

log = logging.getLogger("app.brokers.reconcile")


class Finding(StrEnum):
    """What kind of disagreement was found."""

    # The venue holds a position we have no record of. Opened by hand, opened
    # by a run whose log was lost, or opened by another system on the account.
    unexpected_at_broker = "unexpected_at_broker"
    # We believe a position is open and the venue does not hold it. It closed
    # while we were not watching, or it never opened.
    missing_at_broker = "missing_at_broker"
    # Both know it. They disagree about size.
    volume_mismatch = "volume_mismatch"
    # Both know it. They disagree about the entry.
    price_mismatch = "price_mismatch"
    # Both know it. They disagree about the bracket.
    bracket_mismatch = "bracket_mismatch"
    # An order we sent and never resolved. Must be settled before another
    # order is sent for the same intent.
    unresolved_unknown = "unresolved_unknown"
    # The venue holds an order we have no record of.
    unexpected_order_at_broker = "unexpected_order_at_broker"


@dataclass(frozen=True)
class Mismatch:
    finding: Finding
    position_id: str | None
    symbol: str | None
    detail: str
    ours: str | None = None
    theirs: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "finding": str(self.finding),
            "position_id": self.position_id,
            "symbol": self.symbol,
            "detail": self.detail,
            "ours": self.ours,
            "theirs": self.theirs,
        }


@dataclass(frozen=True)
class InternalPosition:
    """What the platform believes it holds. Deliberately a plain shape rather
    than the ORM row, so this module can be called from a worker with no
    session and tested without a database."""

    position_id: str
    symbol: str
    side: str
    volume: Decimal
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    status: str = "open"


@dataclass(frozen=True)
class Reconciliation:
    checked_at: datetime
    broker_positions: int
    internal_positions: int
    mismatches: list[Mismatch] = field(default_factory=list)
    unresolved_unknowns: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.mismatches and not self.unresolved_unknowns

    @property
    def safe_to_trade(self) -> bool:
        """Whether a new order may be sent on this account right now.

        An unresolved `unknown` blocks. A position we did not expect does not:
        it is a fact to investigate, not a reason to stop the platform from
        managing what it does know about. That distinction matters because a
        rule that halted on any mismatch would be switched off the first time
        somebody opened a manual trade.
        """
        return not self.unresolved_unknowns

    def as_dict(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "broker_positions": self.broker_positions,
            "internal_positions": self.internal_positions,
            "clean": self.clean,
            "safe_to_trade": self.safe_to_trade,
            "mismatches": [m.as_dict() for m in self.mismatches],
            "unresolved_unknowns": list(self.unresolved_unknowns),
            "note": (
                "A report, not a repair. Nothing was opened, closed, cancelled or "
                "rewritten. An unresolved unknown order must be settled before "
                "another order is sent for the same intent, and it is never settled "
                "by retrying."
            ),
        }


# How far two prices may differ before it is called a mismatch. Entry prices
# are compared at the symbol's own precision elsewhere; here a small absolute
# tolerance keeps a display rounding from being reported as a disagreement.
PRICE_TOLERANCE = Decimal("0.00001")


def _differs(a: Decimal | None, b: Decimal | None, tolerance: Decimal) -> bool:
    if a is None or b is None:
        return a is not b and (a is None) != (b is None)
    return abs(a - b) > tolerance


def reconcile_positions(
    broker: list[BrokerPosition],
    internal: list[InternalPosition],
    *,
    now: datetime,
    unresolved_unknowns: list[str] | None = None,
    broker_orders: list[BrokerOrder] | None = None,
    known_order_ids: set[str] | None = None,
    price_tolerance: Decimal = PRICE_TOLERANCE,
) -> Reconciliation:
    """Compare the two views and describe every disagreement.

    Only positions we believe are OPEN are compared. A closed internal record
    that the venue no longer holds is agreement, not a mismatch.
    """
    open_internal = [p for p in internal if p.status == "open"]
    by_broker_id = {p.position_id: p for p in broker}
    by_internal_id = {p.position_id: p for p in open_internal}

    mismatches: list[Mismatch] = []

    for position_id, theirs in by_broker_id.items():
        ours = by_internal_id.get(position_id)
        if ours is None:
            mismatches.append(
                Mismatch(
                    Finding.unexpected_at_broker,
                    position_id,
                    theirs.symbol,
                    "the venue holds a position the platform has no open record of; "
                    "it may have been opened by hand or by a run whose log was lost. "
                    "Investigate before acting: closing it would close somebody's trade",
                    ours=None,
                    theirs=f"{theirs.side} {theirs.volume} @ {theirs.entry_price}",
                )
            )
            continue
        if ours.volume != theirs.volume:
            mismatches.append(
                Mismatch(
                    Finding.volume_mismatch,
                    position_id,
                    theirs.symbol,
                    "size disagrees; the venue is authoritative for what is held",
                    ours=str(ours.volume),
                    theirs=str(theirs.volume),
                )
            )
        if _differs(ours.entry_price, theirs.entry_price, price_tolerance):
            mismatches.append(
                Mismatch(
                    Finding.price_mismatch,
                    position_id,
                    theirs.symbol,
                    "entry price disagrees; read the fill, never the quote",
                    ours=str(ours.entry_price),
                    theirs=str(theirs.entry_price),
                )
            )
        if _differs(ours.stop_loss, theirs.stop_loss, price_tolerance) or _differs(
            ours.take_profit, theirs.take_profit, price_tolerance
        ):
            mismatches.append(
                Mismatch(
                    Finding.bracket_mismatch,
                    position_id,
                    theirs.symbol,
                    "the bracket at the venue is not the one recorded; server-side "
                    "stop and target are what actually protect the position",
                    ours=f"sl={ours.stop_loss} tp={ours.take_profit}",
                    theirs=f"sl={theirs.stop_loss} tp={theirs.take_profit}",
                )
            )

    for position_id, ours in by_internal_id.items():
        if position_id not in by_broker_id:
            mismatches.append(
                Mismatch(
                    Finding.missing_at_broker,
                    position_id,
                    ours.symbol,
                    "the platform believes this is open and the venue does not hold "
                    "it; it closed while we were not watching, or never opened. "
                    "Resolve by reading the deal history, never by re-sending",
                    ours=f"{ours.side} {ours.volume}",
                    theirs=None,
                )
            )

    if broker_orders and known_order_ids is not None:
        for order in broker_orders:
            if order.order_id not in known_order_ids:
                mismatches.append(
                    Mismatch(
                        Finding.unexpected_order_at_broker,
                        None,
                        order.symbol,
                        "the venue holds a pending order the platform has no record of",
                        theirs=f"{order.side} {order.volume} @ {order.price}",
                    )
                )

    unknowns = list(unresolved_unknowns or [])
    for intent_id in unknowns:
        mismatches.append(
            Mismatch(
                Finding.unresolved_unknown,
                None,
                None,
                f"intent {intent_id} was sent and never resolved. Settle it against "
                "the venue's deal history before sending another order for it. "
                "Retrying is how one intent becomes two positions",
            )
        )

    report = Reconciliation(
        checked_at=now,
        broker_positions=len(broker),
        internal_positions=len(open_internal),
        mismatches=mismatches,
        unresolved_unknowns=unknowns,
    )
    if not report.clean:
        log.warning(
            "reconciliation found disagreements",
            extra={
                "event": "broker_reconcile_mismatch",
                "mismatches": len(mismatches),
                "unresolved_unknowns": len(unknowns),
                "safe_to_trade": report.safe_to_trade,
            },
        )
    return report
