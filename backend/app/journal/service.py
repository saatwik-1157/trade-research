"""Writing a journal row from what actually happened, and reading it back.

Section 40: the journal is generated from the execution lifecycle, not typed in.
Section 55: it records, and it may never act.

**This module places nothing.** It imports no order manager, no broker adapter,
no risk decision and no sizing calculator; a parsed test keeps that true. The
only write it performs is an INSERT into `trades` and an UPDATE of the columns a
reconciliation changes — and even that never rewrites a price, a quantity or a
P&L, because §32 makes those historical facts.

**It is idempotent by construction.** §27. `record_close` looks for an existing
row on the same `position_id` before writing one, and a partial unique index
makes the race a database error rather than a duplicate. A duplicate fill event,
a re-delivered broker message and a replayed WebSocket frame all produce the
same single row.

**It never finalises what the venue has not confirmed.** §50. A position parked
as `unknown` produces a trade with status `unknown`, not `closed`; a
reconciliation mismatch produces `reconciliation_required`. Neither is resolved
by choosing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.journal import closes as close_engine
from app.journal import context as context_engine
from app.journal import quality as quality_engine
from app.journal import timeline as timeline_engine
from app.journal.lifecycle import Holding, JournalExit, TradeStatus, exit_reason_of
from app.models.ai_integration import AiDecisionRecord
from app.models.execution import Execution, Order, OrderEvent, Position, PositionEvent, Trade
from app.models.risk import RiskEvent
from app.models.signals import Signal, WebhookEvent

log = logging.getLogger("app.journal")

ZERO = Decimal("0")

#: Position statuses from which a journal row may be written. Section 15 and
#: §50: `open` is not one of them, and neither is `closing` -- a trade recorded
#: before the venue confirmed is a historical fact that may turn out to be
#: false.
RECORDABLE = frozenset({"closed", "unknown"})


class JournalError(Exception):
    """A journal row could not be written, and the message says why."""


@dataclass(frozen=True)
class Recorded:
    """The outcome of one `record_close` call."""

    trade: Trade | None
    created: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade.id if self.trade else None,
            "created": self.created,
            "reason": self.reason,
        }


class TradeJournalService:
    """Assembles trades from the lifecycle. Owns nothing; decides nothing."""

    # ------------------------------------------------------------- writing

    async def record_close(
        self,
        db: AsyncSession,
        position: Position,
        *,
        currency: str | None = None,
    ) -> Recorded:
        """One journal row for one finished position episode. Idempotent.

        Returns the existing row untouched when one is already there. §27 and
        §32 together: a duplicate event must not create a second record, and it
        must not overwrite the first either — a re-delivered fill arriving after
        a correction would otherwise silently undo it.
        """
        existing = await db.scalar(select(Trade).where(Trade.position_id == position.id))
        if existing is not None:
            return Recorded(existing, created=False, reason="a journal row already exists")

        if position.status not in RECORDABLE:
            return Recorded(
                None,
                created=False,
                reason=(
                    f"the position is {position.status!r}. A trade is recorded when the "
                    "episode has ended, and marking one closed before the venue "
                    "confirmed would put an unconfirmed fact in the record."
                ),
            )

        events = list(
            (
                await db.scalars(
                    select(PositionEvent)
                    .where(PositionEvent.position_id == position.id)
                    .order_by(PositionEvent.occurred_at)
                )
            ).all()
        )
        parts = close_engine.closes_from(events)
        exit_of = close_engine.exit_of(parts)
        realized, realized_source = close_engine.realized_from(parts, position.realized_pnl)

        order = await self._entry_order(db, position)
        signal = await self._signal(db, order)
        ai = await self._ai_decision(db, order)
        risk = await self._risk_event(db, order)

        if exit_of.price is None or realized is None:
            # Nothing is invented to fill the gap. §51: an exit price nobody
            # reported and a P&L nobody booked are exactly the fabrications the
            # brief forbids, and a row carrying them would be indistinguishable
            # from a measured one.
            return Recorded(
                None,
                created=False,
                reason=(
                    "no confirmed close carried both a fill price and a booked P&L, so "
                    "there is nothing measured to record. Reported rather than written "
                    "with substituted values."
                ),
            )

        costs = self._costs(events, parts)
        status = TradeStatus.closed if position.status == "closed" else TradeStatus.unknown
        requested = order.requested_price if order is not None else None

        trade = Trade(
            mode=position.mode,
            status=str(status),
            position_id=position.id,
            broker_position_id=position.broker_position_id,
            symbol_id=position.symbol_id,
            strategy_version_id=position.strategy_version_id,
            broker_account_id=position.broker_account_id,
            paper_account_id=position.paper_account_id,
            order_id=order.id if order is not None else None,
            signal_id=signal.id if signal is not None else None,
            ai_decision_id=ai.id if ai is not None else None,
            risk_event_id=risk.id if risk is not None else None,
            bot_id=getattr(ai, "bot_id", None),
            side=position.side,
            # The size that actually closed, summed from the confirmed closes.
            # NOT `initial_quantity`: a position whose last part was never
            # confirmed did not trade that size, and claiming it did would
            # overstate the record.
            volume=exit_of.quantity,
            # Already volume-weighted across fills by L21. Read, not recomputed.
            entry_price=position.entry_price,
            exit_price=exit_of.price,
            requested_entry_price=requested,
            entry_slippage_points=None,
            opened_at=position.opened_at,
            closed_at=exit_of.at or position.closed_at or position.opened_at,
            gross_profit=realized + costs["total"],
            commission=costs["commission"],
            swap=costs["swap"],
            fees=costs["fees"] if costs["fees"] != ZERO else None,
            net_profit=realized,
            currency=currency,
            exit_reason=exit_reason_of(exit_of.reason or None),
            source="pipeline",
        )
        trade.data_quality = quality_engine.summarise(
            quality_engine.check(trade, closes=len(parts))
        )
        trade.data_quality["realized_source"] = realized_source

        db.add(trade)
        try:
            await db.flush()
        except IntegrityError as exc:
            # The partial unique index caught a concurrent writer. The other one
            # won and its row is the record; this is not an error to surface.
            await db.rollback()
            found = await db.scalar(select(Trade).where(Trade.position_id == position.id))
            if found is not None:
                return Recorded(found, created=False, reason="written concurrently by another pass")
            raise JournalError(f"the journal row could not be written: {exc}") from exc
        return Recorded(trade, created=True)

    def _costs(
        self, events: list[PositionEvent], parts: list[close_engine.Close]
    ) -> dict[str, Any]:
        """Commission, swap and fees, from the fills that reported them.

        Zero when nothing reported one, and that is honest here rather than a
        fabrication: a simulator fill genuinely pays no swap, and a venue that
        reported no commission charged none on that deal. The distinction §44
        cares about is the P&L that does not reconcile, and `quality.check`
        catches that from the recorded figures rather than from an assumption
        made here.
        """
        commission = swap = fees = ZERO
        for event in events:
            payload = event.payload or {}
            for key, target in (("commission", "commission"), ("swap", "swap"), ("fee", "fees")):
                value = payload.get(key)
                if value is None:
                    continue
                try:
                    amount = Decimal(str(value))
                except Exception:  # noqa: BLE001 - a malformed payload is not a cost
                    continue
                if target == "commission":
                    commission += amount
                elif target == "swap":
                    swap += amount
                else:
                    fees += amount
        return {
            "commission": commission,
            "swap": swap,
            "fees": fees,
            "total": commission + swap + fees,
        }

    async def _entry_order(self, db: AsyncSession, position: Position) -> Order | None:
        """The order that opened this position, if one can be identified.

        Matched on the account, symbol and side, taking the earliest filled one
        at or before the position opened. `positions` carries no `order_id` —
        L19 and L21 link through the venue rather than through a column — and
        this is a best-effort reconstruction, which is why the trade's
        `order_id` is nullable and the timeline names the gap when it is absent.
        """
        column = Order.paper_account_id if position.mode == "paper" else Order.broker_account_id
        account = (
            position.paper_account_id if position.mode == "paper" else position.broker_account_id
        )
        if account is None:
            return None
        side = "buy" if position.side == "long" else "sell"
        return await db.scalar(
            select(Order)
            .where(
                column == account,
                Order.symbol_id == position.symbol_id,
                Order.side == side,
                Order.status == "filled",
                Order.created_at <= position.opened_at,
            )
            .order_by(Order.created_at.desc())
            .limit(1)
        )

    async def _signal(self, db: AsyncSession, order: Order | None) -> Signal | None:
        if order is None or order.signal_id is None:
            return None
        return await db.get(Signal, order.signal_id)

    async def _ai_decision(self, db: AsyncSession, order: Order | None) -> Any:
        if order is None:
            return None
        return await db.scalar(
            select(AiDecisionRecord).where(AiDecisionRecord.order_id == order.id).limit(1)
        )

    async def _risk_event(self, db: AsyncSession, order: Order | None) -> RiskEvent | None:
        if order is None:
            return None
        return await db.scalar(select(RiskEvent).where(RiskEvent.order_id == order.id).limit(1))

    async def record_pending(
        self,
        db: AsyncSession,
        *,
        mode: str | None = None,
        limit: int = 200,
    ) -> list[Recorded]:
        """Every finished position that has no journal row yet. Section 40.

        **A sweep rather than a hook at each close site.** There are four places
        a position can end -- the position manager, the reconciler, the paper
        service and the close route -- and a hook missed at one of them produces
        a trade that silently never exists. A sweep cannot miss one: it asks the
        database which episodes have ended and which have no row, and the answer
        does not depend on anybody remembering to call it.

        It is safe to run repeatedly. `record_close` is idempotent, so a second
        pass over the same position returns the existing row and writes nothing.
        """
        query = (
            select(Position)
            .outerjoin(Trade, Trade.position_id == Position.id)
            .where(Position.status.in_(sorted(RECORDABLE)), Trade.id.is_(None))
            .order_by(Position.closed_at)
            .limit(limit)
        )
        if mode is not None:
            query = query.where(Position.mode == mode)

        out: list[Recorded] = []
        for position in (await db.scalars(query)).all():
            out.append(await self.record_close(db, position))
        return out

    # ------------------------------------------------------- reconciliation

    async def mark_reconciliation_required(
        self, db: AsyncSession, trade: Trade, *, detail: str
    ) -> Trade:
        """Section 28. The venue disagrees, and nothing here resolves it.

        Only `status` and `data_quality` change. No price, quantity or P&L is
        touched — §32 — because the disagreement is about whether the episode
        ended, not about what the recorded figures were.
        """
        trade.status = str(TradeStatus.reconciliation_required)
        findings = quality_engine.check(trade)
        summary = quality_engine.summarise(findings)
        summary["reconciliation_detail"] = detail
        trade.data_quality = summary
        log.warning(
            "trade marked for reconciliation",
            extra={
                "event": "trade_reconciliation_required",
                "trade_id": trade.id,
                "detail": detail[:200],
            },
        )
        return trade

    # ------------------------------------------------------------- reading

    async def timeline_for(self, db: AsyncSession, trade: Trade) -> dict[str, Any]:
        """Section 26, derived from the recorded rows. Nothing is stored."""
        order = await db.get(Order, trade.order_id) if trade.order_id else None
        signal = await db.get(Signal, trade.signal_id) if trade.signal_id else None
        webhook = (
            await db.get(WebhookEvent, signal.webhook_event_id)
            if signal is not None and signal.webhook_event_id
            else None
        )
        risk = await db.get(RiskEvent, trade.risk_event_id) if trade.risk_event_id else None
        ai = await db.get(AiDecisionRecord, trade.ai_decision_id) if trade.ai_decision_id else None
        order_events = (
            list(
                (
                    await db.scalars(
                        select(OrderEvent)
                        .where(OrderEvent.order_id == order.id)
                        .order_by(OrderEvent.occurred_at)
                    )
                ).all()
            )
            if order is not None
            else []
        )
        executions = (
            list(
                (
                    await db.scalars(
                        select(Execution)
                        .where(Execution.order_id == order.id)
                        .order_by(Execution.executed_at)
                    )
                ).all()
            )
            if order is not None
            else []
        )
        position_events = (
            list(
                (
                    await db.scalars(
                        select(PositionEvent)
                        .where(PositionEvent.position_id == trade.position_id)
                        .order_by(PositionEvent.occurred_at)
                    )
                ).all()
            )
            if trade.position_id
            else []
        )

        events = timeline_engine.derive(
            trade=trade,
            signal=signal,
            webhook=webhook,
            risk=risk,
            ai=ai,
            order=order,
            order_events=order_events,
            executions=executions,
            position_events=position_events,
        )
        return {
            "trade_id": trade.id,
            "events": [event.as_dict() for event in events],
            "count": len(events),
            "gaps": timeline_engine.gaps_in(events),
            "note": (
                "derived at read time from the rows each system already wrote. Nothing "
                "is stored: a second copy could fall behind, and when it disagreed "
                "there would be no way to tell which was right."
            ),
        }

    async def context_for(self, db: AsyncSession, trade: Trade) -> dict[str, Any]:
        """Sections 8 to 12 and §23. What the system knew, as it was written."""
        order = await db.get(Order, trade.order_id) if trade.order_id else None
        signal = await db.get(Signal, trade.signal_id) if trade.signal_id else None
        webhook = (
            await db.get(WebhookEvent, signal.webhook_event_id)
            if signal is not None and signal.webhook_event_id
            else None
        )
        risk = await db.get(RiskEvent, trade.risk_event_id) if trade.risk_event_id else None
        ai = await db.get(AiDecisionRecord, trade.ai_decision_id) if trade.ai_decision_id else None
        executions = (
            list(
                (
                    await db.scalars(
                        select(Execution)
                        .where(Execution.order_id == order.id)
                        .order_by(Execution.executed_at)
                    )
                ).all()
            )
            if order is not None
            else []
        )
        return {
            "trade_id": trade.id,
            "environment": trade.mode,
            "strategy": context_engine.strategy_block(trade, signal, webhook),
            "ai": context_engine.ai_block(trade, ai),
            "risk": context_engine.risk_block(trade, risk),
            "sizing": context_engine.sizing_block(trade, order),
            "execution": context_engine.execution_block(trade, order, executions),
            "costs": context_engine.costs_block(trade),
            "holding": Holding.as_dict(trade.opened_at, trade.closed_at),
            "note": (
                "every block is read from the row written at the time. Nothing is "
                "recalculated with today's values, which section 10 forbids for risk "
                "and which would be the same error anywhere else here."
            ),
        }

    async def closes_for(self, db: AsyncSession, trade: Trade) -> dict[str, Any]:
        """Section 14. Each partial close, itemised, without double-counting."""
        if not trade.position_id:
            return {
                "trade_id": trade.id,
                "closes": [],
                "available": False,
                "why": "no position is linked, so the individual closes are not recorded",
            }
        events = list(
            (
                await db.scalars(
                    select(PositionEvent)
                    .where(PositionEvent.position_id == trade.position_id)
                    .order_by(PositionEvent.occurred_at)
                )
            ).all()
        )
        parts = close_engine.closes_from(events)
        return {
            "trade_id": trade.id,
            "available": True,
            "closes": [part.as_dict() for part in parts],
            "exit": close_engine.exit_of(parts).as_dict(),
            "entry_price": str(trade.entry_price),
            "entry_note": (
                "already volume-weighted across fills by the position record (L21). "
                "Read rather than recomputed: two derivations of one fact eventually "
                "disagree."
            ),
        }

    # -------------------------------------------------------------- events

    async def publish(self, hub: Any | None, trade: Trade, kind: str) -> bool:
        """Through L07's hub. Section 41: never a second realtime system."""
        if hub is None:
            return False
        from app.core.events import Event

        account = trade.paper_account_id or trade.broker_account_id
        if account is None:
            return False
        try:
            await hub.publish(
                Event(
                    type=kind,
                    payload={
                        "trade_id": trade.id,
                        "environment": trade.mode,
                        "status": trade.status,
                        "symbol_id": trade.symbol_id,
                        "side": trade.side,
                        "volume": str(trade.volume),
                        "net_profit": str(trade.net_profit),
                        "r_multiple": None if trade.r_multiple is None else str(trade.r_multiple),
                        "exit_reason": trade.exit_reason,
                        "closed_at": trade.closed_at.isoformat(),
                    },
                    source="trade_journal",
                    channel=f"account:{account}",
                )
            )
        except Exception:  # noqa: BLE001 - a record does not fail because an event did
            log.warning(
                "a trade journal event could not be published",
                extra={"event": "journal_event_failed", "trade_id": trade.id, "type": kind},
            )
            return False
        return True


__all__ = [
    "RECORDABLE",
    "JournalError",
    "JournalExit",
    "Recorded",
    "TradeJournalService",
]
