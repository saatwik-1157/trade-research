"""Settling positions against the venue, and recording what it said.

`app/brokers/reconcile.py` already compares the two views and describes every
disagreement. It reports and never repairs, and nothing here changes that
design — this module is the part that *reads* the two views, writes the
findings to the audit log, and applies the two corrections that are
unambiguously safe. Everything else is left for a person.

**What it corrects, and why only these two.**

  * A position the venue does NOT hold, which we believe is open, becomes
    `closed`. The venue is authoritative for whether a position exists; if it
    is not there, it is not there. What we cannot know is the price it closed
    at, so `realized_pnl` is left alone and the event says the exit price is
    unknown — inventing one would be the fabricated-figure failure this
    repository exists to prevent.
  * A quantity or protective level the venue reports differently is written
    into the `broker_*` columns, which are a RECORD of what the venue said,
    not a claim about what we intended. `stop_loss` and `take_profit` are left
    exactly as they were, so the disagreement stays visible instead of being
    resolved by overwriting one side.

**What it never does.** It does not open, close, cancel or modify anything at
a venue. It does not delete a local position because the venue is missing one.
It does not adopt an unexpected broker position as ours — a position opened by
hand is a fact to investigate, and silently claiming it would attribute
somebody else's trade to a strategy.

**A position being reconciled is not manageable.** `reconciling` is a state
the manager refuses to act on, distinct from `unknown`: `unknown` means nobody
is looking, `reconciling` means somebody is, and only the second means an
answer is coming. The sweep sets it, and clears it on the way out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import BrokerAdapter
from app.brokers.reconcile import (
    Finding,
    InternalPosition,
    Reconciliation,
    reconcile_positions,
)
from app.models.execution import Position, PositionEvent
from app.positions.events import PositionEventOut, event_for, payload_for
from app.positions.ingest import naive_utc

log = logging.getLogger("app.positions.reconciler")

#: Local states worth comparing against the venue. A closed position the venue
#: no longer holds is agreement, not a mismatch, so it is not read.
LIVE = ("open", "partially_closed", "unknown", "closing", "opening")


@dataclass
class ReconcileReport:
    """What one sweep found and what it changed."""

    at: datetime
    account_id: str
    reconciliation: Reconciliation | None = None
    closed_locally: list[str] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)
    unexpected_at_broker: list[str] = field(default_factory=list)
    unattributed: list[str] = field(default_factory=list)
    failed: str | None = None

    @property
    def clean(self) -> bool:
        """Nothing here needs a person.

        `unattributed` counts, and it is the one condition here that is not
        about the venue. A sweep that reports agreement while a position sits
        outside every account it could have swept is describing a subset and
        calling it the whole -- which is the exact shape of defect this
        module's own comparison exists to catch.
        """
        return (
            self.failed is None
            and self.reconciliation is not None
            and self.reconciliation.clean
            and not self.unattributed
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "account_id": self.account_id,
            "clean": self.clean,
            "failed": self.failed,
            "closed_locally": list(self.closed_locally),
            "synced": list(self.synced),
            "unexpected_at_broker": list(self.unexpected_at_broker),
            "unattributed": list(self.unattributed),
            "reconciliation": (
                self.reconciliation.as_dict() if self.reconciliation is not None else None
            ),
            "note": (
                "The venue is authoritative for whether a position exists. Where the "
                "two disagree about a level or a size, both are kept and the "
                "disagreement is recorded; nothing is resolved by overwriting one side."
                + (
                    f" {len(self.unattributed)} position(s) in this mode name no "
                    "account and were reported without being read against any venue: "
                    "no sweep can reach them, and which venue they were held at is "
                    "not something this one can know."
                    if self.unattributed
                    else ""
                )
            ),
        }


class PositionReconciler:
    """Reads the venue, compares, records. Repairs only the unambiguous."""

    def __init__(self, db: AsyncSession, adapter: BrokerAdapter, *, mode: str) -> None:
        self.db = db
        self.adapter = adapter
        self.mode = mode
        self.pending_events: list[PositionEventOut] = []
        # Rows this sweep is holding, so an event payload can be built from the
        # row the change was applied to without a second query.
        self._rows: dict[str, Position] = {}

    async def sweep(self, account_id: str, *, now: datetime | None = None) -> ReconcileReport:
        at = now or datetime.now(UTC)
        report = ReconcileReport(at=at, account_id=account_id)

        rows = await self._local(account_id)
        self._rows = {row.id: row for row in rows}
        try:
            broker_positions = await self.adapter.get_positions()
        except Exception as exc:  # noqa: BLE001
            # The venue could not be read, so NOTHING is concluded. A
            # reconciler that decided a position was gone because the network
            # was down is worse than one that refuses to decide.
            report.failed = f"the venue could not be read: {type(exc).__name__}: {exc}"
            log.error(
                "position reconciliation could not read the venue; nothing was changed",
                extra={
                    "event": "position_reconciliation_failed",
                    "account_id": account_id,
                    "mode": self.mode,
                    "reason": str(exc)[:300],
                },
            )
            return report

        # Mark what we are looking at, so a management pass running beside this
        # one leaves it alone. `reconciling` is refused by the manager.
        previous = {row.id: row.status for row in rows}
        for row in rows:
            if row.status in ("open", "partially_closed", "unknown"):
                row.status = "reconciling"
        await self.db.flush()

        report.reconciliation = reconcile_positions(
            broker_positions,
            [
                InternalPosition(
                    position_id=row.broker_position_id or row.id,
                    symbol=row.symbol_id,
                    side=row.side,
                    volume=row.quantity,
                    entry_price=row.entry_price,
                    stop_loss=row.stop_loss,
                    take_profit=row.take_profit,
                    status="open",
                )
                for row in rows
            ],
            now=at,
        )

        by_broker_id = {p.position_id: p for p in broker_positions}
        for row in rows:
            await self._settle(row, by_broker_id, previous[row.id], at, report)

        # Reported, never touched. See `_unattributed`.
        report.unattributed = [row.id for row in await self._unattributed()]
        if report.unattributed:
            log.warning(
                "positions in this mode belong to no account and cannot be swept",
                extra={
                    "event": "position_unattributed",
                    "account_id": account_id,
                    "mode": self.mode,
                    "positions": report.unattributed[:20],
                    "count": len(report.unattributed),
                },
            )

        await self.db.flush()
        for mismatch in report.reconciliation.mismatches:
            if mismatch.finding is Finding.unexpected_at_broker and mismatch.position_id:
                report.unexpected_at_broker.append(mismatch.position_id)
        return report

    # ------------------------------------------------------------- internals

    async def _local(self, account_id: str) -> list[Position]:
        return list(
            (
                await self.db.scalars(
                    select(Position)
                    .where(
                        Position.mode == self.mode,
                        Position.status.in_(LIVE),
                        (Position.broker_account_id == account_id)
                        | (Position.paper_account_id == account_id),
                    )
                    .order_by(Position.opened_at)
                )
            ).all()
        )

    async def _unattributed(self) -> list[Position]:
        """Live positions in this mode that name NO account at all.

        `_local` scopes by account, which is right -- one venue's adapter can
        only speak for its own account. But a row with both account columns
        NULL matches no account_id, so every sweep silently steps over it and
        the position is not reconciled by anybody. Position 58326177606 sat
        that way: open in the platform, closed at the venue, and invisible to
        the sweep for as long as anybody cared to run it.

        It is REPORTED and never touched. This sweep holds one account's
        adapter and cannot know the orphan was even held there, so closing it
        or attaching it to this account would be a guess about which venue a
        position lived at -- exactly the attribution the reconciler refuses to
        make when it declines to adopt an unexpected broker position.

        Every sweep of a mode reports the same orphans, deliberately. A finding
        that appears once and then hides until somebody sweeps the right
        account is one nobody would see.
        """
        return list(
            (
                await self.db.scalars(
                    select(Position)
                    .where(
                        Position.mode == self.mode,
                        Position.status.in_(LIVE),
                        Position.broker_account_id.is_(None),
                        Position.paper_account_id.is_(None),
                    )
                    .order_by(Position.opened_at)
                )
            ).all()
        )

    async def _settle(
        self,
        row: Position,
        by_broker_id: dict,
        previous_status: str,
        at: datetime,
        report: ReconcileReport,
    ) -> None:
        held = by_broker_id.get(row.broker_position_id) if row.broker_position_id else None

        if held is None:
            if row.broker_position_id is None:
                # We never had an id for it, so the venue cannot be asked about
                # this one specifically. It goes back to what it was; a
                # reconciler that closed it would be guessing.
                row.status = previous_status
                await self._event(
                    row.id,
                    "reconcile_inconclusive",
                    at,
                    {
                        "detail": "no broker position id; the venue cannot be asked "
                        "about this position by id",
                        "restored_status": previous_status,
                    },
                )
                return
            # The venue is authoritative for existence. It is not there.
            row.status = "closed"
            row.closed_at = row.closed_at or naive_utc(at)
            row.quantity = Decimal("0")
            report.closed_locally.append(row.id)
            await self._event(
                row.id,
                "reconcile_closed",
                at,
                {
                    "detail": (
                        f"the venue does not hold {row.broker_position_id}; the position is closed"
                    ),
                    # Deliberately absent. We do not know what it closed at, and
                    # a price we made up would look exactly like a measured one.
                    "exit_price": None,
                    "realized_pnl_note": (
                        "not booked: the venue did not report an exit price for this "
                        "close, and inventing one would misstate the record"
                    ),
                },
            )
            return

        # It is there. Record what the venue says, beside what we intended.
        changes: dict[str, object] = {}
        if held.volume != row.quantity:
            changes["quantity"] = {"ours": str(row.quantity), "venue": str(held.volume)}
        if held.stop_loss != row.stop_loss:
            changes["stop_loss"] = {
                "intended": str(row.stop_loss) if row.stop_loss is not None else None,
                "venue": str(held.stop_loss) if held.stop_loss is not None else None,
            }
        if held.take_profit != row.take_profit:
            changes["take_profit"] = {
                "intended": str(row.take_profit) if row.take_profit is not None else None,
                "venue": str(held.take_profit) if held.take_profit is not None else None,
            }

        row.broker_stop_loss = held.stop_loss
        row.broker_take_profit = held.take_profit
        row.broker_synced_at = naive_utc(at)
        # Back to a manageable state. `partially_closed` is preserved: the
        # venue agreeing about a smaller size does not make it a fresh
        # position.
        row.status = "partially_closed" if previous_status == "partially_closed" else "open"
        report.synced.append(row.id)

        if changes:
            # A stop we believe in that the venue is not holding is the
            # dangerous case, so it is logged at error rather than info.
            missing_stop = row.stop_loss is not None and held.stop_loss is None
            log.log(
                logging.ERROR if missing_stop else logging.WARNING,
                "the venue disagrees with the record about a position",
                extra={
                    "event": "position_reconcile_mismatch",
                    "position_id": row.id,
                    "broker_position_id": row.broker_position_id,
                    "mode": self.mode,
                    "missing_protective_stop": missing_stop,
                },
            )
        await self._event(
            row.id,
            "reconciled",
            at,
            {
                "detail": "compared against the venue",
                "agrees": not changes,
                "differences": changes or None,
                "broker_stop_loss": str(held.stop_loss) if held.stop_loss is not None else None,
                "broker_take_profit": (
                    str(held.take_profit) if held.take_profit is not None else None
                ),
            },
        )

    async def _event(self, position_id: str, event_type: str, at: datetime, payload: dict) -> None:
        """Write one audit entry and queue the bus event it implies.

        Same pairing as the manager, for the same reason: a change recorded in
        one place and published in another eventually disagrees about which
        changes count.
        """
        self.db.add(
            PositionEvent(
                position_id=position_id,
                event_type=event_type[:24],
                occurred_at=naive_utc(at),
                payload=payload,
            )
        )
        row = self._rows.get(position_id)
        kind = event_for(event_type)
        if kind is not None and row is not None:
            self.pending_events.append(
                PositionEventOut(str(kind), payload_for(row, event_type, payload), position_id)
            )

    def drain_events(self) -> list[PositionEventOut]:
        """Take what this sweep produced.

        Published only after the state is durable, which is the one order in
        which an event cannot describe something that was not saved.
        """
        events, self.pending_events = self.pending_events, []
        return events
