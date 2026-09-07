"""Foreign keys the API test suite cannot see, because SQLite does not enforce them.

SQLite has foreign keys **off** unless `PRAGMA foreign_keys=ON` is issued per
connection, and nothing in this suite issues it. PostgreSQL enforces them
always. So a route can write a dangling reference, pass every test, and fail
the first time it runs against the real database — which is exactly what
`POST /v1/orders` did on 2026-09-07:

    ForeignKeyViolationError: insert or update on table "orders" violates
    constraint "fk_orders_signal_id_signals"
    DETAIL: Key (signal_id)=(<the idempotency key>) is not present in "signals".

These tests run the same connection with foreign keys turned ON, so the
constraint is enforced here too.

**This file covers one column.** The general gap is not closed: every other
foreign key in the schema is still unenforced in the rest of the suite. Turning
the pragma on globally is the real fix and it is a separate, wider change —
see `KNOWN_TEST_LIMITATIONS.md`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from app.db.base import Base
from app.models.execution import Order
from app.models.market import Symbol
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def fk_session() -> AsyncIterator[AsyncSession]:
    """A session on SQLite with foreign keys actually enforced."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001, ANN202
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    import app.auth.models  # noqa: F401 - register the auth tables
    import app.models  # noqa: F401 - register every platform table

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        enforced = (await session.execute(text("PRAGMA foreign_keys"))).scalar()
        assert enforced == 1, "the pragma did not take; this test would prove nothing"
        # `orders.symbol_id` is a foreign key too, so with the pragma on there
        # has to be a real symbol to point at. That it is needed at all is the
        # point of the file: without the pragma none of these columns is
        # checked, and a row naming nothing in particular inserts happily.
        session.add(
            Symbol(
                id="sym-1",
                code="EURUSD",
                asset_class="fx",
                unit_class="points",
                is_active=True,
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


async def test_the_pragma_is_the_thing_that_makes_this_file_meaningful(
    fk_session: AsyncSession,
) -> None:
    """Guard the guard.

    If the pragma silently stopped applying, every test below would pass for the
    wrong reason -- SQLite ignoring the constraint rather than the code
    respecting it. That failure mode is invisible, so it gets its own assertion.
    """
    enforced = (await fk_session.execute(text("PRAGMA foreign_keys"))).scalar()
    assert enforced == 1


async def test_an_order_naming_a_signal_that_does_not_exist_is_rejected(
    fk_session: AsyncSession,
) -> None:
    """The defect, reproduced.

    This is what PostgreSQL did and SQLite did not. Without the pragma this
    insert succeeds and the suite stays green while production refuses.
    """
    key = "an-idempotency-key-that-is-not-a-signal"
    fk_session.add(
        Order(
            id="ord-dangling",
            intent_id=key,
            signal_id=key,  # the defect: a key where a signals.id belongs
            symbol_id="sym-1",
            side="buy",
            status="intent",
            mode="demo",
            quantity=Decimal("0.01"),
        )
    )
    with pytest.raises(Exception) as exc:
        await fk_session.commit()
    assert "FOREIGN KEY" in str(exc.value).upper()
    await fk_session.rollback()


async def test_the_same_order_with_no_signal_id_is_accepted(fk_session: AsyncSession) -> None:
    """The fix, at the database.

    Same row, same enforced constraint, `signal_id` null -- so the refusal above
    is about the dangling reference and not about anything else in the row.
    """
    fk_session.add(
        Order(
            id="ord-manual",
            intent_id="an-idempotency-key-that-is-not-a-signal",
            signal_id=None,
            symbol_id="sym-1",
            side="buy",
            status="intent",
            mode="demo",
            quantity=Decimal("0.01"),
        )
    )
    await fk_session.commit()
    stored = await fk_session.get(Order, "ord-manual")
    assert stored is not None
    assert stored.signal_id is None
    assert stored.intent_id == "an-idempotency-key-that-is-not-a-signal"


async def test_the_manual_order_route_leaves_signal_id_null() -> None:
    """The fix, asserted where it lives.

    A manual submission has no signal behind it, so the column is null and the
    idempotency key lives in `intent_id`, which is UNIQUE and is what
    `guard_resend` reads. Asserted by reading the route's source rather than by
    driving it: exercising it needs a venue, and the point of this test is that
    the constraint holds without one.
    """
    import inspect

    from app.api.v1 import orders

    source = inspect.getsource(orders.submit_order)
    assert "signal_id=None," in source, (
        "the manual order route sets a signal_id again; if it is not a real "
        "signals row this fails on PostgreSQL and passes on SQLite"
    )
    assert "signal_id=key" not in source
