"""The `broker_accounts` row that lets a position name where it is held.

Registering a venue used to fill two in-process registries and write nothing
durable. A position recorded from a fill therefore had no way to say which venue
held it, and the close route could not work out which adapter to ask for a
price -- which is what refused the first attempt to close a real demo position
on 2026-09-07.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.brokers.accounts import ACCOUNT_MODES, account_for, ensure_broker_account
from app.db.base import Base
from app.models.accounts import BrokerAccount
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

USER = "user-1"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
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
        # `broker_accounts.user_id` is a foreign key to `users`, and this
        # fixture enforces foreign keys. Seeding real operators is not test
        # ceremony -- it is the constraint the route satisfies by passing the
        # signed-in user's id.
        from app.auth.models import User

        # `users.role` is itself a foreign key, to `roles.name`, so seeding a
        # user needs the role to exist. `roles` is seeded once in conftest, on
        # the table's own `after_create`, from the same ROLE_SEED constant
        # migration 0002 uses -- so this fixture no longer seeds it and cannot
        # drift from what production has.

        for uid in ("user-1", "user-2"):
            session.add(
                User(
                    id=uid,
                    email=f"{uid}@example.org",
                    password_hash="not-a-real-hash",
                    role="user",
                    is_active=True,
                )
            )
        await session.commit()
        yield session
    await engine.dispose()


def _account(**over: object) -> SimpleNamespace:
    """The `Account` an adapter returns from connect() -- what the VENUE said."""
    base = {
        "login": "5055473926",
        "server": "MetaQuotes-Demo",
        "currency": "USD",
        "balance": Decimal("100000"),
    }
    base.update(over)
    return SimpleNamespace(**base)


async def test_registering_a_demo_venue_records_the_account(db: AsyncSession) -> None:
    row = await ensure_broker_account(
        db,
        user_id=USER,
        name="mt5-demo-5055473926",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(),
    )
    await db.commit()

    assert row is not None
    stored = await db.scalar(select(BrokerAccount))
    assert stored is not None
    assert stored.broker == "mt5"
    assert stored.account_mode == "demo"
    assert stored.login == "5055473926"
    assert stored.server == "MetaQuotes-Demo"
    assert stored.currency == "USD"
    assert stored.name == "mt5-demo-5055473926"
    assert stored.is_active is True


async def test_the_simulator_gets_no_account_row(db: AsyncSession) -> None:
    """`account_mode` is demo or live. A fictional account does not belong in
    the same table as a real one, and the schema says so."""
    assert "paper" not in ACCOUNT_MODES
    row = await ensure_broker_account(
        db,
        user_id=USER,
        name="sim-1",
        adapter_key="simulator",
        mode="paper",
        account=_account(),
    )
    await db.commit()
    assert row is None
    assert await db.scalar(select(BrokerAccount)) is None


async def test_an_unknown_adapter_writes_nothing(db: AsyncSession) -> None:
    """`broker IN ('mt5','fake')` is a check constraint. A key with no mapping
    writes no row rather than a value the database would reject anyway."""
    row = await ensure_broker_account(
        db,
        user_id=USER,
        name="whatever",
        adapter_key="some_future_venue",
        mode="demo",
        account=_account(),
    )
    await db.commit()
    assert row is None


async def test_re_registering_the_same_terminal_updates_one_row(db: AsyncSession) -> None:
    """Identity is the account, not the label an operator typed."""
    await ensure_broker_account(
        db,
        user_id=USER,
        name="first-label",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(),
    )
    await db.commit()
    await ensure_broker_account(
        db,
        user_id=USER,
        name="second-label",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(),
    )
    await db.commit()

    rows = list(await db.scalars(select(BrokerAccount)))
    assert len(rows) == 1
    assert rows[0].name == "second-label", "the label is refreshed, not matched on"


async def test_a_different_account_is_a_different_row(db: AsyncSession) -> None:
    await ensure_broker_account(
        db,
        user_id=USER,
        name="a",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(login="111"),
    )
    await ensure_broker_account(
        db,
        user_id=USER,
        name="b",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(login="222"),
    )
    await db.commit()
    assert len(list(await db.scalars(select(BrokerAccount)))) == 2


async def test_another_users_account_is_never_reused(db: AsyncSession) -> None:
    """Two operators may each register the same terminal. Matching across users
    would hand one of them a row scoped to the other."""
    await ensure_broker_account(
        db,
        user_id="user-1",
        name="a",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(),
    )
    await ensure_broker_account(
        db,
        user_id="user-2",
        name="a",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(),
    )
    await db.commit()
    assert len(list(await db.scalars(select(BrokerAccount)))) == 2


# ------------------------------------------------------------- the lookup


async def test_the_registry_key_finds_the_row(db: AsyncSession) -> None:
    """The shared key is the whole link between an in-process adapter and a
    durable row: `broker_accounts.name` IS the id an operator registered with."""
    await ensure_broker_account(
        db,
        user_id=USER,
        name="mt5-demo-5055473926",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(),
    )
    await db.commit()

    found = await account_for(db, name="mt5-demo-5055473926", mode="demo")
    assert found is not None
    assert found.login == "5055473926"


async def test_the_lookup_is_scoped_to_the_mode(db: AsyncSession) -> None:
    await ensure_broker_account(
        db,
        user_id=USER,
        name="acct",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(),
    )
    await db.commit()
    assert await account_for(db, name="acct", mode="live") is None
    assert await account_for(db, name="acct", mode="paper") is None


async def test_an_unknown_key_finds_nothing(db: AsyncSession) -> None:
    assert await account_for(db, name="never-registered", mode="demo") is None


async def test_a_deactivated_account_is_not_returned(db: AsyncSession) -> None:
    row = await ensure_broker_account(
        db,
        user_id=USER,
        name="acct",
        adapter_key="mt5_demo",
        mode="demo",
        account=_account(),
    )
    assert row is not None
    await db.commit()
    row.is_active = False
    await db.commit()

    assert await account_for(db, name="acct", mode="demo") is None


async def test_the_row_holds_no_credential(db: AsyncSession) -> None:
    """A login is half a credential and the half printed on every statement.
    The other half is not in this table and is not anywhere."""
    columns = {c.name for c in BrokerAccount.__table__.columns}
    for forbidden in ("password", "secret", "token", "api_key", "credential"):
        assert not any(forbidden in c for c in columns), columns


@pytest.mark.parametrize("mode", ["paper", "", "LIVE", "unknown"])
async def test_a_mode_that_is_not_a_venue_writes_nothing(db: AsyncSession, mode: str) -> None:
    row = await ensure_broker_account(
        db, user_id=USER, name="x", adapter_key="mt5_demo", mode=mode, account=_account()
    )
    await db.commit()
    assert row is None
