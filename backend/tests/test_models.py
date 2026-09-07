"""The schema promised at L05 exists, is self-consistent, and round-trips."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal

import pytest
from app.db.base import Base
from app.models import (
    EXPECTED_TABLES,
    Order,
    OrderEvent,
    RoleRow,
    Symbol,
    Trade,
    User,
    seed_roles,
)
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await seed_roles(session)
        yield session


async def test_every_promised_table_exists(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        names = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    missing = EXPECTED_TABLES - names
    assert not missing, f"missing tables: {sorted(missing)}"
    # users + roles + auth_sessions + password_reset_tokens + 51 platform tables
    # (market_bars added at L08; feature_sets, label_sets, datasets and
    # dataset_checks at L23; validation_runs at L26; ai_strategy_configs and
    # ai_decisions at L27; model_deployments and model_lifecycle_events at L28;
    # model_monitoring_snapshots and model_alerts at L29; trade_reviews at L33;
    # notification_deliveries and notification_preferences at L34.
    #
    # L30, L31 and L32 added NONE: the portfolio reads `portfolio_snapshots`
    # which L05 already had, the journal extended `trades` rather than shadowing
    # it, and analytics aggregates at read time. L34 added TWO rather than
    # three: `notifications` has existed since L05 and was extended, not
    # shadowed by a second table with a better name.
    #
    # L54/L55 added ONE: `capital_reservations`. The count is asserted rather
    # than derived so that adding a table is a deliberate act -- this line
    # failing is the guard working, and the right response is to say WHY the
    # table exists, here, before changing the number.
    #
    # It exists because reservations were a process-local dict: an approval
    # that reserved budget and had not filled when the process died released
    # nothing, and the next process believed the whole budget was free. The
    # same shape as the L45 C-1 defect. L53 and L56 added none -- portfolio
    # intelligence and strategy quarantine both reused columns and services
    # that already existed.
    assert len(EXPECTED_TABLES) == 56


async def test_metadata_matches_expected_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


async def test_roles_seeded_and_user_role_references_them(db: AsyncSession) -> None:
    roles = {r.name: r.rank for r in (await db.scalars(select(RoleRow))).all()}
    assert roles == {"user": 0, "trader": 1, "admin": 2}
    fk_targets = {fk.target_fullname for fk in User.__table__.c.role.foreign_keys}
    assert fk_targets == {"roles.name"}


async def test_order_execution_trade_round_trip(db: AsyncSession) -> None:
    sym = Symbol(code="EURUSD", asset_class="fx", digits=5, point_size=Decimal("0.00001"))
    db.add(sym)
    await db.flush()
    order = Order(
        intent_id="test:1",
        mode="demo",
        symbol_id=sym.id,
        side="buy",
        quantity=Decimal("0.01"),
        requested_price=Decimal("1.10000"),
        status="filled",
        source="manual",
    )
    db.add(order)
    await db.flush()
    db.add(OrderEvent(order_id=order.id, event_type="submitted", occurred_at=datetime(2026, 9, 1)))
    db.add(
        Trade(
            mode="demo",
            broker_position_id="42",
            symbol_id=sym.id,
            side="long",
            volume=Decimal("0.01"),
            entry_price=Decimal("1.10000"),
            exit_price=Decimal("1.10100"),
            opened_at=datetime(2026, 9, 1, 10),
            closed_at=datetime(2026, 9, 1, 11),
            gross_profit=Decimal("1.00"),
            net_profit=Decimal("1.00"),
            r_multiple=Decimal("0.6667"),
            bracket={"reward_risk": 1.0},
            source="manual",
        )
    )
    await db.commit()

    got = await db.scalar(select(Trade).where(Trade.broker_position_id == "42"))
    assert got is not None
    assert got.r_multiple == Decimal("0.666700")
    assert got.bracket == {"reward_risk": 1.0}
    events = (await db.scalars(select(OrderEvent).where(OrderEvent.order_id == order.id))).all()
    assert [e.event_type for e in events] == ["submitted"]


def test_every_status_value_fits_its_column() -> None:
    """A declared vocabulary must fit the column that stores it.

    SQLite ignores `VARCHAR(n)`; PostgreSQL enforces it. So a status value
    longer than its column passes every test here and raises
    `StringDataRightTruncationError` the first time a real deployment reaches
    that state — which is the worst possible place to find out.

    It had happened twice before this test existed. `positions.status` was
    `String(8)` while L21 added `partially_closed` (16) and `reconciling` (11),
    so a partial close or a reconciliation pass would have failed in
    production on the position-management path. `training_runs.status` was
    `String(16)` while L25's successful terminus is `validation_pending` (18).

    Walks every CHECK of the form `col IN ('a','b',...)`, so it covers every
    table rather than the two that were caught.
    """
    import re

    from sqlalchemy import String

    overflowing: list[str] = []
    checked = 0
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            text = str(getattr(constraint, "sqltext", "")).strip()
            match = re.match(r"^(\w+) IN \((.*)\)$", text)
            if not match:
                continue
            column = table.columns.get(match.group(1))
            if column is None or not isinstance(column.type, String):
                continue
            if column.type.length is None:
                continue
            for value in re.findall(r"'([^']*)'", match.group(2)):
                checked += 1
                if len(value) > column.type.length:
                    overflowing.append(
                        f"{table.name}.{column.name} is String({column.type.length}) "
                        f"but declares {value!r} ({len(value)} chars)"
                    )

    assert checked > 50, f"only {checked} values examined; the walk found nothing to check"
    assert not overflowing, "\n".join(overflowing)
