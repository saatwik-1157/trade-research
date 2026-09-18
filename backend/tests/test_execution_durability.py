"""The execution pipeline across a process restart. **The L45 C-1 regression.**

Every duplicate-order test this platform had lived inside one process. That is
exactly why the defect survived six structural audits: the property under test
was "the same pipeline object refuses twice", and the property that matters is
"the same *intent* produces one order, whatever happens to the process".

The defect, reproduced before the fix and preserved here as
`test_the_restart_regression_is_capable_of_failing`:

    A. SAME process    pass1 = execution_unknown   pass2 = duplicate  -> REFUSED
    B. ACROSS a restart pass1 = execution_unknown  pass2 = filled     -> ALLOWED

Four guards were meant to stop B and none could. `pipeline.seen` and
`OrderManager.by_intent` are process-local; `guard_resend` reads `by_intent`;
and `orders.intent_id` UNIQUE could not fire because **the pipeline never wrote
an `orders` row at all**. Startup reconciliation reads the same empty table, so
safe mode never latched either.

A restart is not exotic. `DEPLOYMENT_GUIDE.md` documents the safe deploy as
stop -> migrate -> start; a crash, an OOM kill and a rolling replacement all
produce the same state.

**A restart here is a genuinely fresh pipeline**: a new `ExecutionPipeline`, a
new `OrderManagerRegistry` and a new `OrderManager`, so `seen` and `by_intent`
are empty exactly as they are in a new process. Only the database and the venue
carry over -- which is precisely what survives a real restart.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.brokers.base import SymbolInfo
from app.brokers.fake import FakeBroker
from app.db.base import Base
from app.execution import ExecutionPipeline, IncomingSignal, Outcome, StrategyState
from app.execution.store import DatabaseOrderStore, OrderNotRecorded, PriorOrder
from app.models.execution import Order
from app.oms.order import ManagedOrder
from app.oms.registry import OrderManagerRegistry
from app.oms.state import NEEDS_RECONCILIATION, OrderStatus
from app.recovery.reconciliation import unresolved_orders
from app.risk.engine import RiskEngine, RiskLimits
from app.sizing.calculator import SizingMethod
from app.symbols.service import ContractSpec
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# The venue's own contract terms. Required since OrderManager.submit
# began validating against them: FakeBroker.symbols defaults EMPTY, and
# an absent spec is a refusal by design -- giving the simulator a
# built-in default would make it the one path where an unspecced symbol
# passes, which is the fail-open the check removes.
VENUE_SPEC = SymbolInfo(
    symbol="EURUSD",
    digits=5,
    point=Decimal("0.00001"),
    contract_size=Decimal("100000"),
    tick_size=Decimal("0.00001"),
    tick_value=Decimal("1"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("100"),
    volume_step=Decimal("0.01"),
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

ACCOUNT = "acct-a"
INTENT = "tv:EURUSD:buy:1"

SPEC = ContractSpec(
    internal_symbol="EURUSD",
    broker_symbol="EURUSD",
    provider="simulator",
    contract_size=Decimal("100000"),
    tick_size=Decimal("0.00001"),
    tick_value=Decimal("1"),
    minimum_volume=Decimal("0.01"),
    maximum_volume=Decimal("100"),
    volume_step=Decimal("0.01"),
    price_precision=5,
    volume_precision=2,
    trading_hours=None,
    spec_source="test",
    spec_updated_at=None,
)


# ================================================================= fixtures


def signal(**overrides: object) -> IncomingSignal:
    base: dict[str, object] = {
        "signal_id": None,
        "signal_key": INTENT,
        "source": "tradingview",
        "symbol": "EURUSD",
        "side": "buy",
        "signal_time": T0,
        "account_id": ACCOUNT,
        "mode": "paper",
        "strategy_id": "sma_cross",
        "entry_price": Decimal("1.10000"),
        "stop_loss": Decimal("1.09500"),
        "take_profit": Decimal("1.11000"),
        "auth_strength": "strong",
    }
    base.update(overrides)
    return IncomingSignal(**base)  # type: ignore[arg-type]


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real schema on SQLite. The `orders` table is the point of this file,
    so it cannot be stubbed."""
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # SQLite ignores foreign keys unless asked, and PostgreSQL does not. Without
    # this the account resolution added with the C-1 fix would appear to work
    # here and raise an IntegrityError in production -- so the test database is
    # made to enforce what the real one enforces.
    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from app.auth.models import Role, User
    from app.models.accounts import PaperAccount
    from app.models.market import Symbol

    async with factory() as db:
        # `users.role` references `roles.name`. Every suite enforces foreign
        # keys now, and conftest seeds `roles` once on the table's own
        # `after_create` -- so the parent rows are real here without this
        # fixture restating them.
        db.add(User(id="u1", email="a@b.io", password_hash="x", role=str(Role.admin)))
        db.add(Symbol(id="sym-eurusd", code="EURUSD", asset_class="fx"))
        db.add(
            PaperAccount(
                id=ACCOUNT,
                user_id="u1",
                name="paper",
                currency="USD",
                starting_balance=Decimal("100000"),
                balance=Decimal("100000"),
                equity=Decimal("100000"),
            )
        )
        await db.commit()
    yield factory
    await engine.dispose()


class CountingBroker(FakeBroker):
    """A `FakeBroker` carrying the record of what it was asked to send.

    The list is attached by the fixture below; naming it here is what lets the
    assertions read `venue.sends` without reaching past the declared type.
    """

    sends: list[object]


@pytest.fixture
async def venue() -> AsyncIterator[CountingBroker]:
    """A venue that counts every `place_order` it is asked to make.

    `FakeBroker._orders_placed` is NOT that count: it increments after the
    fault injector, so a send that returns `unknown` never appears in it -- and
    "the venue was asked twice" is the exact fact this file has to measure.
    Counting the calls rather than the successes is the difference between
    testing the guard and testing the simulator.
    """
    fake = CountingBroker(mode="paper")
    await fake.connect()
    fake.set_quote("EURUSD", "1.10000", "1.10002")
    fake.symbols["EURUSD"] = VENUE_SPEC

    original = fake.place_order
    sends: list[object] = []

    async def counting(request: object) -> object:
        sends.append(request)
        return await original(request)  # type: ignore[arg-type]

    fake.place_order = counting  # type: ignore[assignment,method-assign]
    fake.sends = sends
    yield fake
    await fake.disconnect()


def boot(
    venue: FakeBroker,
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    store: object | None = None,
) -> ExecutionPipeline:
    """Start a pipeline. Calling this twice IS the restart.

    Everything process-local is rebuilt -- the registry, the order manager,
    `seen` and `by_intent` -- and only the database and the venue carry over.
    """
    registry = OrderManagerRegistry()
    registry.register(ACCOUNT, venue, mode="paper", broker="fake")

    async def spec_for(_symbol: str) -> ContractSpec | None:
        return SPEC

    def strategy_state(_strategy_id: str | None) -> StrategyState:
        return StrategyState(exists=True, enabled=True)

    resolved = store if store is not None else _store_or_none(sessions)
    return ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(RiskLimits(require_stop_loss=False)),
        spec_for=spec_for,
        strategy_state=strategy_state,
        sizing_method=SizingMethod.fixed_risk,
        risk_amount=Decimal("100"),
        store=resolved,  # type: ignore[arg-type]
    )


def _store_or_none(
    sessions: async_sessionmaker[AsyncSession] | None,
) -> DatabaseOrderStore | None:
    return None if sessions is None else DatabaseOrderStore(sessions)


async def orders_for(
    sessions: async_sessionmaker[AsyncSession], intent: str = INTENT
) -> list[Order]:
    async with sessions() as db:
        return list((await db.scalars(select(Order).where(Order.intent_id == intent))).all())


# ============================================ 1. the headline: unknown order


async def test_an_unknown_order_is_not_resent_after_a_restart(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """**The C-1 regression.** One signal, one venue, one restart, one order.

    The venue is deliberately made healthy again before the second pass: if it
    still refused, the test would pass for the wrong reason. The only thing
    standing between the second pass and a second live order is the record.
    """
    venue.unknown_next = True
    first = await boot(venue, sessions).process(signal(), now=T0)
    assert first.outcome is Outcome.execution_unknown

    # The row that every guard downstream depends on. Before the fix there was
    # no row here at all, and that single absence disabled four protections.
    rows = await orders_for(sessions)
    assert len(rows) == 1
    assert OrderStatus(rows[0].status) in NEEDS_RECONCILIATION

    # --- restart. New pipeline, new registry, new manager, empty `seen`. ---
    venue.unknown_next = False  # the venue would happily take a second order
    second = await boot(venue, sessions).process(signal(), now=T0)

    assert second.outcome is Outcome.execution_unknown, (
        "an intent whose order was never settled was re-sent after a restart"
    )
    # NOT `created_order is False`: `execution_unknown` is deliberately outside
    # NO_ORDER, because an order for this intent DOES exist -- pass one made it.
    # What must be true is that THIS pass made nothing and sent nothing.
    assert second.order is None
    assert "reconcile" in second.detail.lower()

    assert len(await orders_for(sessions)) == 1
    assert len(venue.sends) == 1, "the venue was asked to place a second order"


async def test_the_restart_regression_is_capable_of_failing(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The same scenario with no store reproduces the defect exactly.

    A regression test that cannot fail is C-4 in test form, and this file
    exists because of C-4. So the old behaviour is pinned here: without the
    durable record the restart IS allowed, the venue DOES get a second order,
    and that is what shipped.
    """
    venue.unknown_next = True
    first = await boot(venue, None).process(signal(), now=T0)
    assert first.outcome is Outcome.execution_unknown

    venue.unknown_next = False
    second = await boot(venue, None).process(signal(), now=T0)

    # The defect, stated as an assertion so it can never silently return.
    assert second.outcome is Outcome.filled
    assert len(venue.sends) == 2, "the defect no longer reproduces"
    assert await orders_for(sessions) == []


# ============================================== 2. the settled-order variant


async def test_a_completed_order_is_not_repeated_after_a_restart(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Not only the unknown case. A signal that FILLED must not fill twice.

    This is the commoner shape in practice: a deploy lands between the fill and
    the next worker tick, and the signal row is still reoffered.
    """
    first = await boot(venue, sessions).process(signal(), now=T0)
    assert first.outcome is Outcome.filled

    second = await boot(venue, sessions).process(signal(), now=T0)
    assert second.outcome is Outcome.duplicate_signal
    assert not second.created_order
    assert len(venue.sends) == 1
    assert len(await orders_for(sessions)) == 1


def _memory_verdict(status: OrderStatus) -> str:
    """What the in-process guard says about an intent in this state."""
    from app.oms.service import OrderManager, OrderRefused, ReconciliationRequired

    manager = OrderManager(FakeBroker(mode="paper"), mode="paper")
    manager.resume(
        [
            ManagedOrder(
                id="o1",
                client_order_id=INTENT,
                account_id=ACCOUNT,
                symbol="EURUSD",
                side="buy",
                quantity=Decimal("0.1"),
                mode="paper",
                created_at=T0,
                status=status,
            )
        ]
    )
    try:
        manager.guard_resend(INTENT)
    except ReconciliationRequired:
        return "reconcile"
    except OrderRefused:
        return "refused"
    return "allowed"


async def _durable_verdict(status: OrderStatus) -> str:
    """What the database-backed guard says about the same state."""
    from types import SimpleNamespace

    from app.execution.store import _prior_from
    from app.oms.service import OrderRefused, ReconciliationRequired

    prior = _prior_from(SimpleNamespace(id="o1", intent_id=INTENT, status=str(status)))

    class Stub:
        async def prior_order(self, intent_id: str) -> PriorOrder:
            return prior

        async def record(self, order: ManagedOrder) -> None:  # pragma: no cover
            raise AssertionError("not used")

    pipeline = boot(FakeBroker(mode="paper"), None, store=Stub())
    try:
        await pipeline._guard_resend_durably(INTENT)
    except ReconciliationRequired:
        return "reconcile"
    except OrderRefused:
        return "refused"
    return "allowed"


@pytest.mark.parametrize("status", list(OrderStatus))
async def test_the_durable_guard_is_never_more_permissive(
    status: OrderStatus,
) -> None:
    """For EVERY order state, the record may refuse more than memory does and
    must never allow more.

    This is the property that keeps the fix safe as the state machine grows.
    Parametrising over `OrderStatus` means a state added later cannot quietly
    pick a permissive default -- which is how C-1 would come back.
    """
    durable = await _durable_verdict(status)
    memory = _memory_verdict(status)
    assert durable != "allowed" or memory == "allowed", (
        f"the durable guard allows a second order in `{status}` that the in-memory guard refuses"
    )
    # An unresolved order must be classified as needing RECONCILIATION by both,
    # not merely refused: the two produce different outcomes, different
    # counters and different operator instructions.
    assert (durable == "reconcile") == (memory == "reconcile")


def test_the_one_state_where_the_record_is_stricter_than_the_oms() -> None:
    """`failed`, and only `failed`. Named here so the divergence is a decision
    on the record rather than something a reader has to infer.

    The OMS permits a fresh order for an intent whose send provably never
    reached the venue (`SAFE_TO_RESEND`). The `orders` table does not:
    `intent_id` is UNIQUE, so one intent has one order row by construction.
    The schema is the stricter authority and it wins -- refusing at the guard,
    where the message can say why, rather than as an IntegrityError raised
    between `create` and `submit`.
    """
    stricter = [s for s in OrderStatus if _memory_verdict(s) == "allowed"]
    assert stricter == [OrderStatus.failed]


async def test_a_failed_send_is_refused_a_second_order_row(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The stricter reading, exercised end to end.

    A send that never reached the venue is recorded as `failed`, and a later
    pass over the same intent is refused with a message that names the reason.
    Nothing reaches the venue and no IntegrityError is raised -- the refusal
    happens at the guard, before the decision to send is taken.

    The cost of this strictness is a signal that is not retried. It is bounded:
    `execution_rejected` already retires the signal row (`status_for`), so the
    worker was never going to offer it again anyway. A genuine retry needs a
    fresh alert, which carries a fresh intent.
    """
    from app.oms.state import OrderStatus as S

    venue.force_disconnect()
    first = await boot(venue, sessions).process(signal(), now=T0)
    assert first.outcome is Outcome.execution_rejected
    # ZERO attempts, and that is the stricter reading getting stricter.
    #
    # This asserted 1 until 2026-09-18: the send reached `place_order` and the
    # disconnected fake refused it there. `OrderManager.submit` now reads the
    # venue's contract terms before building the request, and a disconnected
    # venue cannot report them -- `get_symbols()` requires a connection -- so
    # the refusal happens one step earlier and nothing is ever handed to
    # `place_order`. Same `failed` state, same safe-to-resend guarantee, same
    # `NotConnected` in the reason; the only difference is that the order was
    # refused before it was even constructed, which is the fail-closed rule
    # applied sooner rather than later. The load-bearing check is below: the
    # SECOND pass adds no sends, whatever the first pass counted.
    attempted = len(venue.sends)
    assert attempted == 0

    rows = await orders_for(sessions)
    assert [S(r.status) for r in rows] == [S.failed], (
        "a send that never reached the venue must be recorded as `failed`"
    )

    await venue.connect()
    second = await boot(venue, sessions).process(signal(), now=T0)
    assert second.outcome is Outcome.duplicate_signal
    assert "UNIQUE" in second.detail
    assert "fresh intent" in second.detail
    assert len(venue.sends) == attempted, "a second send was attempted"
    assert len(await orders_for(sessions)) == 1


# ==================================================== 3. the ordering, §18


class RecordingStore:
    """A store that remembers the status of each order as it was written."""

    def __init__(self, inner: DatabaseOrderStore) -> None:
        self.inner = inner
        self.writes: list[str] = []

    async def prior_order(self, intent_id: str) -> PriorOrder | None:
        return await self.inner.prior_order(intent_id)

    async def record(self, order: ManagedOrder) -> None:
        self.writes.append(str(order.status))
        await self.inner.record(order)


async def test_the_order_is_recorded_before_the_venue_is_called(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Brief §18: the CREATED row exists before anything is transmitted.

    The API order route has always done this. The pipeline never did, and that
    asymmetry between the manual path and the automated one was C-1.
    """
    spy = RecordingStore(DatabaseOrderStore(sessions))
    result = await boot(venue, sessions, store=spy).process(signal(), now=T0)

    assert result.outcome is Outcome.filled
    assert spy.writes[0] == str(OrderStatus.intent), "the first durable write must precede the send"
    assert spy.writes[-1] == str(OrderStatus.filled)


# =============================================== 4. failing closed on storage


class BrokenStore:
    """Storage that is down. Nothing may be sent while it is."""

    def __init__(self) -> None:
        self.attempts = 0

    async def prior_order(self, intent_id: str) -> PriorOrder | None:
        return None

    async def record(self, order: ManagedOrder) -> None:
        self.attempts += 1
        raise OrderNotRecorded("simulated: the database is unreachable")


async def test_an_order_that_cannot_be_recorded_is_never_sent(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """An order at a venue with no record of it is the failure §18 exists to
    prevent. So a storage fault refuses the pass rather than sending anyway."""
    broken = BrokenStore()
    pipeline = boot(venue, sessions, store=broken)
    result = await pipeline.process(signal(), now=T0)

    assert result.outcome is Outcome.not_recorded
    assert not result.created_order
    assert venue.sends == [], "an unrecorded order reached the venue"


async def test_a_signal_refused_by_storage_is_not_consumed(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The database coming back must not cost a real trading signal.

    Two things had to be right for this: the signal is not added to `seen`, and
    the order created in memory is DISCARDED from the OMS. Without the second,
    the next pass finds a live order for the intent, reports a duplicate, and
    the worker retires a signal that never reached a venue.
    """
    broken = BrokenStore()
    pipeline = boot(venue, sessions, store=broken)
    refused = await pipeline.process(signal(), now=T0)
    assert refused.outcome is Outcome.not_recorded
    assert pipeline.seen == set()

    manager = pipeline.managers.get(ACCOUNT)
    assert manager.by_intent == {}, "a phantom order was left behind for this intent"
    assert manager.orders == {}

    # Storage recovers, on the same pipeline, and the signal still trades.
    pipeline.store = DatabaseOrderStore(sessions)
    recovered = await pipeline.process(signal(), now=T0)
    assert recovered.outcome is Outcome.filled
    assert len(venue.sends) == 1


# ====================================== 5. the chain C-1 also broke: recovery


async def test_startup_reconciliation_can_see_the_pipelines_unresolved_order(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The second half of C-1, and the reason it was CRITICAL rather than a
    duplicate-order bug.

    `recovery/reconciliation.unresolved_orders` reads the `orders` table. With
    no row it reported `unresolved=0`, so the startup sequence found nothing
    and **safe mode never latched** -- removing the platform's last line of
    defence at exactly the moment it was needed.
    """
    venue.unknown_next = True
    result = await boot(venue, sessions).process(signal(), now=T0)
    assert result.outcome is Outcome.execution_unknown

    async with sessions() as db:
        unresolved = await unresolved_orders(db)
    assert len(unresolved) == 1
    assert unresolved[0].intent_id == INTENT


async def test_a_recorded_order_carries_its_account(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`persist` never wrote an account, so every stored order had both account
    columns NULL.

    That left a recovered order belonging to nobody (`_rehydrate` returned
    `account_id=""`) and silently emptied two live queries that filter on those
    columns -- per-account order history in analytics, and the paper account's
    own order list.
    """
    assert (await boot(venue, sessions).process(signal(), now=T0)).outcome is Outcome.filled
    rows = await orders_for(sessions)
    assert rows[0].paper_account_id == ACCOUNT
    assert rows[0].broker_account_id is None


async def test_an_order_whose_account_is_unknown_is_still_recorded(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Both account columns carry a foreign key, so an id with no row would
    turn a successful order into an IntegrityError at commit.

    Losing the record of an order that may already sit at a venue is the one
    outcome this whole mechanism exists to prevent, so an unrecognised account
    is looked up, reported and left NULL rather than written blindly. The
    foreign keys are enforced in this fixture, so a regression here fails here.
    """
    registry = OrderManagerRegistry()
    registry.register("acct-ghost", venue, mode="paper", broker="fake")

    async def spec_for(_symbol: str) -> ContractSpec | None:
        return SPEC

    pipeline = ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(RiskLimits(require_stop_loss=False)),
        spec_for=spec_for,
        strategy_state=lambda _s: StrategyState(exists=True, enabled=True),
        sizing_method=SizingMethod.fixed_risk,
        risk_amount=Decimal("100"),
        store=DatabaseOrderStore(sessions),
    )
    result = await pipeline.process(signal(account_id="acct-ghost"), now=T0)

    assert result.outcome is Outcome.filled, "an unknown account broke the write"
    rows = await orders_for(sessions)
    assert len(rows) == 1
    assert rows[0].paper_account_id is None
    assert rows[0].broker_account_id is None


async def test_an_unresolved_order_can_be_reloaded_into_a_new_manager(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`OrderManager.resume` and `load_unresolved` were built, tested and never
    called. They work; nothing wired them. This asserts the round trip, so the
    recovery path a new process would use is exercised rather than assumed."""
    from app.oms.repository import OrderRepository

    venue.unknown_next = True
    await boot(venue, sessions).process(signal(), now=T0)

    fresh = OrderManagerRegistry()
    manager = fresh.register(ACCOUNT, venue, mode="paper", broker="fake")
    assert manager.by_intent == {}

    async with sessions() as db:
        repo = OrderRepository({"EURUSD": "sym-eurusd"})
        loaded = await repo.load_unresolved(db, mode="paper")
    needs = manager.resume(loaded)

    assert [o.client_order_id for o in needs] == [INTENT]
    assert manager.by_intent[INTENT].account_id == ACCOUNT
    assert fresh.unresolved() == {ACCOUNT: [needs[0].id]}


# ================================================= 6. the seat must be filled


def test_the_deployed_pipeline_is_given_a_store() -> None:
    """The defect one level up: building the mechanism and not wiring it.

    `resume()` and `load_unresolved()` were both complete, both tested and both
    had zero callers in `app/`. A `store=` parameter with no argument at the
    one construction site would be the same defect wearing the fix's clothes,
    so this reads `app/main.py` rather than trusting it.
    """
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ExecutionPipeline"
    ]
    assert built, "app/main.py no longer constructs an ExecutionPipeline"
    for call in built:
        keywords = {kw.arg for kw in call.keywords}
        assert "store" in keywords, (
            "the deployed execution pipeline is constructed without a store, so its "
            "duplicate and unresolved-order guards do not survive a restart (L45 C-1)"
        )


def test_a_pipeline_without_a_store_says_so() -> None:
    """Honesty over a silent default. A pipeline that cannot survive a restart
    reports that in `status()` rather than looking identical to one that can."""
    venue = FakeBroker(mode="paper")
    assert boot(venue, None).status()["durable"] is False
    assert "NOT DURABLE" in str(boot(venue, None).status()["durability"])


def test_the_deployed_reconcile_sweep_is_constructed_registered_and_gated() -> None:
    """The same defect one item later: `OrderManager.reconcile` was complete,
    tested, and called from one HTTP route. A worker module with no
    construction site in `app/main.py` would be the mechanism built and not
    wired again, so this reads the file rather than trusting it."""
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    text = source.read_text(encoding="utf-8")
    tree = ast.parse(text)

    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OmsReconcileWorker"
    ]
    assert built, "app/main.py no longer constructs an OmsReconcileWorker"
    for call in built:
        assert len(call.args) == 2, "the sweep takes the registry and the store, positionally"
        keywords = {kw.arg for kw in call.keywords}
        assert {"interval_seconds", "max_per_pass"} <= keywords
    assert "registry.register(app.state.oms_reconciler)" in text
    assert "settings.oms_reconcile_enabled and settings.workers_enabled" in text
    # One store for the pipeline that writes orders and the sweep that settles
    # them; a second `store_for` would be a second symbol cache.
    assert text.count("order_store_for(") == 1


# ============================================== 7. the OMS discard is fenced


async def test_an_order_that_reached_the_venue_cannot_be_discarded(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`discard` exists for one caller and one moment: a durable write that
    failed before anything was sent.

    A method that could erase an order the venue has seen would be a way to
    lose exactly the record this platform exists to keep, so the fence is a
    refusal rather than a comment.
    """
    from app.oms.service import OrderRefused

    pipeline = boot(venue, sessions)
    assert (await pipeline.process(signal(), now=T0)).outcome is Outcome.filled

    manager = pipeline.managers.get(ACCOUNT)
    order = manager.by_intent[INTENT]
    with pytest.raises(OrderRefused, match="cannot be discarded"):
        manager.discard(order)
    assert manager.by_intent[INTENT] is order


async def test_an_adapter_that_refuses_the_send_is_also_refused_a_second_row(
    venue: CountingBroker, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The failed SEND, which the test above no longer reaches.

    Since `OrderManager.submit` began reading the venue's contract terms
    first, a disconnected venue is refused at that read and `place_order` is
    never called -- so the test named for a failed send stopped covering the
    `except BrokerError` branch that handles one, and deleting that branch
    left every test in this suite green.

    This is the case the branch exists for: the terms are readable, the
    adapter accepts the call, and the SEND itself is refused before
    transmission. `failed` is correct because the venue never saw it, and a
    second pass over the same intent is still refused at the guard.
    """
    from app.brokers.base import NotConnected
    from app.oms.state import OrderStatus as S

    async def refusing(request: object) -> object:
        venue.sends.append(request)
        raise NotConnected("the terminal refused the send before transmitting it")

    venue.place_order = refusing  # type: ignore[assignment,method-assign]

    first = await boot(venue, sessions).process(signal(), now=T0)
    assert first.outcome is Outcome.execution_rejected
    assert len(venue.sends) == 1, "the send never reached the adapter"

    rows = await orders_for(sessions)
    assert [S(r.status) for r in rows] == [S.failed]
    assert rows[0].error_code == "BROKER_ERROR", (
        "an adapter refusal is not a contract-terms refusal, and the code must say which"
    )

    second = await boot(venue, sessions).process(signal(), now=T0)
    assert second.outcome is Outcome.duplicate_signal
    assert len(venue.sends) == 1, "a second send was attempted"
    assert len(await orders_for(sessions)) == 1
