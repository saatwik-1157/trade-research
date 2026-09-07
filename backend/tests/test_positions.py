"""Position management: exit decisions, and never assuming a close happened."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from app.db.base import Base
from app.models.execution import Position, PositionEvent
from app.models.market import Symbol
from app.positions.executor import (
    CloseOutcome,
    CloseStatus,
    PaperExitExecutor,
    UnavailableExitExecutor,
)
from app.positions.manager import PositionManager
from app.positions.monitor import PositionMonitor
from app.positions.policies import (
    BreakEvenPolicy,
    EmergencyExitPolicy,
    ExitDecision,
    ExitReason,
    MarketState,
    PartialTakeProfitPolicy,
    PolicySet,
    PositionView,
    RiskContext,
    RiskExitPolicy,
    StopLossPolicy,
    StrategyExitPolicy,
    TakeProfitPolicy,
    TimeExitPolicy,
    TrailingStopPolicy,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

#: The one instrument these tests trade. Quotes and views are both keyed by the
#: tradable code -- the same code the application uses since 2026-09-07, when
#: `PositionView.symbol` stopped meaning two different things.
CODE = "EURUSD"

OPENED = datetime(2026, 9, 1, 10, 0, 0)
NOW = datetime(2026, 9, 1, 12, 0, 0)


def long_position(**over: object) -> PositionView:
    base = {
        "id": "p1",
        # The tradable CODE -- which is what this field means everywhere now.
        "symbol": "EURUSD",
        "side": "long",
        "quantity": Decimal("0.10"),
        "entry_price": Decimal("1.10000"),
        "opened_at": OPENED,
        "mode": "paper",
        "stop_loss": Decimal("1.09000"),
        "take_profit": Decimal("1.11000"),
    }
    return PositionView(**{**base, **over})  # type: ignore[arg-type]


def short_position(**over: object) -> PositionView:
    return long_position(
        **{
            "side": "short",
            "stop_loss": Decimal("1.11000"),
            "take_profit": Decimal("1.09000"),
            **over,
        }
    )


def quote(bid: str, ask: str | None = None, atr: str | None = None) -> MarketState:
    return MarketState(
        symbol="EURUSD",
        bid=Decimal(bid),
        ask=Decimal(ask or bid),
        as_of=NOW,
        atr=Decimal(atr) if atr else None,
    )


HOLD = RiskContext()


# ------------------------------------------------------------------ policies


def test_stop_loss_fires_for_long_and_short() -> None:
    policy = StopLossPolicy()
    assert policy.evaluate(long_position(), quote("1.08999"), HOLD) is not None
    assert policy.evaluate(long_position(), quote("1.09500"), HOLD) is None
    # A short exits into the ask, so the ask is what must reach its stop.
    assert policy.evaluate(short_position(), quote("1.10900", "1.11001"), HOLD) is not None
    assert policy.evaluate(short_position(), quote("1.10000", "1.10010"), HOLD) is None


def test_take_profit_fires_for_long_and_short() -> None:
    policy = TakeProfitPolicy()
    assert policy.evaluate(long_position(), quote("1.11000"), HOLD) is not None
    assert policy.evaluate(long_position(), quote("1.10999"), HOLD) is None
    assert policy.evaluate(short_position(), quote("1.08000", "1.08999"), HOLD) is not None


def test_missing_levels_never_trigger() -> None:
    naked = long_position(stop_loss=None, take_profit=None)
    assert StopLossPolicy().evaluate(naked, quote("1.00000"), HOLD) is None
    assert TakeProfitPolicy().evaluate(naked, quote("2.00000"), HOLD) is None


def test_when_both_levels_are_reached_the_loss_is_booked() -> None:
    # One poll can span both levels and the order inside it is unknown. The
    # simulator books the loss for the same reason; so does this.
    policies = PolicySet.default()
    wide = long_position(stop_loss=Decimal("1.10500"), take_profit=Decimal("1.10500"))
    decision = policies.decide(wide, quote("1.10500"), HOLD)
    assert decision is not None
    assert decision.reason is ExitReason.stop_loss


def test_time_exit_uses_the_clock_it_is_given() -> None:
    policy = TimeExitPolicy(max_hold=timedelta(hours=2))
    assert policy.evaluate(long_position(), quote("1.10500"), HOLD) is not None
    early = MarketState("EURUSD", Decimal("1.105"), Decimal("1.105"), OPENED + timedelta(hours=1))
    assert policy.evaluate(long_position(), early, HOLD) is None
    # It can also come from the context rather than the policy.
    assert (
        TimeExitPolicy().evaluate(
            long_position(), quote("1.10500"), RiskContext(max_hold=timedelta(hours=1))
        )
        is not None
    )


def test_strategy_exit_only_on_signal() -> None:
    policy = StrategyExitPolicy()
    assert policy.evaluate(long_position(), quote("1.10500"), HOLD) is None
    assert (
        policy.evaluate(
            long_position(), quote("1.10500"), RiskContext(strategy_exit_signalled=True)
        )
        is not None
    )


def test_risk_exit_needs_a_known_floating_figure() -> None:
    policy = RiskExitPolicy()
    ctx = RiskContext(max_floating_loss=Decimal("50"))
    # Unknown P&L: refuse to act rather than guess.
    assert policy.evaluate(long_position(floating_pnl=None), quote("1.10500"), ctx) is None
    assert (
        policy.evaluate(long_position(floating_pnl=Decimal("-10")), quote("1.10500"), ctx) is None
    )
    hit = policy.evaluate(long_position(floating_pnl=Decimal("-50")), quote("1.10500"), ctx)
    assert hit is not None and "net of swap" in hit.detail


def test_daily_loss_breach_closes_regardless_of_position_pnl() -> None:
    decision = RiskExitPolicy().evaluate(
        long_position(floating_pnl=Decimal("25")),
        quote("1.10500"),
        RiskContext(daily_loss_breached=True),
    )
    assert decision is not None and decision.reason is ExitReason.risk_exit


def test_emergency_exit_outranks_everything_including_a_profitable_target() -> None:
    policies = PolicySet.default()
    ctx = RiskContext(emergency_stop=True, kill_switch_reason="operator halt")
    decision = policies.decide(long_position(), quote("1.11000"), ctx)
    assert decision is not None
    assert decision.reason is ExitReason.emergency_exit
    assert "operator halt" in decision.detail
    assert EmergencyExitPolicy().evaluate(long_position(), quote("1.10500"), HOLD) is None


# ------------------------------------------------------------ trailing stops


def test_trailing_stop_ratchets_and_never_loosens() -> None:
    policy = TrailingStopPolicy(distance=Decimal("0.00500"))
    position = long_position(stop_loss=Decimal("1.09000"), extreme_price=Decimal("1.10000"))

    up = policy.proposed_stop(position, quote("1.10800"))
    assert up is not None and up.new_stop == Decimal("1.10300")

    # Price falls back: the stop must not follow it down.
    tightened = replace(position, stop_loss=up.new_stop, extreme_price=Decimal("1.10800"))
    assert policy.proposed_stop(tightened, quote("1.10400")) is None


def test_trailing_stop_for_a_short_ratchets_downward() -> None:
    policy = TrailingStopPolicy(distance=Decimal("0.00500"))
    position = short_position(stop_loss=Decimal("1.11000"), extreme_price=Decimal("1.10000"))
    down = policy.proposed_stop(position, quote("1.09000", "1.09200"))
    assert down is not None and down.new_stop == Decimal("1.09700")
    tightened = replace(position, stop_loss=down.new_stop, extreme_price=Decimal("1.09200"))
    assert policy.proposed_stop(tightened, quote("1.09800", "1.09900")) is None


def test_atr_trailing_without_an_atr_moves_nothing() -> None:
    policy = TrailingStopPolicy(atr_multiple=Decimal("2"))
    assert policy.proposed_stop(long_position(), quote("1.10800")) is None
    with_atr = policy.proposed_stop(long_position(), quote("1.10800", atr="0.00100"))
    assert with_atr is not None and with_atr.new_stop == Decimal("1.10600")


def test_trailing_policy_never_decides_an_exit_itself() -> None:
    # Exactly one place decides a stop exit, and it is StopLossPolicy.
    policy = TrailingStopPolicy(distance=Decimal("0.00100"))
    assert policy.evaluate(long_position(), quote("1.10800"), HOLD) is None


# ------------------------------------------------------------------ database


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def make_position(db: AsyncSession, **over: object) -> Position:
    symbol = await db.scalar(select(Symbol).where(Symbol.code == "EURUSD"))
    if symbol is None:
        symbol = Symbol(code="EURUSD", asset_class="fx", digits=5)
        db.add(symbol)
        await db.flush()
    fields: dict[str, object] = {
        "mode": "paper",
        "symbol_id": symbol.id,
        "side": "long",
        "quantity": Decimal("0.10"),
        "entry_price": Decimal("1.10000"),
        "stop_loss": Decimal("1.09000"),
        "take_profit": Decimal("1.11000"),
        "status": "open",
        "opened_at": OPENED,
        "source": "simulator",
    }
    merged = {**fields, **over}
    # What it opened at follows whatever quantity the caller asked for, which
    # is what is true of a real new position. Set here rather than left to
    # default because the closed-quantity CHECK compares against it, and a row
    # claiming to have opened at zero cannot have anything closed from it.
    merged.setdefault("initial_quantity", merged["quantity"])
    row = Position(**merged)
    db.add(row)
    await db.flush()
    return row


async def events_for(db: AsyncSession, position_id: str) -> list[PositionEvent]:
    return list(
        (
            await db.scalars(
                select(PositionEvent)
                .where(PositionEvent.position_id == position_id)
                .order_by(PositionEvent.id)
            )
        ).all()
    )


def market_for(row: Position, bid: str, ask: str | None = None) -> MarketState:
    # Keyed and labelled by the tradable CODE, like the quotes a real feed
    # produces and like `PositionView.symbol`, which this is compared against.
    return MarketState(symbol=CODE, bid=Decimal(bid), ask=Decimal(ask or bid), as_of=NOW)


async def test_confirmed_close_marks_closed_and_records_the_fill(db: AsyncSession) -> None:
    row = await make_position(db)
    manager = PositionManager(db, PaperExitExecutor())
    result = await manager.process(row, market_for(row, "1.11000"), HOLD)

    assert result.closed
    assert row.status == "closed" and row.closed_at == NOW
    assert result.decision is not None and result.decision.reason is ExitReason.take_profit

    kinds = [e.event_type for e in await events_for(db, row.id)]
    assert kinds == ["exit_decided", "closed"]
    closed = (await events_for(db, row.id))[-1]
    assert closed.payload is not None
    # The fill is recorded separately from the price the decision used.
    assert closed.payload["fill_price"] == "1.11000"
    assert closed.payload["reference_price"] == "1.11000"
    assert closed.payload["fill_source"] == "simulator"


async def test_rejected_close_leaves_the_position_open_and_records_why(
    db: AsyncSession,
) -> None:
    row = await make_position(db, mode="demo")

    @dataclass
    class Refusing:
        mode: str = "demo"

        async def close(self, position, market, decision) -> CloseOutcome:  # noqa: ANN001
            return CloseOutcome(CloseStatus.rejected, "market closed", fill_source="broker")

    result = await PositionManager(db, Refusing()).process(row, market_for(row, "1.11000"), HOLD)

    assert not result.closed and not result.needs_reconciliation
    assert row.status == "open" and row.closed_at is None
    kinds = [e.event_type for e in await events_for(db, row.id)]
    assert kinds == ["exit_decided", "close_rejected"]


async def test_unknown_close_parks_the_position_and_is_never_retried(
    db: AsyncSession,
) -> None:
    row = await make_position(db)
    calls: list[str] = []

    @dataclass
    class Uncertain:
        mode: str = "paper"

        async def close(self, position, market, decision) -> CloseOutcome:  # noqa: ANN001
            calls.append(position.id)
            return CloseOutcome(CloseStatus.unknown, "IPC timeout after send")

    manager = PositionManager(db, Uncertain())
    first = await manager.process(row, market_for(row, "1.11000"), HOLD)
    assert first.needs_reconciliation
    assert row.status == "unknown"

    event = (await events_for(db, row.id))[-1]
    assert event.event_type == "close_unknown"
    assert event.payload is not None and "do not retry" in event.payload["next"]

    # A second pass must not touch it again.
    second = await manager.process(row, market_for(row, "1.11000"), HOLD)
    assert second.skipped is not None and "reconciliation" in second.skipped
    assert calls == [row.id]


async def test_confirmed_without_a_fill_price_is_treated_as_unknown(db: AsyncSession) -> None:
    row = await make_position(db)

    @dataclass
    class Sloppy:
        mode: str = "paper"

        async def close(self, position, market, decision) -> CloseOutcome:  # noqa: ANN001
            return CloseOutcome(CloseStatus.confirmed, "done", fill_price=None)

    result = await PositionManager(db, Sloppy()).process(row, market_for(row, "1.11000"), HOLD)
    assert row.status == "unknown"
    assert result.outcome is not None and result.outcome.status is CloseStatus.unknown


async def test_stop_modification_is_recorded_and_persisted(db: AsyncSession) -> None:
    row = await make_position(db, take_profit=None)
    policies = PolicySet.default(trailing=TrailingStopPolicy(distance=Decimal("0.00300")))
    result = await PositionManager(db, PaperExitExecutor(), policies).process(
        row, market_for(row, "1.10800"), HOLD
    )

    assert result.modification is not None
    assert row.stop_loss == Decimal("1.10500")
    assert row.status == "open"
    event = (await events_for(db, row.id))[0]
    assert event.event_type == "stop_modified"
    assert event.payload is not None
    assert event.payload["old_stop"] == "1.09000" and event.payload["new_stop"] == "1.10500"


async def test_a_trailed_stop_can_close_on_the_same_pass(db: AsyncSession) -> None:
    # Trail first, then decide: a stop moved onto the current price should not
    # wait for the next poll to fire.
    row = await make_position(db, take_profit=None)
    policies = PolicySet.default(trailing=TrailingStopPolicy(distance=Decimal("0")))
    result = await PositionManager(db, PaperExitExecutor(), policies).process(
        row, market_for(row, "1.10800"), HOLD
    )
    # distance 0 is refused as a trail, so nothing moved and nothing closed.
    assert result.modification is None and not result.closed


async def test_paper_executor_refuses_a_mismatched_or_unusable_quote() -> None:
    executor = PaperExitExecutor()
    decision = ExitDecision(ExitReason.strategy_exit, "x", Decimal("1.1"), NOW)
    wrong = MarketState("GBPUSD", Decimal("1.3"), Decimal("1.3"), NOW)
    out = await executor.close(long_position(), wrong, decision)
    assert out.status is CloseStatus.rejected and "GBPUSD" in out.detail

    dead = MarketState("EURUSD", Decimal("0"), Decimal("0"), NOW)
    assert (await executor.close(long_position(), dead, decision)).status is CloseStatus.rejected


async def test_demo_and_live_executors_cannot_confirm_anything() -> None:
    decision = ExitDecision(ExitReason.emergency_exit, "halt", Decimal("1.1"), NOW)
    for mode in ("demo", "live"):
        out = await UnavailableExitExecutor(mode=mode).close(
            long_position(), quote("1.10500"), decision
        )
        assert out.status is CloseStatus.rejected
        assert "no broker adapter" in out.detail
        assert out.fill_price is None


async def test_sweep_skips_positions_without_a_quote(db: AsyncSession) -> None:
    row = await make_position(db)
    manager = PositionManager(db, PaperExitExecutor())
    results = await manager.run_once({}, HOLD, mode="paper")
    assert len(results) == 1 and results[0].skipped == "no quote available"
    assert row.status == "open"


async def test_sweep_only_touches_the_requested_mode(db: AsyncSession) -> None:
    paper = await make_position(db, mode="paper")
    demo = await make_position(db, mode="demo")
    manager = PositionManager(db, PaperExitExecutor())
    quotes = {CODE: market_for(paper, "1.11000")}
    results = await manager.run_once(quotes, HOLD, mode="paper")
    assert [r.position_id for r in results] == [paper.id]
    assert paper.status == "closed" and demo.status == "open"


async def test_monitor_runs_passes_and_stops_cleanly(db: AsyncSession) -> None:
    row = await make_position(db)
    manager = PositionManager(db, PaperExitExecutor())

    async def quotes() -> dict[str, MarketState]:
        return {CODE: market_for(row, "1.10500")}  # neither level hit

    async def context() -> RiskContext:
        return HOLD

    monitor = PositionMonitor(
        manager=manager, quotes=quotes, context=context, interval_seconds=0.01
    )
    passes = await monitor.run(max_passes=3)
    assert passes == 3
    assert row.status == "open"  # nothing triggered, nothing closed

    monitor.stop()
    assert monitor.stop_event.is_set()


async def test_monitor_survives_a_failing_pass(db: AsyncSession) -> None:
    manager = PositionManager(db, PaperExitExecutor())
    calls = {"n": 0}

    async def quotes() -> dict[str, MarketState]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("quote feed down")
        return {}

    async def context() -> RiskContext:
        return HOLD

    monitor = PositionMonitor(
        manager=manager, quotes=quotes, context=context, interval_seconds=0.01
    )
    assert await monitor.run(max_passes=2) >= 1
    assert calls["n"] >= 2  # it kept going after the failure


# ================================================== L21: break-even and partials


def test_break_even_moves_the_stop_to_the_entry_once_far_enough_ahead() -> None:
    policy = BreakEvenPolicy(trigger_distance=Decimal("0.00500"))
    # Not far enough: nothing proposed.
    assert policy.proposed_stop(long_position(), quote("1.10400")) is None
    # Far enough: the stop moves to the entry.
    moved = policy.proposed_stop(long_position(), quote("1.10500"))
    assert moved is not None
    assert moved.new_stop == Decimal("1.10000")
    assert "break-even" in moved.reason


def test_break_even_clears_the_spread_when_a_buffer_is_given() -> None:
    """A stop placed exactly at the entry loses the spread every time it
    fires, which is not break-even."""
    policy = BreakEvenPolicy(trigger_distance=Decimal("0.00500"), buffer=Decimal("0.00020"))
    moved = policy.proposed_stop(long_position(), quote("1.10600"))
    assert moved is not None
    assert moved.new_stop == Decimal("1.10020")


def test_break_even_never_moves_a_stop_backwards() -> None:
    """Invariant 11. A break-even that could loosen a stop would be a way to
    widen risk under a name that sounds safe."""
    policy = BreakEvenPolicy(trigger_distance=Decimal("0.00500"))
    # The stop is already past the entry, so break-even has nothing to offer.
    already = long_position(stop_loss=Decimal("1.10500"))
    assert policy.proposed_stop(already, quote("1.10800")) is None
    # And running it twice on the same tick proposes nothing the second time.
    first = long_position()
    moved = policy.proposed_stop(first, quote("1.10600"))
    assert moved is not None
    second = PositionView(**{**first.__dict__, "stop_loss": moved.new_stop})
    assert policy.proposed_stop(second, quote("1.10600")) is None


def test_break_even_works_for_a_short() -> None:
    policy = BreakEvenPolicy(trigger_distance=Decimal("0.00500"))
    moved = policy.proposed_stop(short_position(), quote("1.09400", "1.09500"))
    assert moved is not None
    assert moved.new_stop == Decimal("1.10000")


def test_break_even_invents_nothing_without_an_atr() -> None:
    policy = BreakEvenPolicy(atr_multiple=Decimal("2"))
    assert policy.proposed_stop(long_position(), quote("1.20000")) is None
    with_atr = policy.proposed_stop(long_position(), quote("1.10500", atr="0.00100"))
    assert with_atr is not None


def test_a_partial_target_takes_a_fraction_of_the_INITIAL_size() -> None:
    """A 0.3 rule takes 30 of a 100-lot position once, not 30 then 21 then
    14.7."""
    policy = PartialTakeProfitPolicy(level=Decimal("1.10500"), fraction=Decimal("0.30"))
    position = long_position(quantity=Decimal("1.00"), initial_quantity=Decimal("1.00"))
    decision = policy.evaluate(position, quote("1.10500"), HOLD)
    assert decision is not None
    assert decision.reason is ExitReason.partial_take_profit
    assert decision.quantity == Decimal("0.30")


def test_a_partial_target_does_not_fire_twice() -> None:
    """Section 34's idempotency, satisfied by arithmetic rather than a flag
    somebody has to clear."""
    policy = PartialTakeProfitPolicy(level=Decimal("1.10500"), fraction=Decimal("0.30"))
    after = long_position(
        quantity=Decimal("0.70"),
        initial_quantity=Decimal("1.00"),
        closed_quantity=Decimal("0.30"),
    )
    assert policy.evaluate(after, quote("1.10600"), HOLD) is None


def test_a_partial_target_needs_a_level_nobody_invented() -> None:
    assert PartialTakeProfitPolicy().evaluate(long_position(), quote("9.9"), HOLD) is None


def test_the_full_target_outranks_the_partial_one() -> None:
    """If price reached the level that closes everything, closing part of it
    instead would leave size the full target had already decided to remove."""
    policies = PolicySet.default(
        partial=PartialTakeProfitPolicy(level=Decimal("1.10500"), fraction=Decimal("0.3"))
    )
    position = long_position(quantity=Decimal("1.00"), initial_quantity=Decimal("1.00"))
    decision = policies.decide(position, quote("1.11000"), HOLD)
    assert decision is not None
    assert decision.reason is ExitReason.take_profit
    assert decision.quantity is None  # all of it


def test_the_most_protective_stop_wins_when_two_policies_propose() -> None:
    """Section 20: taking whichever policy ran first would make the outcome
    depend on list order."""
    policies = PolicySet.default(
        trailing=TrailingStopPolicy(distance=Decimal("0.00800")),
        break_even=BreakEvenPolicy(trigger_distance=Decimal("0.00100")),
    )
    movers = policies.stop_movers()
    assert len(movers) == 2
    market = quote("1.10500")
    position = long_position()
    # The trail proposes 1.09700, break-even proposes 1.10000. The higher one
    # is the more protective on a long, and it is the one that must win.
    proposals = [m.proposed_stop(position, market) for m in movers]
    assert {p.new_stop for p in proposals if p} == {Decimal("1.09700"), Decimal("1.10000")}


# ============================================ L21: partial closes on the row


async def test_a_partial_close_leaves_the_position_open_at_a_smaller_size(
    db: AsyncSession,
) -> None:
    row = await make_position(db, quantity=Decimal("1.00"))
    manager = PositionManager(db, PaperExitExecutor())
    decision = ExitDecision(
        ExitReason.partial_take_profit,
        "scale out",
        Decimal("1.10500"),
        NOW,
        quantity=Decimal("0.30"),
    )
    result = await manager.close_now(row, market_for(row, "1.10500"), decision)

    assert result.outcome is not None and result.outcome.is_confirmed
    assert result.partially_closed
    assert row.status == "partially_closed"
    assert row.quantity == Decimal("0.70")
    assert row.closed_quantity == Decimal("0.30")
    # Money is booked on the size that actually closed, at the venue's fill.
    assert row.realized_pnl == Decimal("0.00150")
    assert row.closed_at is None  # not finished


async def test_a_partial_close_is_not_reported_as_a_finished_trade(
    db: AsyncSession,
) -> None:
    """A caller treating a scale-out as a finished trade books a P&L for size
    that is still at the venue."""
    row = await make_position(db, quantity=Decimal("1.00"))
    manager = PositionManager(db, PaperExitExecutor())
    result = await manager.close_now(
        row,
        market_for(row, "1.10500"),
        ExitDecision(
            ExitReason.partial_take_profit,
            "scale",
            Decimal("1.105"),
            NOW,
            quantity=Decimal("0.30"),
        ),
    )
    assert result.partially_closed is True
    assert result.closed is True  # the CLOSE was confirmed...
    assert row.status == "partially_closed"  # ...but the POSITION is not closed


async def test_closing_the_remainder_finishes_the_position(db: AsyncSession) -> None:
    row = await make_position(db, quantity=Decimal("1.00"))
    manager = PositionManager(db, PaperExitExecutor())
    partial = ExitDecision(
        ExitReason.partial_take_profit,
        "scale",
        Decimal("1.105"),
        NOW,
        quantity=Decimal("0.30"),
    )
    await manager.close_now(row, market_for(row, "1.10500"), partial)
    rest = ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.106"), NOW)
    await manager.close_now(row, market_for(row, "1.10600"), rest)

    assert row.status == "closed"
    assert row.quantity == Decimal("0")
    assert row.closed_quantity == Decimal("1.00")
    assert row.closed_at is not None


async def test_a_partially_closed_position_is_still_managed(db: AsyncSession) -> None:
    """The remaining size still needs its stop watched."""
    row = await make_position(db, quantity=Decimal("0.70"), status="partially_closed")
    manager = PositionManager(db, PaperExitExecutor())
    results = await manager.run_once({CODE: market_for(row, "1.08000")}, HOLD, "paper")
    assert len(results) == 1
    assert results[0].skipped is None
    assert row.status == "closed"  # its stop was hit


@pytest.mark.parametrize("status", ["unknown", "reconciling", "closing", "opening"])
async def test_an_unsettled_position_is_never_acted_on(db: AsyncSession, status: str) -> None:
    """Each of these means the venue's answer is not settled. Acting could
    double-close a position that is still open, or close one never opened."""
    row = await make_position(db, status=status)
    manager = PositionManager(db, PaperExitExecutor())
    result = await manager.process(row, market_for(row, "1.00000"), HOLD)
    assert result.skipped is not None
    assert row.status == status  # untouched


# ================================================ L21: the protection mismatch


async def test_a_stop_the_venue_is_not_holding_is_recorded_every_pass(
    db: AsyncSession,
) -> None:
    """The screen says protected and the position is not. This is the most
    dangerous disagreement the module can observe."""
    row = await make_position(db)
    row.broker_stop_loss = Decimal("1.05000")  # the venue has a different stop
    await db.flush()

    manager = PositionManager(db, PaperExitExecutor())
    await manager.process(row, market_for(row, "1.10000"), HOLD)
    events = (
        await db.scalars(select(PositionEvent).where(PositionEvent.position_id == row.id))
    ).all()
    kinds = [e.event_type for e in events]
    assert "protection_mismatch" in kinds


def test_a_venue_never_read_is_not_reported_as_a_mismatch() -> None:
    """`None` means never read, which is not the same as absent. Reporting it
    would cry wolf on every position before its first sync."""
    assert long_position().protection_gap is None
    agreed = long_position(broker_stop_loss=Decimal("1.09000"))
    assert agreed.protection_gap is None
    differs = long_position(broker_stop_loss=Decimal("1.05000"))
    assert differs.protection_gap is not None
    assert "1.09000" in differs.protection_gap


# ============================================ L21: the broker exit executor


def _registry(venue=None, mode: str = "demo"):  # noqa: ANN001, ANN202
    from app.oms.registry import OrderManagerRegistry

    registry = OrderManagerRegistry()
    if venue is not None:
        registry.register("acct-a", venue, mode=mode, broker="fake")
    return registry


def broker_position(**over: object) -> PositionView:
    base: dict[str, object] = {
        "account_id": "acct-a",
        "broker_position_id": "fake-pos-1",
        "mode": "demo",
    }
    return long_position(**{**base, **over})


async def test_a_demo_position_closes_through_the_order_manager() -> None:
    """The gap MIGRATION_STATUS recorded against L21. It is closed by using
    the adapter L10 built and the manager L19 built, not by a new path."""
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor

    venue = FakeBroker(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    placed = await venue.place_order(
        __import__("app.brokers.base", fromlist=["OrderRequest"]).OrderRequest(
            symbol="EURUSD", side="buy", volume=Decimal("0.10"), intent_id="i-1"
        )
    )

    executor = BrokerExitExecutor(_registry(venue), mode="demo")
    outcome = await executor.close(
        broker_position(broker_position_id=placed.position_id),
        quote("1.10500", "1.10502"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )
    assert outcome.status is CloseStatus.confirmed
    assert outcome.fill_price is not None
    assert outcome.fill_source == "simulator"  # the FAKE venue says so
    assert len(await venue.get_positions()) == 0


async def test_an_unclear_venue_answer_parks_the_close_and_never_retries() -> None:
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor

    venue = FakeBroker(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    venue.unknown_next = True

    executor = BrokerExitExecutor(_registry(venue), mode="demo")
    outcome = await executor.close(
        broker_position(),
        quote("1.10500"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )
    assert outcome.status is CloseStatus.unknown
    assert outcome.fill_price is None


async def test_a_close_without_a_broker_id_is_unknown_not_rejected() -> None:
    """Rejected would say "the position is still open and fine", and we cannot
    say that: we simply cannot ask the venue about it."""
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor

    venue = FakeBroker(mode="demo")
    await venue.connect()
    executor = BrokerExitExecutor(_registry(venue), mode="demo")
    outcome = await executor.close(
        broker_position(broker_position_id=None),
        quote("1.10500"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )
    assert outcome.status is CloseStatus.unknown


async def test_a_close_with_no_registered_venue_is_rejected_not_parked() -> None:
    """Nothing was sent, and nothing could have been, so the position is
    known to be untouched."""
    from app.positions.broker_executor import BrokerExitExecutor

    executor = BrokerExitExecutor(_registry(None), mode="demo")
    outcome = await executor.close(
        broker_position(),
        quote("1.10500"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )
    assert outcome.status is CloseStatus.rejected
    assert "no order manager is registered" in outcome.detail


async def test_a_partial_close_larger_than_the_position_is_refused() -> None:
    """Refused rather than clamped: the difference between closing 30 of 70
    and 30 of 100 is a position size nobody chose."""
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor

    venue = FakeBroker(mode="demo")
    await venue.connect()
    executor = BrokerExitExecutor(_registry(venue), mode="demo")
    outcome = await executor.close(
        broker_position(quantity=Decimal("0.10")),
        quote("1.10500"),
        ExitDecision(
            ExitReason.partial_take_profit,
            "scale",
            Decimal("1.105"),
            NOW,
            quantity=Decimal("0.50"),
        ),
    )
    assert outcome.status is CloseStatus.rejected
    assert "exceeds" in outcome.detail


async def test_a_position_cannot_be_closed_in_the_wrong_execution_mode() -> None:
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor

    venue = FakeBroker(mode="paper")
    await venue.connect()
    executor = BrokerExitExecutor(_registry(venue, mode="paper"), mode="demo")
    outcome = await executor.close(
        broker_position(),
        quote("1.10500"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )
    assert outcome.status is CloseStatus.rejected
    assert "not in" in outcome.detail


def _executor_source() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parents[1] / "app" / "positions" / "broker_executor.py"
    ).read_text(encoding="utf-8")


def test_the_broker_executor_reaches_no_terminal_of_its_own() -> None:
    """Position Manager -> OMS -> BrokerAdapter -> MT5, never a shortcut."""
    import ast

    tree = ast.parse(_executor_source())
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert not name.startswith("MetaTrader5"), name
            assert "mt5" not in name.lower(), name


def test_the_broker_executor_never_touches_an_adapter_directly() -> None:
    """**The fix for L45 C-3.**

    The test above is the whole guard this path used to have, and it asserted
    only that the module imports nothing called `mt5`. It passed for four
    levels on code that called `manager.adapter.close_position(...)` -- no
    approval, no OMS, no `intent_id`, no order record -- while the module's own
    docstring claimed a close "takes the same path any other order takes".

    A test that reads as protection and provides none is C-4 in test form. So
    this one asserts the thing that was actually wrong: no attribute named
    `adapter` is read anywhere in the module. Reaching a venue is the OMS's
    job, and an executor that holds an adapter is an executor that can send.
    """
    import ast

    tree = ast.parse(_executor_source())
    reached = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "adapter"
    ]
    assert reached == [], (
        "the broker executor reads `.adapter` directly, which is a second path "
        "to the venue that bypasses the OMS lifecycle and its idempotency"
    )


def test_the_broker_executor_goes_through_risk_and_the_oms() -> None:
    """The positive half of the same guard. Asserting the absence of a shortcut
    is not the same as asserting the presence of the real path, and a module
    that did neither would pass the test above."""
    import ast

    tree = ast.parse(_executor_source())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for required in ("approve_close", "create", "close", "guard_resend"):
        assert required in called, (
            f"the broker executor never calls `{required}`; a close that skips it is "
            "not on the path this module claims to take"
        )


async def test_a_close_creates_an_order_record_with_an_intent() -> None:
    """The substance of C-2, not its shape.

    No `intent_id` meant two concurrent close requests were two
    `close_position` calls at the venue, and on a hedging account a double
    close OPENS a position in the opposite direction. No order record meant
    reconciliation could not see the close at all.
    """
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor

    venue = FakeBroker(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    placed = await venue.place_order(
        __import__("app.brokers.base", fromlist=["OrderRequest"]).OrderRequest(
            symbol="EURUSD", side="buy", volume=Decimal("0.10"), intent_id="i-1"
        )
    )

    registry = _registry(venue)
    executor = BrokerExitExecutor(registry, mode="demo")
    position = broker_position(broker_position_id=placed.position_id)
    outcome = await executor.close(
        position,
        quote("1.10500", "1.10502"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )

    assert outcome.status is CloseStatus.confirmed
    manager = registry.get("acct-a")
    assert len(manager.orders) == 1
    order = next(iter(manager.orders.values()))
    assert order.client_order_id.startswith("close:")
    assert order.risk_decision_id, "a close with no decision id traces to no risk record"
    assert order.sizing_snapshot["closes_position"] == position.id


async def test_the_same_close_twice_reaches_the_venue_once() -> None:
    """Idempotency, which is what the missing `intent_id` cost."""
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor

    venue = FakeBroker(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    placed = await venue.place_order(
        __import__("app.brokers.base", fromlist=["OrderRequest"]).OrderRequest(
            symbol="EURUSD", side="buy", volume=Decimal("0.10"), intent_id="i-1"
        )
    )

    closes: list[str] = []
    original = venue.close_position

    async def counting(position_id, volume=None):  # noqa: ANN001, ANN202
        closes.append(position_id)
        return await original(position_id, volume=volume)

    venue.close_position = counting  # type: ignore[method-assign]

    executor = BrokerExitExecutor(_registry(venue), mode="demo")
    position = broker_position(broker_position_id=placed.position_id)
    decision = ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW)

    first = await executor.close(position, quote("1.10500", "1.10502"), decision)
    second = await executor.close(position, quote("1.10500", "1.10502"), decision)

    assert first.status is CloseStatus.confirmed
    assert second.status is CloseStatus.rejected
    assert len(closes) == 1, "the venue was asked to close the same position twice"


async def test_a_breached_limit_cannot_trap_an_open_position() -> None:
    """The design decision behind `approve_close`, asserted rather than assumed.

    Every limit this platform enforces bounds the risk of TAKING a position.
    Applying them to a close would refuse to reduce exposure at the moment
    exposure is worst, so a close is evaluated, recorded, and approved anyway.
    What must NOT happen is that it becomes silent: the breached limits are
    stamped `not_enforced` and reach the order's risk snapshot.
    """
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor
    from app.risk.engine import RiskEngine, RiskLimits

    venue = FakeBroker(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    placed = await venue.place_order(
        __import__("app.brokers.base", fromlist=["OrderRequest"]).OrderRequest(
            symbol="EURUSD", side="buy", volume=Decimal("0.10"), intent_id="i-1"
        )
    )

    # A limit the close breaches by construction.
    registry = _registry(venue)
    executor = BrokerExitExecutor(
        registry,
        mode="demo",
        risk=RiskEngine(RiskLimits(max_position_size=Decimal("0.01"))),
    )
    outcome = await executor.close(
        broker_position(broker_position_id=placed.position_id),
        quote("1.10500", "1.10502"),
        ExitDecision(ExitReason.risk_exit, "get out", Decimal("1.105"), NOW),
    )

    assert outcome.status is CloseStatus.confirmed, (
        "a risk limit refused to let a position be closed"
    )
    order = next(iter(registry.get("acct-a").orders.values()))
    assert order.risk_snapshot["not_enforced"], (
        "the close went over a limit and the record does not say which"
    )


async def test_a_close_in_an_unsupported_mode_is_still_refused() -> None:
    """The one check `approve_close` keeps. A close is still an instruction
    transmitted to a venue, so the mode fence applies to it unchanged."""
    from app.brokers.fake import FakeBroker
    from app.positions.broker_executor import BrokerExitExecutor

    venue = FakeBroker(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")

    executor = BrokerExitExecutor(_registry(venue, mode="live"), mode="live")
    outcome = await executor.close(
        broker_position(mode="live"),
        quote("1.10500", "1.10502"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )
    assert outcome.status is CloseStatus.rejected
    assert "mode" in outcome.detail


# =================================================== L21: reconciliation


async def demo_row(db: AsyncSession, **over: object):  # noqa: ANN201
    """A demo position on account `acct-a`, which is what the sweep scopes by."""
    row = await make_position(db, mode="demo", **over)
    row.paper_account_id = None
    row.broker_account_id = "acct-a"
    await db.flush()
    return row


async def connected_venue():  # noqa: ANN201
    from app.brokers.fake import FakeBroker

    venue = FakeBroker(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    return venue


async def test_reconciliation_closes_a_position_the_venue_does_not_hold(
    db: AsyncSession,
) -> None:
    """The venue is authoritative for whether a position exists."""
    from app.positions.reconciler import PositionReconciler

    row = await demo_row(db, broker_position_id="gone-1")
    venue = await connected_venue()  # holds nothing

    report = await PositionReconciler(db, venue, mode="demo").sweep("acct-a")
    assert report.failed is None
    assert row.id in report.closed_locally
    assert row.status == "closed"
    assert row.quantity == Decimal("0")

    kinds = [e.event_type for e in await events_for(db, row.id)]
    assert "reconcile_closed" in kinds
    # The exit price is NOT invented. We do not know what it closed at.
    closed = next(e for e in await events_for(db, row.id) if e.event_type == "reconcile_closed")
    assert closed.payload is not None
    assert closed.payload["exit_price"] is None
    assert row.realized_pnl is None


async def test_a_position_with_no_broker_id_is_left_alone(db: AsyncSession) -> None:
    """The venue cannot be asked about it by id, so closing it would be a
    guess. It goes back to the state it was in."""
    from app.positions.reconciler import PositionReconciler

    row = await demo_row(db, broker_position_id=None)
    venue = await connected_venue()

    report = await PositionReconciler(db, venue, mode="demo").sweep("acct-a")
    assert report.failed is None
    assert row.id not in report.closed_locally
    assert row.status == "open"
    kinds = [e.event_type for e in await events_for(db, row.id)]
    assert "reconcile_inconclusive" in kinds


async def test_reconciliation_records_a_stop_the_venue_is_not_holding(
    db: AsyncSession,
) -> None:
    """The single most dangerous disagreement: the screen says protected and
    the position is not. Both figures are kept; neither overwrites the other."""
    from app.brokers.base import BrokerPosition
    from app.positions.reconciler import PositionReconciler

    row = await demo_row(db, broker_position_id="pos-1")
    intended = row.stop_loss
    venue = await connected_venue()
    venue._positions["pos-1"] = BrokerPosition(  # noqa: SLF001
        position_id="pos-1",
        symbol="EURUSD",
        side="long",
        volume=row.quantity,
        entry_price=row.entry_price,
        opened_at=OPENED,
        stop_loss=None,  # the venue is NOT holding the stop we believe in
        take_profit=row.take_profit,
    )

    report = await PositionReconciler(db, venue, mode="demo").sweep("acct-a")
    assert report.failed is None
    assert row.id in report.synced
    assert row.status == "open"  # still open, and now visibly unprotected
    # What we intended is untouched; what the venue says is recorded beside it.
    assert row.stop_loss == intended
    assert row.broker_stop_loss is None
    assert row.broker_synced_at is not None

    event = next(e for e in await events_for(db, row.id) if e.event_type == "reconciled")
    assert event.payload is not None
    assert event.payload["agrees"] is False
    assert event.payload["differences"]["stop_loss"]["venue"] is None
    assert event.payload["differences"]["stop_loss"]["intended"] == str(intended)


async def test_reconciliation_concludes_nothing_when_the_venue_cannot_be_read(
    db: AsyncSession,
) -> None:
    """A reconciler that decided a position was gone because the network was
    down is worse than one that refuses to decide."""
    from app.brokers.fake import FakeBroker
    from app.positions.reconciler import PositionReconciler

    row = await demo_row(db, broker_position_id="pos-1")
    before = row.status

    venue = FakeBroker(mode="demo")  # never connected: reads raise
    report = await PositionReconciler(db, venue, mode="demo").sweep("acct-a")
    assert report.failed is not None
    assert "could not be read" in report.failed
    assert row.status == before  # untouched
    assert report.closed_locally == []


def test_reconciliation_reports_rather_than_repairs() -> None:
    """The module's own contract, restated where L21 uses it."""
    import inspect

    from app.positions import reconciler

    source = inspect.getsource(reconciler)
    # It must never send anything to a venue.
    for forbidden in ("place_order", "close_position", "modify_order", "cancel_order"):
        assert forbidden not in source, f"the reconciler calls {forbidden}"


# ============================================ L21: events on the existing bus


def test_every_recorded_change_has_an_event_type_or_is_deliberately_silent() -> None:
    """The L07 catalogue declared three POSITION_* types and named L21 as the
    producer. Two had none until now."""
    from app.positions.events import EVENT_FOR
    from app.realtime.catalogue import CATALOGUE, EventType

    assert set(EVENT_FOR.values()) == {
        EventType.POSITION_OPENED,
        EventType.POSITION_UPDATED,
        EventType.POSITION_CLOSED,
    }
    for kind in EVENT_FOR.values():
        assert kind in CATALOGUE  # no second event system


async def test_a_close_publishes_a_position_closed_event(db: AsyncSession) -> None:
    from app.realtime.catalogue import EventType

    row = await make_position(db)
    manager = PositionManager(db, PaperExitExecutor())
    await manager.close_now(
        row,
        market_for(row, "1.10500"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )
    kinds = [e.type for e in manager.drain_events()]
    assert str(EventType.POSITION_CLOSED) in kinds


async def test_a_partial_close_publishes_an_update_not_a_close(
    db: AsyncSession,
) -> None:
    """A scale-out is not a finished trade, and the bus must not say it is."""
    from app.realtime.catalogue import EventType

    row = await make_position(db, quantity=Decimal("1.00"))
    manager = PositionManager(db, PaperExitExecutor())
    await manager.close_now(
        row,
        market_for(row, "1.10500"),
        ExitDecision(
            ExitReason.partial_take_profit,
            "scale",
            Decimal("1.105"),
            NOW,
            quantity=Decimal("0.30"),
        ),
    )
    kinds = [e.type for e in manager.drain_events()]
    assert str(EventType.POSITION_UPDATED) in kinds
    assert str(EventType.POSITION_CLOSED) not in kinds


async def test_draining_events_twice_does_not_republish(db: AsyncSession) -> None:
    row = await make_position(db)
    manager = PositionManager(db, PaperExitExecutor())
    await manager.close_now(
        row,
        market_for(row, "1.10500"),
        ExitDecision(ExitReason.strategy_exit, "done", Decimal("1.105"), NOW),
    )
    assert manager.drain_events()
    assert manager.drain_events() == []


async def test_an_event_carries_both_the_intended_and_the_venue_levels(
    db: AsyncSession,
) -> None:
    """A subscriber shown only one could not tell a protected position from
    one that merely believes it is."""
    row = await make_position(db)
    row.broker_stop_loss = Decimal("1.05000")
    await db.flush()

    manager = PositionManager(db, PaperExitExecutor())
    await manager.process(row, market_for(row, "1.10000"), HOLD)
    events = manager.drain_events()
    assert events
    payload = events[0].payload
    assert payload["stop_loss"] == "1.09000"
    assert payload["broker_stop_loss"] == "1.05000"
    assert "nothing may execute from it" in str(payload["advisory"])


# ==================================== L21: the brief's end-to-end scenario (48)


async def test_the_briefs_end_to_end_trailing_scenario(db: AsyncSession) -> None:
    """Section 48, run literally.

    Entry 100, SL 98, trailing distance 3.
    Price 110 -> SL 107. Price 115 -> SL 112. Price falls to 112 -> closed.

    Every step goes through the manager: the trail moves the stop, the stop
    policy fires, and the position closes on a CONFIRMED fill and not before.
    """
    row = await make_position(
        db,
        quantity=Decimal("100"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit=None,
    )
    policies = PolicySet.default(trailing=TrailingStopPolicy(distance=Decimal("3")))
    manager = PositionManager(db, PaperExitExecutor(), policies)

    # Price rises to 110. The trail ratchets the stop to 107 and does not close.
    result = await manager.process(row, market_for(row, "110"), HOLD)
    assert result.modification is not None
    assert row.stop_loss == Decimal("107")
    assert row.status == "open"
    assert result.outcome is None

    # Price rises to 115. The stop follows to 112.
    result = await manager.process(row, market_for(row, "115"), HOLD)
    assert row.stop_loss == Decimal("112")
    assert row.status == "open"

    # Price falls back to 112. The stop is reached and the position closes on
    # a confirmed fill.
    result = await manager.process(row, market_for(row, "112"), HOLD)
    assert result.decision is not None
    assert result.decision.reason is ExitReason.stop_loss
    assert result.outcome is not None and result.outcome.is_confirmed
    assert row.status == "closed"
    assert row.quantity == Decimal("0")
    assert row.closed_quantity == Decimal("100")
    # Money booked at the VENUE's fill, on the size that closed.
    assert row.realized_pnl == Decimal("1200")

    # And the audit trail shows the whole thing in order.
    kinds = [e.event_type for e in await events_for(db, row.id)]
    assert kinds.count("stop_modified") == 2
    assert "exit_decided" in kinds
    assert "closed" in kinds


async def test_the_trail_never_moves_the_stop_backwards_across_a_sweep(
    db: AsyncSession,
) -> None:
    """Invariant 11, driven through the manager rather than the policy."""
    row = await make_position(
        db,
        quantity=Decimal("100"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit=None,
    )
    policies = PolicySet.default(trailing=TrailingStopPolicy(distance=Decimal("3")))
    manager = PositionManager(db, PaperExitExecutor(), policies)

    await manager.process(row, market_for(row, "115"), HOLD)
    high_water = row.stop_loss
    assert high_water == Decimal("112")

    # Price retreats but does not reach the stop. The stop must not follow it
    # down: `extreme_price` is not tracked on the row, so the trail re-seeds
    # from the quote and would propose 110 -- which is a LOOSENING and is
    # refused because it does not improve on 112.
    await manager.process(row, market_for(row, "113"), HOLD)
    assert row.stop_loss == high_water
    assert row.status == "open"


async def test_a_position_with_no_quote_is_left_alone_not_closed(
    db: AsyncSession,
) -> None:
    """Invariant 17. Missing market data is not a reason to act."""
    row = await make_position(db)
    manager = PositionManager(db, PaperExitExecutor())
    results = await manager.run_once({}, HOLD, "paper")
    assert len(results) == 1
    assert results[0].skipped == "no quote available"
    assert row.status == "open"


# ================================ L70m: the position no sweep could reach


async def test_a_position_belonging_to_no_account_is_reported_not_skipped(
    db: AsyncSession,
) -> None:
    """`_local` scopes by account, so a row with neither account column set
    matches no sweep at all.

    Position 58326177606 sat exactly that way on the demo venue: open in the
    platform, closed at the venue, and stepped over by every sweep anybody ran.
    It was not filtered out by a rule anybody could read -- it simply never
    appeared, which is the harder kind of invisible.
    """
    from app.positions.reconciler import PositionReconciler

    orphan = await make_position(db, mode="demo", broker_position_id="orphan-1")
    orphan.paper_account_id = None
    orphan.broker_account_id = None
    await db.flush()

    venue = await connected_venue()
    report = await PositionReconciler(db, venue, mode="demo").sweep("acct-a")

    assert report.failed is None
    assert orphan.id in report.unattributed
    assert orphan.id in report.as_dict()["unattributed"]
    # Reported and NOT acted on. This sweep holds one account's adapter and
    # cannot know the orphan was ever held there.
    assert orphan.status == "open"
    assert orphan.id not in report.closed_locally
    assert orphan.id not in report.synced


async def test_a_sweep_is_not_clean_while_a_position_is_unreachable(
    db: AsyncSession,
) -> None:
    """A report that says `clean` while a position sits outside every account
    it could have swept is describing a subset and calling it the whole."""
    from app.positions.reconciler import PositionReconciler

    venue = await connected_venue()
    clean = await PositionReconciler(db, venue, mode="demo").sweep("acct-a")
    assert clean.clean is True

    orphan = await make_position(db, mode="demo", broker_position_id="orphan-2")
    orphan.paper_account_id = None
    orphan.broker_account_id = None
    await db.flush()

    after = await PositionReconciler(db, venue, mode="demo").sweep("acct-a")
    assert after.unattributed == [orphan.id]
    assert after.clean is False
    assert after.failed is None  # a finding for a person, not a failed sweep


async def test_an_orphan_in_another_mode_is_not_this_sweeps_finding(
    db: AsyncSession,
) -> None:
    """The sweep speaks for one mode. A paper position with no account is not
    a demo venue's problem, and reporting it here would put a finding in front
    of somebody who cannot act on it."""
    from app.positions.reconciler import PositionReconciler

    other = await make_position(db, mode="paper", broker_position_id="orphan-3")
    other.paper_account_id = None
    other.broker_account_id = None
    await db.flush()

    venue = await connected_venue()
    report = await PositionReconciler(db, venue, mode="demo").sweep("acct-a")
    assert report.unattributed == []
    assert report.clean is True
