"""Position manager: evaluate, act, and record every modification.

What it guarantees:

  * A position is marked closed only on a CONFIRMED outcome carrying a fill.
  * A REJECTED close leaves the position open and records why.
  * An UNKNOWN close parks the position in `unknown` and it is never acted on
    again until something reconciles it. No retry, ever.
  * Every stop move and every close attempt is written to `position_events`,
    so the record shows what was decided as well as what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import Position, PositionEvent
from app.models.market import Symbol
from app.positions.events import PositionEventOut, event_for, payload_for
from app.positions.executor import CloseOutcome, CloseStatus, ExitExecutor
from app.positions.ingest import BROKER_MODES, naive_utc
from app.positions.policies import (
    ExitDecision,
    MarketState,
    PolicySet,
    PositionView,
    RiskContext,
    StopModification,
)

#: States a management pass may act on. `partially_closed` is manageable: it
#: is an open position at a smaller size, and the remaining size still needs a
#: stop watched.
MANAGEABLE = frozenset({"open", "partially_closed"})

#: States a pass must NOT act on, and why. Each of these means the venue's
#: answer is not settled, and acting could double-close a position that is
#: still open or close one that was never opened.
NOT_ACTIONABLE: dict[str, str] = {
    "unknown": "status is unknown; awaiting reconciliation",
    "reconciling": "status is reconciling; a sweep is settling it against the venue",
    "closing": "status is closing; a close is in flight and has not been confirmed",
    "opening": "status is opening; no fill has confirmed this position exists",
}


@dataclass(frozen=True)
class StopProposal:
    """A stop move, and which policy proposed it."""

    modification: StopModification
    policy: str

    @property
    def new_stop(self) -> Decimal:
        return self.modification.new_stop

    @property
    def old_stop(self) -> Decimal | None:
        return self.modification.old_stop

    @property
    def reason(self) -> str:
        return self.modification.reason

    @property
    def at(self) -> datetime:
        return self.modification.at


@dataclass(frozen=True)
class ManagedResult:
    """What the manager did to one position on one pass."""

    position_id: str
    decision: ExitDecision | None = None
    outcome: CloseOutcome | None = None
    modification: StopProposal | None = None
    skipped: str | None = None

    @property
    def closed(self) -> bool:
        return self.outcome is not None and self.outcome.is_confirmed

    @property
    def needs_reconciliation(self) -> bool:
        return self.outcome is not None and self.outcome.status is CloseStatus.unknown

    @property
    def partially_closed(self) -> bool:
        """Part of the position was taken off and the rest is still open.

        Deliberately not folded into `closed`: a caller that treats a scale-out
        as a finished trade books a P&L for size that is still at the venue.
        """
        return (
            self.outcome is not None
            and self.outcome.is_confirmed
            and self.decision is not None
            and self.decision.quantity is not None
        )


def view_of(
    row: Position,
    floating_pnl: Decimal | None = None,
    symbol_code: str | None = None,
) -> PositionView:
    """Build a policy view from a database row.

    `symbol_code` is the row's tradable code, which the caller resolves because
    this function has no session. It lands in `PositionView.symbol` -- the one
    place that used to hold `row.symbol_id` in the application and the code in
    every test, and silently broke every close order for it.

    Optional so the callers that only read policy fields stay unchanged, and
    EMPTY rather than substituted when it is missing: an order built from a
    symbol nothing resolves fails at the write, long after the venue was asked.
    """
    return PositionView(
        id=row.id,
        # The CODE. Empty when it could not be resolved, which anything building
        # an order treats as a refusal rather than substituting the row id.
        symbol=symbol_code or "",
        side=row.side,
        quantity=row.quantity,
        entry_price=row.entry_price,
        opened_at=row.opened_at,
        mode=row.mode,
        stop_loss=row.stop_loss,
        take_profit=row.take_profit,
        floating_pnl=floating_pnl,
        account_id=row.broker_account_id or row.paper_account_id,
        broker_position_id=row.broker_position_id,
        # A row written before L21 has 0 here, which would make every partial
        # rule refuse. `quantity` is the honest fallback: a position that has
        # never been partially closed opened at the size it still holds.
        initial_quantity=row.initial_quantity or row.quantity,
        closed_quantity=row.closed_quantity or Decimal("0"),
        broker_stop_loss=row.broker_stop_loss,
        broker_take_profit=row.broker_take_profit,
    )


class PositionManager:
    def __init__(
        self,
        db: AsyncSession,
        executor: ExitExecutor,
        policies: PolicySet | None = None,
    ) -> None:
        self.db = db
        self.executor = executor
        self.policies = policies or PolicySet.default()
        self._pending_events: list[PositionEventOut] = []
        self._codes: dict[str, str] = {}

    async def _symbol_code(self, row: Position) -> str | None:
        """The tradable code for a row's symbol, cached per manager.

        Anything that builds an order needs the CODE and not the row's
        `symbol_id`; see `PositionView.symbol` for what handing over the id
        cost. Cached because a manager processes many positions on one sweep and
        a lookup per position per pass is a query nobody needs.

        None when the symbol is missing, which the caller treats as "cannot
        build an order" rather than substituting the id and failing later.
        """
        if row.symbol_id in self._codes:
            return self._codes[row.symbol_id]
        symbol = await self.db.get(Symbol, row.symbol_id)
        code = symbol.code if symbol is not None else None
        if code:
            self._codes[row.symbol_id] = code
        return code

    async def _record(
        self,
        row: Position,
        event_type: str,
        at: datetime,
        payload: dict,
    ) -> None:
        """Write one audit entry, and queue the bus event it implies.

        One place, so a change that is recorded is a change that is published
        and vice versa -- two separate call sites would eventually disagree
        about which changes count.

        The timestamp is a column, never a payload field: JSON has no
        datetime, and a stringified one is not a queryable time.
        """
        self.db.add(
            PositionEvent(
                position_id=row.id,
                event_type=event_type,
                # Naive, because the column is. The sweep and the exit policies
                # work in aware UTC and must convert before they write --
                # PostgreSQL raises on an aware value and SQLite does not.
                occurred_at=naive_utc(at),
                payload=payload,
            )
        )
        kind = event_for(event_type)
        if kind is not None:
            self._pending_events.append(
                PositionEventOut(str(kind), payload_for(row, event_type, payload), row.id)
            )

    def drain_events(self) -> list[PositionEventOut]:
        """Take the lifecycle events produced since the last drain.

        Queued rather than published inline so a slow or broken bus cannot sit
        in the middle of a venue interaction, and so a caller publishes them
        only AFTER the state is durable -- the one order in which an event can
        never describe something that was not saved.
        """
        events, self._pending_events = self._pending_events, []
        return events

    async def process(
        self,
        row: Position,
        market: MarketState,
        context: RiskContext,
        floating_pnl: Decimal | None = None,
    ) -> ManagedResult:
        """One pass over one position."""
        if row.status in NOT_ACTIONABLE:
            # `unknown` is parked for reconciliation, `reconciling` is being
            # settled right now, `closing` has a close in flight and `opening`
            # has no confirmed fill. Acting on any of them could double-close a
            # position that is still open at the venue, or close one that was
            # never opened.
            return ManagedResult(row.id, skipped=NOT_ACTIONABLE[row.status])
        if row.status not in MANAGEABLE:
            return ManagedResult(row.id, skipped=f"status is {row.status}")

        position = view_of(row, floating_pnl, await self._symbol_code(row))

        # 1. Move the stop first, so a stop moved onto the current price can
        #    trigger StopLossPolicy on this same pass rather than the next one.
        #
        #    Several policies can propose a move on the same tick -- a trail
        #    and a break-even, typically. The MOST PROTECTIVE proposal wins,
        #    not the first: taking whichever policy ran first would make the
        #    outcome depend on list order, which section 20 forbids. Each
        #    policy has already refused to propose a loosening of its own, so
        #    picking the most protective cannot widen risk.
        modification: StopProposal | None = self._best_stop(position, market)
        if modification is not None:
            row.stop_loss = modification.new_stop
            await self._record(
                row,
                "stop_modified",
                modification.at,
                {
                    "old_stop": str(modification.old_stop)
                    if modification.old_stop is not None
                    else None,
                    "new_stop": str(modification.new_stop),
                    "reason": modification.reason,
                    "policy": modification.policy,
                },
            )
            position = PositionView(**{**position.__dict__, "stop_loss": modification.new_stop})

        # A stop this platform believes in that the venue is NOT holding is
        # the most dangerous disagreement this module can observe: the screen
        # says protected and the position is not. It is recorded on every pass
        # it is true, and it does not stop the pass -- the position still
        # needs managing, and now visibly needs reconciling too.
        gap = position.protection_gap
        if gap is not None:
            await self._record(
                row,
                "protection_mismatch",
                market.as_of,
                {
                    "detail": gap,
                    "intended_stop": str(position.stop_loss),
                    "broker_stop": str(position.broker_stop_loss),
                    "next": "reconcile against the venue",
                },
            )

        # 2. Decide.
        decision = self.policies.decide(position, market, context)
        if decision is None:
            await self.db.flush()
            return ManagedResult(row.id, modification=modification)

        await self._record(
            row,
            "exit_decided",
            decision.decided_at,
            {
                "reason": str(decision.reason),
                "detail": decision.detail,
                "reference_price": str(decision.reference_price),
                "mode": row.mode,
            },
        )

        # 3. Act, and believe only a confirmation.
        outcome = await self._act(row, position, market, decision)

        await self.db.flush()
        return ManagedResult(row.id, decision=decision, outcome=outcome, modification=modification)

    async def _act(
        self,
        row: Position,
        position: PositionView,
        market: MarketState,
        decision: ExitDecision,
    ) -> CloseOutcome:
        """Send the close and record what came back. ONE confirmation contract.

        Extracted so `process` (which decides for itself) and `close_now`
        (which acts on a caller's decision) cannot drift apart about what
        counts as closed. The rules are unchanged from L21's original:

          * CONFIRMED with a fill price closes, or partially closes.
          * CONFIRMED without a fill price is NOT a confirmation.
          * UNKNOWN parks the position and nothing retries it.
          * REJECTED leaves it open with the reason recorded.
        """
        outcome = await self.executor.close(position, market, decision)
        if outcome.status is CloseStatus.confirmed and outcome.fill_price is not None:
            self._book(row, decision, outcome)
            await self._record(
                row,
                "partially_closed" if row.status == "partially_closed" else "closed",
                outcome.closed_at or decision.decided_at,
                {
                    "reason": str(decision.reason),
                    "fill_price": str(outcome.fill_price),
                    "reference_price": str(decision.reference_price),
                    "fill_source": outcome.fill_source,
                    # Where the money figure came from. `realized_pnl` below is
                    # the row's running total; this says whether the venue
                    # supplied it or the close left a named gap, so a reader can
                    # tell a booked zero from an unbooked one.
                    "realized_pnl_source": (
                        "venue" if outcome.realized_pnl is not None else "not reported"
                    ),
                    "broker_deal_id": outcome.broker_deal_id,
                    "detail": outcome.detail,
                    "closed_quantity": str(decision.quantity or position.quantity),
                    "remaining_quantity": str(row.quantity),
                    "realized_pnl": str(row.realized_pnl) if row.realized_pnl is not None else None,
                },
            )
        elif outcome.status is CloseStatus.confirmed:
            # Confirmed without a fill price is not a confirmation.
            row.status = "unknown"
            await self._record(
                row,
                "close_unknown",
                decision.decided_at,
                {
                    "reason": str(decision.reason),
                    "detail": "venue reported confirmed with no fill price",
                },
            )
            outcome = CloseOutcome(
                CloseStatus.unknown,
                "confirmed without a fill price; treated as unknown",
                fill_source=outcome.fill_source,
            )
        elif outcome.status is CloseStatus.unknown:
            row.status = "unknown"
            await self._record(
                row,
                "close_unknown",
                decision.decided_at,
                {
                    "reason": str(decision.reason),
                    "detail": outcome.detail,
                    "next": "reconcile against the venue; do not retry",
                },
            )
        else:
            await self._record(
                row,
                "close_rejected",
                decision.decided_at,
                {
                    "reason": str(decision.reason),
                    "detail": outcome.detail,
                },
            )
        return outcome

    async def close_now(
        self,
        row: Position,
        market: MarketState,
        decision: ExitDecision,
    ) -> ManagedResult:
        """Act on a decision the CALLER made — an operator close, a risk exit.

        Separate from `process`, which decides for itself. Split deliberately:
        a method that both decided and acted could not offer "close this,
        because I said so" without also re-running the policies, and an
        operator close that a policy could veto would be an operator close in
        name only. Everything after the decision is identical, which is what
        keeps the confirmation contract in one place.
        """
        position = view_of(row, None, await self._symbol_code(row))
        await self._record(
            row,
            "exit_decided",
            decision.decided_at,
            {
                "reason": str(decision.reason),
                "detail": decision.detail,
                "reference_price": str(decision.reference_price),
                "quantity": str(decision.quantity) if decision.quantity is not None else None,
                "mode": row.mode,
                "source": "caller",
            },
        )
        outcome = await self._act(row, position, market, decision)
        await self.db.flush()
        return ManagedResult(row.id, decision=decision, outcome=outcome)

    # ------------------------------------------------------------- helpers

    def _best_stop(self, position: PositionView, market: MarketState) -> StopProposal | None:
        """The most protective stop any policy proposes, or None.

        "Most protective" is the highest stop on a long and the lowest on a
        short. Deterministic, order-independent, and incapable of loosening.
        """
        best: StopProposal | None = None
        for policy in self.policies.stop_movers():
            proposal = policy.proposed_stop(position, market)
            if proposal is None:
                continue
            candidate = StopProposal(proposal, policy.name)
            if best is None:
                best = candidate
                continue
            tighter = (
                proposal.new_stop > best.new_stop
                if position.is_long
                else proposal.new_stop < best.new_stop
            )
            if tighter:
                best = candidate
        return best

    @staticmethod
    def _book(row: Position, decision: ExitDecision, outcome: CloseOutcome) -> None:
        """Apply a confirmed fill to the row: quantity, status, realised P&L.

        The fill price is the VENUE's, never the price the decision was made
        against, and the two are recorded separately for exactly that reason.
        """
        fill = outcome.fill_price
        assert fill is not None  # the caller checked

        # A row written before L21 carries 0 here. Backfilling it from the
        # current quantity is a measurement, not an assumption: a position
        # that has never been partially closed opened at the size it holds,
        # and the partial-close mechanism did not exist when the row was
        # written. Doing it here rather than in a migration means a row
        # created by any writer that forgets the column is still correct.
        if not row.initial_quantity:
            row.initial_quantity = row.quantity + (row.closed_quantity or Decimal("0"))

        closed_now = decision.quantity if decision.quantity is not None else row.quantity
        if closed_now > row.quantity:  # pragma: no cover - the executor refuses first
            closed_now = row.quantity

        # MONEY COMES FROM THE VENUE.
        #
        # `positions.realized_pnl` is documented as "what the account actually
        # received" -- `test_trade_journal.py` says so in as many words -- and
        # the arithmetic below is not that. `(fill - entry) * quantity` is a
        # PRICE DIFFERENCE: it omits the contract size, so 0.01 lots of EURUSD
        # from 1.16319 to 1.16315 books -0.0000004 where the account received
        # -0.04, and a NUMERIC(18,4) column stores that as 0.0000. Measured
        # against a live MT5 demo close on 2026-09-07.
        #
        # There is no single multiplier that repairs it. Contract size alone
        # fixes EURUSD and leaves USDJPY, XAUUSD and DE40 wrong, because the
        # profit currency is not always the account currency. So the figure is
        # taken from the venue, which has already done the conversion.
        direction = Decimal(1) if row.side == "long" else Decimal(-1)
        booked = outcome.realized_pnl
        if booked is None and row.mode not in BROKER_MODES:
            # The simulator is its own venue and its arithmetic is
            # self-consistent in the units it quotes. Unchanged.
            booked = (fill - row.entry_price) * direction * closed_now
        if booked is not None:
            row.realized_pnl = (row.realized_pnl or Decimal("0")) + booked
        # Otherwise: a broker close whose money the venue did not report. The
        # gap is LEFT as a gap -- `realized_pnl` is untouched and the event says
        # why. Reporting nothing is recoverable, because reconciliation can read
        # the deal history; booking the price difference would put a wrong
        # number in the column every downstream reader trusts.

        row.closed_quantity = (row.closed_quantity or Decimal("0")) + closed_now
        row.quantity = row.quantity - closed_now
        if row.quantity <= 0:
            row.status = "closed"
            row.closed_at = naive_utc(outcome.closed_at or decision.decided_at)
        else:
            # Still open, at a smaller size. NOT `closed`, and not `open`
            # either: the remaining size is no longer the size that was
            # risk-sized, and a table showing it as `open` would hide that.
            row.status = "partially_closed"

    async def open_positions(self, mode: str | None = None) -> list[Position]:
        query = select(Position).where(Position.status.in_(sorted(MANAGEABLE)))
        if mode is not None:
            query = query.where(Position.mode == mode)
        return list((await self.db.scalars(query.order_by(Position.opened_at))).all())

    async def codes_for(self, rows: list[Position]) -> dict[str, Position]:
        """Open positions keyed by tradable CODE, one row per code.

        The key a quote source has to answer on, and the same key `run_once`
        looks a quote up under. Where two positions share a code the first is
        kept: they are the same instrument, so one quote serves both, and the
        row travels only to say which venue holds it.
        """
        by_code: dict[str, Position] = {}
        for row in rows:
            code = await self._symbol_code(row)
            if code and code not in by_code:
                by_code[code] = row
        return by_code

    async def run_once(
        self,
        quotes: dict[str, MarketState],
        context: RiskContext,
        mode: str | None = None,
        floating: dict[str, Decimal] | None = None,
    ) -> list[ManagedResult]:
        """One sweep over every open position we have a quote for."""
        results: list[ManagedResult] = []
        for row in await self.open_positions(mode):
            code = await self._symbol_code(row)
            if not code:
                results.append(ManagedResult(row.id, skipped="symbol does not resolve"))
                continue
            # Keyed by the tradable CODE, like the quotes themselves: a market
            # feed is keyed by symbol, not by this platform's row ids.
            market = quotes.get(code)
            if market is None:
                # No quote is not a reason to close, and it is not a reason to
                # pretend the position is fine either: it is recorded and the
                # position is left alone.
                results.append(ManagedResult(row.id, skipped="no quote available"))
                continue
            results.append(await self.process(row, market, context, (floating or {}).get(row.id)))
        await self.db.commit()
        return results
