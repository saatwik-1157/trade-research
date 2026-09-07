"""`positions.realized_pnl` is what the account received, not a price difference.

`test_trade_journal.py` has stated that intent since L19 -- *"positions.realized_pnl
is what the account actually received"* -- and until 2026-09-07 the booking code
did not honour it for a broker close:

    booked = (fill - entry) * direction * quantity

That omits the contract size. Measured against a live MT5 demo close: 0.01 lots
of EURUSD from 1.16319 to 1.16315 booked -0.0000004 where the account received
-0.04, and `NUMERIC(18,4)` stored it as **0.0000**. Every reader of the column
inherited it.

There is no single multiplier that repairs it -- contract size alone fixes
EURUSD and leaves USDJPY, XAUUSD and DE40 wrong, because the profit currency is
not always the account currency. So the figure comes from the venue, which has
already done the conversion, and a broker close whose money the venue did not
report books **nothing** rather than a plausible wrong number.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from app.db.base import Base
from app.models.execution import Position
from app.models.market import Symbol
from app.positions.executor import CloseOutcome, CloseStatus
from app.positions.manager import PositionManager
from app.positions.policies import ExitDecision, ExitReason, MarketState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
SYMBOL_ID = "sym-eurusd"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    import app.auth.models  # noqa: F401 - register the auth tables
    import app.models  # noqa: F401 - register every platform table

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Symbol(
                id=SYMBOL_ID,
                code="EURUSD",
                asset_class="fx",
                unit_class="points",
                is_active=True,
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


async def _position(db: AsyncSession, *, mode: str) -> Position:
    """The position from the real run: 0.01 EURUSD long at 1.16319."""
    row = Position(
        id=f"pos-{mode}",
        mode=mode,
        symbol_id=SYMBOL_ID,
        broker_position_id="58328827918",
        side="long",
        quantity=Decimal("0.01"),
        initial_quantity=Decimal("0.01"),
        closed_quantity=Decimal("0"),
        entry_price=Decimal("1.16319"),
        status="open",
        opened_at=NOW.replace(tzinfo=None),
        source="manual",
    )
    db.add(row)
    await db.commit()
    return row


class StubExecutor:
    """Closes with whatever the venue is pretending to have said.

    Satisfies the `ExitExecutor` protocol, `mode` included -- a stub missing a
    protocol member is a stub the type checker cannot vouch for, and the point
    of this file is what the real manager does with a real outcome.
    """

    fill_source = "broker"

    def __init__(self, realized_pnl: Decimal | None, mode: str = "demo") -> None:
        self._realized = realized_pnl
        self.mode = mode

    async def close(self, position, market, decision):  # noqa: ANN001, ANN202
        return CloseOutcome(
            CloseStatus.confirmed,
            "closed at the venue",
            fill_price=Decimal("1.16315"),
            closed_at=NOW,
            broker_deal_id="58328827918",
            fill_source="broker",
            realized_pnl=self._realized,
        )


def _decision() -> ExitDecision:
    return ExitDecision(ExitReason.stop_loss, "close it", Decimal("1.16315"), NOW)


def _market() -> MarketState:
    return MarketState(symbol="EURUSD", bid=Decimal("1.16315"), ask=Decimal("1.16316"), as_of=NOW)


# ------------------------------------------------------- the venue's figure


async def test_a_demo_close_books_what_the_venue_reported(db: AsyncSession) -> None:
    """The measured case: the account received -0.04, so the row says -0.04."""
    row = await _position(db, mode="demo")
    manager = PositionManager(db, StubExecutor(Decimal("-0.04")))

    result = await manager.close_now(row, _market(), _decision())
    assert result.outcome is not None and result.outcome.is_confirmed
    assert row.status == "closed"
    assert row.realized_pnl == Decimal("-0.04")

    # And emphatically NOT the price difference, which is what the arithmetic
    # this replaced would have produced.
    price_difference = (Decimal("1.16315") - Decimal("1.16319")) * Decimal("0.01")
    assert price_difference == Decimal("-0.0000004")
    assert row.realized_pnl != price_difference


async def test_a_profit_is_booked_as_reported_too(db: AsyncSession) -> None:
    row = await _position(db, mode="demo")
    manager = PositionManager(db, StubExecutor(Decimal("0.33")))
    await manager.close_now(row, _market(), _decision())
    assert row.realized_pnl == Decimal("0.33")


async def test_the_event_says_the_figure_came_from_the_venue(db: AsyncSession) -> None:
    from app.models.execution import PositionEvent
    from sqlalchemy import select

    row = await _position(db, mode="demo")
    manager = PositionManager(db, StubExecutor(Decimal("-0.04")))
    await manager.close_now(row, _market(), _decision())

    events = list(await db.scalars(select(PositionEvent)))
    sources = [e.payload.get("realized_pnl_source") for e in events if e.payload]
    assert "venue" in sources


# --------------------------------------------------------------- the gap


async def test_a_broker_close_with_no_venue_figure_books_nothing(db: AsyncSession) -> None:
    """**The rule that matters.**

    Reporting nothing is recoverable -- reconciliation can read the deal
    history. Booking the price difference would put a wrong number in the
    column every downstream reader trusts, and it would look like a real one.
    """
    row = await _position(db, mode="demo")
    manager = PositionManager(db, StubExecutor(None))

    result = await manager.close_now(row, _market(), _decision())
    assert result.outcome is not None and result.outcome.is_confirmed
    # The close still happened: quantity and status are settled from the fill.
    assert row.status == "closed"
    assert row.closed_quantity == Decimal("0.01")
    # The money is a NAMED GAP, not a zero and not a derivation.
    assert row.realized_pnl is None


async def test_the_event_names_the_gap(db: AsyncSession) -> None:
    from app.models.execution import PositionEvent
    from sqlalchemy import select

    row = await _position(db, mode="demo")
    manager = PositionManager(db, StubExecutor(None))
    await manager.close_now(row, _market(), _decision())

    events = list(await db.scalars(select(PositionEvent)))
    sources = [e.payload.get("realized_pnl_source") for e in events if e.payload]
    assert "not reported" in sources


@pytest.mark.parametrize("mode", ["demo", "live"])
async def test_no_broker_mode_derives_the_figure(db: AsyncSession, mode: str) -> None:
    row = await _position(db, mode=mode)
    manager = PositionManager(db, StubExecutor(None))
    await manager.close_now(row, _market(), _decision())
    assert row.realized_pnl is None, f"{mode} derived a money figure it was not given"


# ------------------------------------------------------------- paper is not


async def test_paper_still_books_its_own_arithmetic(db: AsyncSession) -> None:
    """The simulator is its own venue.

    Its prices and its accounting are in the same units by construction, so the
    figure it derives is self-consistent. Changing that would rewrite results
    this repository has already recorded, for no gain -- the units problem is a
    property of a REAL venue's contract sizes.
    """
    row = await _position(db, mode="paper")
    manager = PositionManager(db, StubExecutor(None))
    await manager.close_now(row, _market(), _decision())

    expected = (Decimal("1.16315") - Decimal("1.16319")) * Decimal("0.01")
    assert row.realized_pnl == expected


async def test_a_venue_figure_wins_even_in_paper(db: AsyncSession) -> None:
    """If something ever does report one, it is preferred over the derivation."""
    row = await _position(db, mode="paper")
    manager = PositionManager(db, StubExecutor(Decimal("-0.04")))
    await manager.close_now(row, _market(), _decision())
    assert row.realized_pnl == Decimal("-0.04")


# ------------------------------------------------------- the carrying chain


def test_every_link_in_the_chain_can_carry_the_figure() -> None:
    """A field that exists at one end and not the other silently drops it.

    The money travels venue -> OrderResult -> FillRecord -> CloseOutcome ->
    positions.realized_pnl, and a gap anywhere in that chain would look exactly
    like a venue that did not report.
    """
    from app.brokers.base import OrderResult
    from app.oms.fills import FillRecord

    for cls in (OrderResult, FillRecord, CloseOutcome):
        assert "realized_pnl" in cls.__dataclass_fields__, cls.__name__


# ------------------------------------------------- the close order is durable


async def test_the_close_order_carries_the_tradable_code_not_the_row_id() -> None:
    """The close order's durability depended on a symbol the store could resolve.

    `BrokerExitExecutor` built its `OrderProposal` from `PositionView.symbol`,
    which `view_of` fills with the row's `symbol_id` -- a UUID. The order store
    resolves a CODE, so every close order failed to record with

        OrderNotRecorded: symbol '4097df82-...' does not resolve.

    and `_record` logs that and proceeds, correctly: a close that already
    happened must not be refused because storage blinked. The cost was that the
    durability the close path was designed to have never once worked, silently,
    and it took a real venue to notice.

    Asserted at the call site, because the failure was invisible everywhere
    else -- the test suite's hand-built views put a CODE in `symbol`, so the
    suite agreed with itself while production disagreed.
    """
    import inspect

    from app.positions import broker_executor

    source = inspect.getsource(broker_executor.BrokerExitExecutor.close)
    # `position.symbol` IS the tradable code now -- one meaning, application
    # and tests alike. What must never come back is the row's symbol_id.
    assert "symbol=position.symbol," in source
    assert "symbol=position.symbol_id" not in source


async def test_a_position_with_no_resolvable_code_is_refused_before_the_venue() -> None:
    """No code means the record is guaranteed to fail, so nothing is sent.

    Sending a close whose record cannot be written is how a venue ends up
    holding something the database has never heard of -- the state the L45 C-1
    reasoning exists to prevent.
    """
    from app.oms.registry import OrderManagerRegistry
    from app.positions.broker_executor import BrokerExitExecutor
    from app.positions.policies import PositionView

    view = PositionView(
        id="p-nocode",
        # Empty: `view_of` leaves it so when a row's symbol does not resolve.
        symbol="",
        side="long",
        quantity=Decimal("0.01"),
        entry_price=Decimal("1.16319"),
        opened_at=NOW.replace(tzinfo=None),
        mode="demo",
        account_id="acct-1",
        broker_position_id="58328827918",
    )
    # A registered venue, so the earlier "no order manager" guard passes and the
    # symbol-code guard is the one under test.
    from app.brokers.fake import FakeBroker

    venue = FakeBroker(mode="demo")
    await venue.connect()
    managers = OrderManagerRegistry()
    managers.register("acct-1", venue, mode="demo", broker="simulator")
    executor = BrokerExitExecutor(managers, mode="demo")
    outcome = await executor.close(view, _market(), _decision())
    assert outcome.status is CloseStatus.rejected
    assert "no resolved symbol code" in outcome.detail
    assert outcome.fill_source == "none"
