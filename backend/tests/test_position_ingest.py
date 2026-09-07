"""The `positions` row a broker fill implies.

The gap this closes was found by running the platform against a real terminal on
2026-09-07: an order filled, the order row was written, and nothing recorded a
position. `PositionManager` could not manage it, `/v1/positions/{id}/close`
could not close it, and reconciliation reported it as unexpected at the broker
forever.

Every test here is about what is NOT written as much as what is: a position
written from an unconfirmed fill is a belief with nothing behind it, and that is
worse than no row at all.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.db.base import Base
from app.models.execution import Position
from app.models.market import Symbol
from app.positions.ingest import BROKER_MODES, open_internal_positions, record_fill
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

SYMBOL_ID = "sym-eurusd"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Foreign keys ON, so a row that names nothing real cannot pass."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001, ANN202
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

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


def _order(**over: object) -> SimpleNamespace:
    """A filled ManagedOrder, shaped by what `record_fill` actually reads."""
    base = {
        "mode": "demo",
        "status": "filled",
        "side": "buy",
        "filled_quantity": Decimal("0.01"),
        "average_fill_price": Decimal("1.16104"),
        "broker_order_id": "58325115749",
        "stop_loss": Decimal("1.15904"),
        "take_profit": Decimal("1.16304"),
        "filled_at": datetime(2026, 9, 7, 13, 22, 31),
        "strategy_id": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


# --------------------------------------------------------------- it writes


async def test_a_confirmed_demo_fill_becomes_a_position(db: AsyncSession) -> None:
    """The whole point: the platform now knows what it opened."""
    row = await record_fill(db, _order(), symbol_id=SYMBOL_ID)
    await db.commit()

    assert row is not None
    stored = await db.scalar(select(Position))
    assert stored is not None
    assert stored.mode == "demo"
    assert stored.broker_position_id == "58325115749"
    assert stored.side == "long"  # orders say buy/sell, positions say long/short
    assert stored.quantity == Decimal("0.01")
    assert stored.initial_quantity == Decimal("0.01")
    assert stored.closed_quantity == Decimal("0")
    assert stored.entry_price == Decimal("1.16104")
    assert stored.stop_loss == Decimal("1.15904")
    assert stored.take_profit == Decimal("1.16304")
    assert stored.status == "open"
    # No account id: the platform has no `broker_accounts` row to point at, and
    # this column is a foreign key to that table.
    assert stored.broker_account_id is None
    assert stored.strategy_version_id is None


async def test_a_sell_becomes_a_short(db: AsyncSession) -> None:
    await record_fill(db, _order(side="sell"), symbol_id=SYMBOL_ID)
    await db.commit()
    stored = await db.scalar(select(Position))
    assert stored is not None
    assert stored.side == "short"


async def test_the_platforms_levels_are_recorded_not_the_venues(db: AsyncSession) -> None:
    """`stop_loss` is what we asked for; `broker_stop_loss` is what the venue holds.

    Keeping them apart is what lets "the venue moved our stop" be something the
    platform can notice. Writing the venue's value into both would erase it.
    """
    await record_fill(db, _order(), symbol_id=SYMBOL_ID)
    await db.commit()
    stored = await db.scalar(select(Position))
    assert stored is not None
    assert stored.stop_loss == Decimal("1.15904")
    assert stored.broker_stop_loss is None
    assert stored.broker_take_profit is None


# ------------------------------------------------------------ it refuses


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"mode": "paper"}, "paper writes its own rows; doubling them doubles exposure"),
        ({"status": "unknown"}, "an unknown outcome is not a fill"),
        ({"status": "submitting"}, "nothing has been confirmed yet"),
        ({"status": "rejected"}, "the venue refused it"),
        ({"status": "intent"}, "not sent"),
        ({"average_fill_price": None}, "accepted with no fill price is not a confirmation"),
        ({"filled_quantity": Decimal("0")}, "nothing was filled"),
        ({"broker_order_id": None}, "no identifier means it could never be matched"),
        ({"broker_order_id": ""}, "same, empty"),
        ({"side": "sideways"}, "not a side this platform knows"),
    ],
)
async def test_nothing_is_written_without_a_confirmed_fill(
    db: AsyncSession, override: dict, why: str
) -> None:
    result = await record_fill(db, _order(**override), symbol_id=SYMBOL_ID)
    await db.commit()
    assert result is None, why
    assert await db.scalar(select(Position)) is None, why


async def test_paper_is_left_entirely_alone(db: AsyncSession) -> None:
    """Rule 2, asserted on the constant rather than only on behaviour."""
    assert "paper" not in BROKER_MODES
    assert BROKER_MODES == frozenset({"demo", "live"})


# --------------------------------------------------------- it is idempotent


async def test_the_same_fill_twice_is_one_position(db: AsyncSession) -> None:
    """The OMS persists an order more than once by design -- before the send and
    again after. A fill seen twice must not become two positions."""
    await record_fill(db, _order(), symbol_id=SYMBOL_ID)
    await db.commit()
    await record_fill(db, _order(), symbol_id=SYMBOL_ID)
    await db.commit()

    rows = list(await db.scalars(select(Position)))
    assert len(rows) == 1


async def test_a_larger_second_fill_grows_the_position(db: AsyncSession) -> None:
    """A partial fill topped up later. `initial_quantity` follows the maximum
    ever open, because `closed_quantity <= initial_quantity` is a database
    constraint and a shrinking initial would make a legal close illegal."""
    await record_fill(db, _order(filled_quantity=Decimal("0.01")), symbol_id=SYMBOL_ID)
    await db.commit()
    await record_fill(
        db,
        _order(filled_quantity=Decimal("0.03"), average_fill_price=Decimal("1.16110")),
        symbol_id=SYMBOL_ID,
    )
    await db.commit()

    stored = await db.scalar(select(Position))
    assert stored is not None
    assert stored.quantity == Decimal("0.03")
    assert stored.initial_quantity == Decimal("0.03")
    assert stored.entry_price == Decimal("1.16110")


async def test_a_partial_close_does_not_shrink_initial_quantity(db: AsyncSession) -> None:
    await record_fill(db, _order(filled_quantity=Decimal("0.03")), symbol_id=SYMBOL_ID)
    await db.commit()
    await record_fill(db, _order(filled_quantity=Decimal("0.01")), symbol_id=SYMBOL_ID)
    await db.commit()

    stored = await db.scalar(select(Position))
    assert stored is not None
    assert stored.quantity == Decimal("0.01")
    assert stored.initial_quantity == Decimal("0.03"), "initial records what OPENED"


async def test_two_different_tickets_are_two_positions(db: AsyncSession) -> None:
    await record_fill(db, _order(broker_order_id="111"), symbol_id=SYMBOL_ID)
    await record_fill(db, _order(broker_order_id="222"), symbol_id=SYMBOL_ID)
    await db.commit()
    assert len(list(await db.scalars(select(Position)))) == 2


async def test_the_same_ticket_in_a_different_mode_is_a_different_position(
    db: AsyncSession,
) -> None:
    """Ticket numbers are unique per venue, not across them."""
    await record_fill(db, _order(mode="demo"), symbol_id=SYMBOL_ID)
    await record_fill(db, _order(mode="live"), symbol_id=SYMBOL_ID)
    await db.commit()
    assert len(list(await db.scalars(select(Position)))) == 2


# ------------------------------------------------- what reconciliation reads


async def test_open_positions_are_returned_for_their_mode_only(db: AsyncSession) -> None:
    await record_fill(db, _order(mode="demo", broker_order_id="1"), symbol_id=SYMBOL_ID)
    await record_fill(db, _order(mode="live", broker_order_id="2"), symbol_id=SYMBOL_ID)
    await db.commit()

    demo = await open_internal_positions(db, mode="demo")
    assert [p.broker_position_id for p in demo] == ["1"]


async def test_a_closed_position_is_not_offered_for_reconciliation(db: AsyncSession) -> None:
    """The venue will not hold it, and reporting it would be a permanent
    `missing_at_broker` that trains an operator to ignore the finding."""
    await record_fill(db, _order(), symbol_id=SYMBOL_ID)
    await db.commit()
    row = await db.scalar(select(Position))
    assert row is not None
    row.status = "closed"
    await db.commit()

    assert await open_internal_positions(db, mode="demo") == []


# ---------------------------------------------------------------- contained


def test_the_ingest_module_cannot_reach_a_venue() -> None:
    """It records a fill that already happened. It must not be able to cause one."""
    path = pathlib.Path(__file__).resolve().parents[1] / "app" / "positions" / "ingest.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = ("app.brokers", "app.oms", "app.risk", "MetaTrader5", "mt5_paper")
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert not any(name == bad or name.startswith(bad + ".") for bad in forbidden), (
                f"ingest.py imports {name}"
            )


async def test_an_account_id_that_names_no_row_is_refused_by_the_database(
    db: AsyncSession,
) -> None:
    """The reason the route passes None.

    `broker_account_id` is a foreign key to `broker_accounts`, and the value an
    operator uses to register a venue is an in-process registry key, not a row
    in that table. Passing it would fail on PostgreSQL and pass on SQLite -- the
    same shape as the `orders.signal_id` defect this run found.
    """
    await record_fill(db, _order(), symbol_id=SYMBOL_ID, broker_account_id="mt5-demo-505")
    with pytest.raises(Exception) as exc:
        await db.commit()
    assert "FOREIGN KEY" in str(exc.value).upper()
    await db.rollback()


async def test_the_manual_route_never_writes_the_registry_key_as_an_account_id() -> None:
    """Asserted at the call site, where the mistake would be made again.

    `body.account_id` is the label an operator chose when registering a venue.
    `positions.broker_account_id` is a foreign key to `broker_accounts`. The
    route must LOOK UP the row rather than pass the label, and the difference
    is invisible on SQLite and fatal on PostgreSQL.
    """
    import inspect

    from app.api.v1 import orders

    source = inspect.getsource(orders.submit_order)
    assert "broker_account_id=body.account_id" not in source, (
        "the registry key is not a broker_accounts id"
    )
    assert "broker_account_for(" in source, "the row has to be resolved, not assumed"
    assert "venue_account.id if venue_account else None" in source
