"""Closing a demo or live position, through the OMS and nothing else.

This is the gap `MIGRATION_STATUS.md` recorded against L21 as "demo/live
executor — arrives with L10". L10 built the adapter; L19 built the OMS. This
is the executor that uses them, and it replaces `UnavailableExitExecutor` for
those two modes.

**A close is an order.** It takes the same path any other order takes — the
Risk Engine approves it, the OMS creates and sends it, the adapter reaches the
venue — because a close that skipped those would be a second execution path,
and the second path is always the one nobody is watching.

**That claim was false for four levels, and this paragraph is the reason it is
worth reading carefully now.** Until the L45 C-2 fix this module called
`manager.adapter.close_position(...)` directly, borrowing the `OrderManager`
purely as a container for `.adapter`. There was no `Approval`, no RiskEngine
call, no `OrderManager` method, no `intent_id` and no order record — and the
docstring above said otherwise, in this file, at the top. It is proof that a
confident docstring can be exactly wrong, which is why the guard test below it
now reads the code rather than trusting the prose.

What was actually at stake was idempotency, not permission. No `intent_id`
meant two concurrent close requests were two `close_position` calls at the
venue, and on a hedging account a double close OPENS a position the other way.
No order record meant reconciliation could not see the close at all.

**A close is approved, not vetoed.** `RiskEngine.approve_close` is a separate
entry point from `approve`, and the difference is deliberate: the limits this
platform enforces bound the risk of TAKING a position, so applying them to a
close would refuse to reduce exposure at the moment exposure is worst — a
breached daily loss would trap the position, and a kill switch would trap
every open position behind it. The verdict is still computed and still
recorded, and every limit it breached is named in `not_enforced`. The one
check that still refuses is the mode fence, because a close is still an
instruction transmitted to a venue.

**Nothing is assumed.** The three outcomes are the ones `CloseOutcome` already
has, and they mean exactly what they say:

  * CONFIRMED — the venue reported a fill, and the fill is the venue's number.
  * REJECTED — the venue refused, and said why. The position is still open.
  * UNKNOWN — we do not know. The request may or may not have reached the
    venue; an IPC timeout after a close looks exactly like a rejection from
    here. The position parks, and **nothing retries it**. Retrying an
    uncertain close is how a position gets closed twice, which on a hedging
    account opens a new one in the opposite direction.

**A partial close is a close of part.** `decision.quantity` names how much,
and it is checked against what is actually open — a request to close more than
exists is refused rather than clamped, because the difference between "close
30 of 70" and "close 30 of 100" is a position size nobody chose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.oms.registry import NoOrderManager, OrderManagerRegistry
from app.oms.service import OrderRefused, ReconciliationRequired
from app.oms.state import OrderStatus
from app.positions.executor import CloseOutcome, CloseStatus
from app.positions.policies import ExitDecision, MarketState, PositionView
from app.risk.engine import OrderProposal, RiskEngine, RiskLimits

log = logging.getLogger("app.positions.broker_executor")


@dataclass
class BrokerExitExecutor:
    """Closes a position at a real venue, via the account's order manager.

    It holds a registry rather than an adapter, for the same reason the OMS
    does: an adapter handed out for the wrong account closes the wrong
    position, and `OrderManagerRegistry.get` raises rather than falling back
    to "the other one".
    """

    managers: OrderManagerRegistry
    mode: str = "demo"
    fill_source: str = "broker"
    #: The engine that mints the close's `Approval`. Optional, and a default is
    #: sound HERE and nowhere else in this codebase: `approve_close` enforces
    #: only `LimitKind.trading_mode`, which reads the proposal's mode rather
    #: than the configured limits, so a bare engine is exactly as strict as a
    #: fully configured one. The route passes the real engine anyway.
    risk: RiskEngine | None = None
    #: Where close orders are written down. Optional for the same reason the
    #: pipeline's is, and reported the same way.
    store: object | None = None

    def _engine(self) -> RiskEngine:
        if self.risk is None:
            self.risk = RiskEngine(RiskLimits())
        return self.risk

    async def close(
        self, position: PositionView, market: MarketState, decision: ExitDecision
    ) -> CloseOutcome:
        if position.broker_position_id is None:
            # Nothing at the venue to close BY id. That is not the same as
            # knowing the venue holds nothing, so it is unknown rather than
            # rejected: a rejection would say "the position is still open and
            # fine", and we cannot say that.
            return self._unknown(
                position,
                "the position has no broker id, so the venue cannot be asked about it",
            )

        account_id = position.account_id
        if not account_id:
            return CloseOutcome(
                CloseStatus.rejected,
                "the position names no account, so there is no order manager to route "
                "the close through",
                fill_source="none",
            )

        try:
            manager = self.managers.get(account_id)
        except NoOrderManager as exc:
            # No venue is registered. Rejected rather than unknown: nothing
            # was sent, and nothing could have been.
            return CloseOutcome(CloseStatus.rejected, str(exc), fill_source="none")

        if manager.mode != self.mode:
            return CloseOutcome(
                CloseStatus.rejected,
                f"the position is {self.mode} and the account's manager runs "
                f"{manager.mode}; refusing to close in an environment the position is "
                "not in",
                fill_source="none",
            )

        volume = self._volume(position, decision)
        if volume is None:
            return CloseOutcome(
                CloseStatus.rejected,
                f"a close of {decision.quantity} exceeds the {position.quantity} open on "
                f"{position.symbol}; refusing rather than clamping, because the "
                "difference is a position size nobody chose",
                fill_source="none",
            )

        # RISK. The engine is the only thing that can mint an `Approval`, and
        # the OMS creates nothing without one, so there is no path from here to
        # a venue that does not pass through both.
        if not position.symbol:
            # No tradable code means no order can be built that the order tables
            # will accept. Rejected rather than attempted: sending a close whose
            # record is guaranteed to fail is how a venue ends up holding
            # something the database has never heard of.
            return CloseOutcome(
                CloseStatus.rejected,
                f"position {position.id} has no resolved symbol code, so a close "
                "order cannot be built; the venue was not asked",
                fill_source="none",
            )

        proposal = OrderProposal(
            # `position.symbol` is the tradable CODE. It held the row's
            # symbol_id until 2026-09-07, which is why every close order failed
            # to record: the order store resolves a code.
            symbol=position.symbol,
            # A close is the opposite side of what is open. `PositionView` uses
            # long/short; an order uses buy/sell.
            side="sell" if position.side == "long" else "buy",
            mode=self.mode,
            volume=volume,
            entry_price=decision.reference_price,
            account_id=account_id,
            strategy_id=position.strategy_version_id,
        )
        approval, verdict = self._engine().approve_close(proposal)
        if approval is None:
            # Only the mode fence can do this. Rejected rather than unknown:
            # nothing was sent and nothing could have been.
            return CloseOutcome(
                CloseStatus.rejected,
                f"the risk engine refused this close: {verdict.reason}"[:300],
                fill_source="none",
            )

        # OMS. The `intent_id` is what makes two concurrent requests to close
        # the same amount of the same position ONE close at the venue. The
        # account lock closes the window inside this process; the intent is
        # what holds when there is more than one.
        intent = f"close:{position.id}:{volume}"
        async with self.managers.lock(account_id):
            try:
                manager.guard_resend(intent)
                submission = manager.create(
                    approval,
                    intent_id=intent,
                    account_id=account_id,
                    sizing_snapshot={
                        "closes_position": position.id,
                        "broker_position_id": position.broker_position_id,
                        "exit_reason": str(decision.reason),
                    },
                )
            except ReconciliationRequired as exc:
                # A previous close for this intent is unresolved at the venue.
                # Nothing is sent: retrying an uncertain close is how a
                # position gets closed twice.
                return self._unknown(position, str(exc))
            except OrderRefused as exc:
                return CloseOutcome(CloseStatus.rejected, str(exc)[:300], fill_source="none")

            if submission.duplicate:
                return CloseOutcome(
                    CloseStatus.rejected,
                    f"this close is already order {submission.order.id} in state "
                    f"{submission.order.status}; it is not sent twice",
                    fill_source="none",
                )

            order = submission.order
            await self._record(order)
            closed = await manager.close(order, broker_position_id=position.broker_position_id)
            await self._record(closed)

        if closed.status is OrderStatus.unknown:
            return self._unknown(position, closed.reject_reason or "the venue's answer was unclear")

        if closed.status in (OrderStatus.rejected, OrderStatus.failed):
            return CloseOutcome(
                CloseStatus.rejected,
                (closed.reject_reason or "the venue refused the close")[:300],
                fill_source="none",
            )

        if closed.status is not OrderStatus.filled:
            # Accepted, acknowledged or partly filled. None of those is "this
            # position is closed by the volume asked for", and reporting one as
            # a confirmation would tell the position manager something the
            # venue has not said. It parks for reconciliation.
            return self._unknown(
                position,
                f"the venue left the close in `{closed.status}`; a close is confirmed "
                "only on a full fill",
            )

        if closed.average_fill_price is None:
            # Filled with no fill figure. That is an acknowledgement, not an
            # execution, and treating it as one would invent the number this
            # whole repository exists to avoid inventing.
            return self._unknown(
                position,
                "the venue accepted the close but reported no fill price; a close is "
                "not confirmed until the fill is known",
            )

        # The venue's own claim about where the fill came from, kept rather
        # than replaced by this executor's default. `fill_source` is what makes
        # a simulated fill and a broker fill distinguishable in the record, and
        # overwriting it here would erase exactly that distinction.
        recorded = closed.book.fills
        return CloseOutcome(
            CloseStatus.confirmed,
            f"closed at the venue as order {closed.id}",
            fill_price=closed.average_fill_price,
            closed_at=closed.filled_at or datetime.now(UTC),
            broker_deal_id=closed.broker_order_id,
            fill_source=(recorded[-1].fill_source if recorded else self.fill_source),
            # The venue's own money figure, carried off the fill it reported.
            realized_pnl=(recorded[-1].realized_pnl if recorded else None),
        )

    async def _record(self, order: object) -> None:
        """Write the close order down, if this executor was given somewhere to
        write it. **The same L45 C-1 reasoning as the execution pipeline**: an
        order the venue may hold and the database has never heard of is the
        state no guard can reason about.

        A failure is logged and swallowed rather than raised, which is the
        opposite of the pipeline's choice and is right here: the pipeline can
        refuse to send and lose nothing, while a close that is refused because
        storage blinked leaves a position open that a policy decided to shut.
        """
        if self.store is None:
            return
        try:
            await self.store.record(order)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.exception(
                "a close order could not be recorded; the close proceeds and the "
                "record does not, so reconcile this position against the venue",
                extra={"event": "close_order_not_recorded", "order_id": getattr(order, "id", "")},
            )

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _volume(position: PositionView, decision: ExitDecision) -> Decimal | None:
        """How much to close: all of it, or the part the decision named.

        `None` means the request is impossible and the caller refuses. It is
        deliberately not "clamp to what is open": a caller asking to close 30
        of a position it believes is 100 has a stale view, and giving it 30 of
        70 silently leaves it believing something false about the remaining 40.
        """
        wanted = decision.quantity
        if wanted is None:
            return None if position.quantity <= 0 else position.quantity
        if wanted <= 0 or wanted > position.quantity:
            return None
        return wanted

    def _unknown(self, position: PositionView, detail: str) -> CloseOutcome:
        log.error(
            "close outcome unknown; the position parks for reconciliation and is NOT retried",
            extra={
                "event": "position_close_unknown",
                "position_id": position.id,
                "broker_position_id": position.broker_position_id,
                "symbol": position.symbol,
                "mode": self.mode,
                "reason": detail[:300],
            },
        )
        return CloseOutcome(CloseStatus.unknown, detail[:300], fill_source="none")
