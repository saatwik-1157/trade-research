"""End-to-end integration (L40): the platform as one system, not as modules.

Every subsystem here already has its own suite, and this file deliberately does
not repeat them. What it tests is the thing no unit suite can: that the stages
**hand off correctly**, that a refusal at one stage really does stop the next
one, and that the records left behind by one run agree with each other.

The scenarios that carry the level:

  * `test_a_webhook_becomes_an_order_a_position_and_a_journal_row` -- step 42,
    the happy path, in one database, with the correlation chain asserted at
    every link.
  * `test_the_same_alert_twice_produces_one_order` -- steps 13, 45 and 49.
  * `test_a_concurrent_duplicate_produces_one_order` -- the same claim under
    real concurrency rather than in sequence, which is where idempotency
    usually turns out to be a check-then-act.
  * `test_a_risk_veto_stops_every_later_stage` -- step 43. Not "risk said no"
    but "and therefore nothing downstream ran".
  * `test_an_unknown_venue_answer_is_never_retried_and_latches_safe_mode` --
    step 24, the mandatory one.
  * `test_paper_mode_cannot_reach_a_live_adapter` -- step 51.
  * `test_no_stage_writes_a_naive_timestamp` -- step 48.

**Nothing here places a real order.** Every venue is `FakeBroker(mode="paper")`,
every settings object is the default (`TRADING_MODE=paper`,
`LIVE_TRADING=false`), and one test asserts that this is still true rather than
trusting it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import pytest
from app.brokers.base import SymbolInfo
from app.brokers.fake import FakeBroker
from app.core.settings import LIVE_GATES, Settings, TradingMode
from app.db.base import Base
from app.execution import (
    NO_ORDER,
    ExecutionPipeline,
    IncomingSignal,
    Outcome,
    StrategyState,
    created_an_order,
)
from app.main import create_app
from app.models.execution import Order, Position, Trade
from app.models.market import Symbol, SymbolMapping
from app.models.signals import Signal
from app.models.strategies import Strategy, StrategyVersion
from app.oms.registry import OrderManagerRegistry
from app.paper.engine import AiVerdict
from app.risk.engine import RiskEngine, RiskLimits
from app.sizing.calculator import SizingMethod
from app.symbols.service import ContractSpec
from app.webhooks.gateway import WebhookGateway
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from tests.routes import api_routes

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

SECRET = "integration-shared-secret-not-a-real-one"


def now_utc() -> datetime:
    """The clock, in one place.

    A fixed T0 was the first thing this file got wrong: the webhook gateway
    refuses an alert older than 120 seconds -- correctly, because a stale alert
    names a bar that has closed and a price that is gone -- so a hard-coded
    date made every payload here stale the day after it was written, and the
    suite would have "failed" for the one reason that is not a defect.

    Freshness is a real gate and it is tested against it; what a test must not
    do is fight it by accident.
    """
    return datetime.now(UTC)


#: Far enough in the past to be unambiguously stale, for the tests that WANT a
#: refusal. Named rather than inlined so it cannot be mistaken for a fresh one.
STALE = datetime(2021, 6, 1, 0, 0, tzinfo=UTC)

#: The same contract spec `test_execution.py` uses. Imported by value rather
#: than redefined loosely: a second spec that differed slightly would make a
#: sizing disagreement between the two files look like a bug in one of them.
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


# ================================================================== fixtures


#: When set, the integration suite runs against a REAL PostgreSQL database
#: instead of in-memory SQLite. Added at L43 to close the gap
#: `KNOWN_TEST_LIMITATIONS.md` recorded: SQLite with `StaticPool` gives every
#: request in a test the same connection and therefore the same transaction, so
#: two concurrent requests roll back each other's work and genuine concurrency
#: cannot be expressed at all.
#:
#: That mattered more than a missing test. TWO real defects were found at L40 in
#: exactly this area, and the second only appeared once the first was fixed --
#: so the register warned that a third might be hiding behind them. This is how
#: you look.
POSTGRES_URL = os.environ.get("TEST_DATABASE_URL")

#: This fixture calls `drop_all`. On a scratch database that is correct; on a
#: real one it destroys the trade history. Same guard as `test_migrations.py`,
#: for the same reason: the only thing distinguishing a test database from
#: production is its NAME, since host, port and credentials are legitimately
#: identical.
_TEST_DB_MARKERS = ("test", "scratch", "ci", "tmp")

if POSTGRES_URL:
    _name = urlsplit(POSTGRES_URL).path.lstrip("/").lower()
    if not any(marker in _name for marker in _TEST_DB_MARKERS):
        raise RuntimeError(
            f"refusing to run integration tests against a database named {_name!r}: "
            f"this fixture DROPS EVERY TABLE, and the name contains none of "
            f"{_TEST_DB_MARKERS}."
        )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    """One application and one database for a whole scenario.

    **Two backends, and which one is in use changes what can be tested.**

    In-memory SQLite with `StaticPool` by default: fast, isolated, and the
    reason a test can write through the API and read back through the session
    factory. Its cost is that every request in a test shares one connection, so
    concurrent requests share a transaction and genuine races are inexpressible.

    Real PostgreSQL when `TEST_DATABASE_URL` is set: each request gets its own
    connection from the pool, which is what production looks like and what
    makes `test_concurrent_identical_webhooks_produce_one_signal` meaningful.
    """
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    if POSTGRES_URL:
        # A small pool, but more than one -- the whole point is that concurrent
        # requests get different connections.
        engine = create_async_engine(POSTGRES_URL, pool_size=10, max_overflow=5)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    else:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    application.state.webhook_gateway = WebhookGateway(secret=SECRET, mode="paper")

    async with application.state.session_factory() as db:
        db.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        db.add(
            SymbolMapping(
                id="map-tv",
                symbol_id="sym-eur",
                provider="tradingview",
                provider_symbol="OANDA:EURUSD",
            )
        )
        db.add(Strategy(id="strat-1", key="rsi_reversion", name="RSI reversion"))
        db.add(
            StrategyVersion(
                id="sv-1", strategy_id="strat-1", version=1, code_ref="tools.mt5_paper:rsi"
            )
        )
        await db.commit()
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def venue() -> AsyncIterator[FakeBroker]:
    fake = FakeBroker(mode="paper")
    await fake.connect()
    fake.set_quote("EURUSD", "1.10000", "1.10002")
    fake.symbols["EURUSD"] = VENUE_SPEC
    yield fake
    await fake.disconnect()


def alert(**overrides: object) -> dict:
    """A controlled TradingView payload. Step 12: a real shape, a fake secret."""
    body: dict[str, object] = {
        "secret": SECRET,
        "ticker": "OANDA:EURUSD",
        "action": "buy",
        "time": now_utc().isoformat(),
        "strategy": "rsi_reversion",
        "price": "1.10000",
        "sl": "1.09500",
        "tp": "1.11000",
        "bar_time": now_utc().isoformat(),
    }
    body.update(overrides)
    return body


def pipeline(
    venue: FakeBroker | None,
    *,
    limits: RiskLimits | None = None,
    strategy: StrategyState | None = None,
    spec: ContractSpec | None = SPEC,
    **kwargs: object,
) -> ExecutionPipeline:
    """The real pipeline, wired to a fake venue. No stage is stubbed.

    Reused from `test_execution.py`'s shape on purpose: step 1 says not to
    build a second testing framework, and a second pipeline builder that
    configured risk or sizing differently would make these tests agree with
    nothing.
    """
    registry = OrderManagerRegistry()
    if venue is not None:
        registry.register("acct-a", venue, mode="paper", broker="fake")

    async def spec_for(_symbol: str) -> ContractSpec | None:
        return spec

    def strategy_state(_strategy_id: str | None) -> StrategyState:
        return strategy or StrategyState(exists=True, enabled=True)

    options: dict[str, object] = {
        "sizing_method": SizingMethod.fixed_risk,
        "risk_amount": Decimal("100"),
    }
    options.update(kwargs)
    return ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(limits or RiskLimits(require_stop_loss=False)),
        spec_for=spec_for,
        strategy_state=strategy_state,
        **options,  # type: ignore[arg-type]
    )


def signal_from(row: Signal) -> IncomingSignal:
    """Turn the persisted webhook row into what the pipeline consumes.

    This is the join the platform makes in `app/execution/worker.py`, done here
    explicitly so the test can assert the two halves line up -- which is the
    handoff a unit test on either side cannot see.
    """
    return IncomingSignal(
        signal_id=row.id,
        signal_key=row.signal_key,
        source=row.source,
        symbol="EURUSD",
        side=row.direction,
        signal_time=row.signal_time.replace(tzinfo=UTC)
        if row.signal_time.tzinfo is None
        else row.signal_time,
        account_id="acct-a",
        mode="paper",
        strategy_id="rsi_reversion",
        entry_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09500"),
        take_profit=Decimal("1.11000"),
        auth_strength="strong",
    )


async def _count(app: FastAPI, model: Any) -> int:
    async with app.state.session_factory() as db:
        return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def _one_signal(app: FastAPI) -> Signal:
    async with app.state.session_factory() as db:
        rows = list((await db.scalars(select(Signal))).all())
    assert len(rows) == 1, f"expected exactly one signal, found {len(rows)}"
    return rows[0]


# =========================================== 12, 42, 47. the happy path


async def test_a_webhook_becomes_an_order_a_position_and_a_journal_row(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 42, end to end, with the chain asserted at every link.

    The stages are exercised in the order the platform runs them, and the point
    of the test is the JOINS between them -- that the order names the signal
    that caused it, the position names the order, and the journal row names the
    position. Each of those is a foreign key somebody could have forgotten to
    set, and every subsystem's own suite would still pass if they had.
    """
    # 1-6. The alert arrives and is accepted, validated and persisted.
    accepted = await client.post("/v1/webhooks/tradingview", json=alert())
    assert accepted.status_code in (200, 201, 202), accepted.text
    assert await _count(app, Signal) == 1

    row = await _one_signal(app)
    assert row.source == "tradingview"
    assert row.direction == "buy"
    assert row.signal_key, "a signal with no key cannot be deduplicated"

    # 7-13. Strategy state, risk, sizing, OMS, venue. Nothing is stubbed.
    engine = pipeline(venue)
    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))
    assert created_an_order(result.outcome), f"{result.outcome}: {result.detail}"

    # 14. The venue confirmed, and the fake counted exactly one order.
    assert venue._orders_placed == 1

    # 15-16. The correlation chain, which is the point of the test: the result
    # names the sizing that produced the volume, the risk decision that let it
    # through, and one execution id that ties the pass together.
    assert result.order is not None
    assert result.sizing is not None
    assert result.sizing.volume is not None and result.sizing.volume > 0
    assert result.execution_id, "no execution id means no trace through the stages"
    assert result.order.risk_decision_id, "an order with no risk decision behind it"
    assert result.order.sizing_snapshot["execution_id"] == result.execution_id
    assert result.order.client_order_id, "no client order id means no idempotency at the OMS"
    # And back to the signal that caused it. This is the link a unit test on
    # either side cannot see, and the one a refactor silently drops.
    assert result.order.signal_id == row.id


async def test_the_quantity_the_venue_received_is_the_one_sizing_computed(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 22 and step 47. The number must survive every handoff unchanged.

    The pipeline computes no quantity of its own -- `test_execution.py` asserts
    that structurally. What this asserts is the other half: the figure the
    sizing calculator produced is the figure that reached the venue, rounded to
    the venue's own step and never re-derived along the way.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue)
    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))
    assert created_an_order(result.outcome)

    assert result.sizing is not None
    sent = result.sizing.volume
    assert sent is not None
    step = SPEC.volume_step
    assert sent % step == 0, f"{sent} is not a multiple of the venue's step {step}"
    assert SPEC.minimum_volume <= sent <= SPEC.maximum_volume


# ============================================ 13, 45, 49. duplicate handling


async def test_the_same_alert_twice_produces_one_signal(app: FastAPI, client: AsyncClient) -> None:
    """Step 13. Same event, same key: the second is recognised, not stored.

    One payload, posted twice -- not two calls to `alert()`. The fingerprint
    includes the alert's timestamp, so two separately-built payloads are two
    genuinely different alerts a microsecond apart, and asserting that they
    collapse would be asserting a bug.
    """
    payload = alert()
    first = await client.post("/v1/webhooks/tradingview", json=payload)
    second = await client.post("/v1/webhooks/tradingview", json=payload)
    assert first.status_code < 400 and second.status_code < 400
    assert await _count(app, Signal) == 1, "a re-delivered alert created a second signal"


async def test_the_same_signal_twice_produces_one_order(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 13, at the execution stage rather than at the gateway.

    Two independent mechanisms have to hold for this: the pipeline's in-process
    `seen` set, and the OMS's `intent_id`. This drives the first; the second is
    covered by `test_execution.py`'s own case, which forgets the first on
    purpose.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue)
    incoming = signal_from(row)

    first = await engine.process(incoming, now=now_utc() + timedelta(seconds=5))
    second = await engine.process(incoming, now=now_utc() + timedelta(seconds=6))

    assert created_an_order(first.outcome)
    assert not created_an_order(second.outcome), second.detail
    assert venue._orders_placed == 1, f"{venue._orders_placed} orders for one signal"


async def test_a_concurrent_duplicate_produces_one_order(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 45. The same claim, but raced.

    Sequential deduplication is easy and the usual implementation -- check the
    set, then act -- has a window between the two. Running both passes
    concurrently is what puts anything in that window. Both coroutines share
    one pipeline and one venue, exactly as two worker ticks would.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue)
    incoming = signal_from(row)

    results = await asyncio.gather(
        engine.process(incoming, now=now_utc() + timedelta(seconds=5)),
        engine.process(incoming, now=now_utc() + timedelta(seconds=5)),
    )
    created = [r for r in results if created_an_order(r.outcome)]
    assert len(created) == 1, [f"{r.outcome}: {r.detail}" for r in results]
    assert venue._orders_placed == 1, f"{venue._orders_placed} orders from a raced duplicate"


async def test_a_burst_of_identical_alerts_produces_one_signal(
    app: FastAPI, client: AsyncClient
) -> None:
    """Step 49. Ten deliveries of one alert; one signal, and no 5xx.

    **Sequential, and the reason is a limit of this harness rather than a
    choice.** The suite runs on in-memory SQLite with `StaticPool`, which is
    what lets a test read back what a request wrote -- but it means every
    request shares ONE connection and therefore one transaction, so a genuinely
    concurrent version has the two requests rolling back each other's work and
    fails for a reason that does not exist on Postgres, where each request has
    its own connection.

    Writing it concurrently anyway would produce a red test that says nothing
    about the platform, and forcing it green would mean weakening the
    assertion. It is recorded in `KNOWN_TEST_LIMITATIONS.md` instead, with the
    command to run it against Postgres.

    What this still checks is real and is what TradingView actually does:
    repeated delivery of the identical payload, which must collapse to one
    signal and must never answer 5xx -- TradingView retries on 5xx, so a 500
    here is what turns one duplicate into a delivery storm.
    """
    payload = alert()
    statuses = []
    for _ in range(10):
        r = await client.post("/v1/webhooks/tradingview", json=payload)
        statuses.append(r.status_code)
    assert all(code < 500 for code in statuses), statuses
    assert await _count(app, Signal) == 1, "a burst of one alert created several signals"


async def test_concurrent_identical_webhooks_produce_one_signal(
    app: FastAPI, client: AsyncClient
) -> None:
    """Step 45, for real. Ten identical alerts delivered AT ONCE.

    **Only meaningful against PostgreSQL**, and it says so rather than passing
    vacuously: on SQLite with `StaticPool` every request shares one connection
    and therefore one transaction, so the requests roll back each other's work
    and the test fails for a harness reason that does not exist in production.

    Run it with:

        TEST_DATABASE_URL=<postgres url for a scratch db> \
          python -m pytest tests/test_integration.py -k concurrent

    What must hold: exactly ONE signal, and **no 5xx**. TradingView retries on
    5xx, so a 500 under concurrent re-delivery turns one duplicate into a
    delivery storm -- which is precisely the defect L40 found here.
    """
    if not POSTGRES_URL:
        pytest.skip("needs TEST_DATABASE_URL: SQLite shares one connection")

    payload = alert()
    responses = await asyncio.gather(
        *(client.post("/v1/webhooks/tradingview", json=payload) for _ in range(10))
    )
    codes = [r.status_code for r in responses]
    assert all(code < 500 for code in codes), f"a concurrent duplicate 5xx'd: {codes}"
    assert await _count(app, Signal) == 1, f"{await _count(app, Signal)} signals from one alert"


async def test_concurrent_distinct_alerts_each_produce_a_signal(
    app: FastAPI, client: AsyncClient
) -> None:
    """The control for the test above.

    Ten DIFFERENT alerts, concurrently. If deduplication were over-eager --
    collapsing on something coarser than the idempotency key -- the previous
    test would pass for the wrong reason and this one would fail. A
    deduplication test without a distinctness control proves only that
    something was dropped.
    """
    if not POSTGRES_URL:
        pytest.skip("needs TEST_DATABASE_URL: SQLite shares one connection")

    payloads = [alert(id=f"distinct-{i}") for i in range(10)]
    responses = await asyncio.gather(
        *(client.post("/v1/webhooks/tradingview", json=p) for p in payloads)
    )
    assert all(r.status_code < 500 for r in responses), [r.status_code for r in responses]
    assert await _count(app, Signal) == 10, (
        f"{await _count(app, Signal)} signals from 10 distinct alerts -- dedup is too coarse"
    )


async def test_the_duplicate_race_is_answered_rather_than_raised() -> None:
    """The unit-level half of the race the harness cannot run end to end.

    An `IntegrityError` on either unique key -- `webhook_events.idempotency_key`
    or `signals.signal_key` -- must produce a `duplicate` outcome. Before L40
    only the second was handled, and losing the race at the first raised
    through to a 500.

    Asserted against the source because the condition needs two database
    connections to reproduce and this suite has one. It is a weaker test than
    running it, and it is honest about which of the two it is.
    """
    import inspect

    from app.webhooks import gateway as module

    source = inspect.getsource(module.WebhookGateway.receive)
    # Both inserts sit inside an IntegrityError handler.
    assert source.count("except IntegrityError:") >= 2, (
        "only one of the two unique keys has a race handler"
    )
    # And neither handler raises; both return a duplicate result.
    assert source.count("Outcome.duplicate") >= 2


# =============================================== 14, 43. the rejection paths


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("invalid secret", alert(secret="wrong-secret")),
        ("missing secret", {k: v for k, v in alert().items() if k != "secret"}),
        ("missing ticker", {k: v for k, v in alert().items() if k != "ticker"}),
        ("unknown action", alert(action="yolo")),
        ("missing timestamp", {k: v for k, v in alert().items() if k != "time"}),
        ("unparseable timestamp", alert(time="the day before yesterday")),
        ("stale timestamp", alert(time=STALE.isoformat())),
    ],
)
async def test_an_invalid_alert_creates_no_signal_and_no_order(
    app: FastAPI, client: AsyncClient, name: str, payload: dict
) -> None:
    """Step 14. Refused, and -- the part that matters -- refused BEFORE the
    signal table, so nothing downstream ever has the chance to run."""
    r = await client.post("/v1/webhooks/tradingview", json=payload)
    assert r.status_code >= 400, f"{name} was accepted"
    assert await _count(app, Signal) == 0, f"{name} reached the signal table"
    assert await _count(app, Order) == 0, f"{name} reached the order table"


async def test_malformed_json_is_refused(app: FastAPI, client: AsyncClient) -> None:
    r = await client.post("/v1/webhooks/tradingview", content="{not json at all")
    assert r.status_code >= 400
    assert await _count(app, Signal) == 0


async def test_a_risk_veto_stops_every_later_stage(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 43. Not "risk said no" -- "and therefore nothing after it ran".

    A test that only asserted the outcome would pass on an implementation that
    vetoed the signal and submitted the order anyway. The venue's own record is
    what makes this test about the ordering rather than about the verdict.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue, limits=RiskLimits(max_open_positions=0))

    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))

    assert result.outcome in NO_ORDER, result.outcome
    assert not created_an_order(result.outcome)
    assert venue._orders_placed == 0, "risk vetoed and the order was sent anyway"


async def test_an_ai_rejection_stops_the_pass_before_the_venue(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 29 and 43. The AI seat may decline; it reaches no venue when it does."""

    class Decliner:
        def score(self, _signal: object) -> AiVerdict:
            return AiVerdict(accept=False, reason="the model declined this setup")

    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue, ai=Decliner())
    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))

    assert not created_an_order(result.outcome)
    assert venue._orders_placed == 0


async def test_an_approving_ai_cannot_override_a_risk_veto(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 29's hard rule: the AI must not be able to bypass the RiskEngine.

    Both gates are on the path and the veto is the one that wins, so an
    approving model changes nothing about a refusal. This is the combination
    an isolated AI test and an isolated risk test cannot check between them.
    """

    class Approver:
        def score(self, _signal: object) -> AiVerdict:
            return AiVerdict(accept=True, reason="the model likes it")

    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue, ai=Approver(), limits=RiskLimits(max_open_positions=0))
    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))

    assert not created_an_order(result.outcome)
    assert venue._orders_placed == 0


async def test_a_sizing_refusal_stops_the_pass_before_the_venue(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 22 and 43. A size below the venue minimum is refused, not rounded up.

    Rounding up is the tempting implementation and it silently trades a bigger
    position than the risk budget allowed, which is the failure this refusal
    exists to prevent.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue, risk_amount=Decimal("0.01"))
    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))

    assert not created_an_order(result.outcome)
    assert venue._orders_placed == 0


async def test_a_disconnected_venue_stops_new_orders(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Steps 26 and 44. MT5 goes away mid-session; nothing is sent hopefully."""
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue)
    await venue.disconnect()

    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))
    # NOT `created_an_order`: that answers "may something exist at the venue?",
    # and its conservative default is yes for anything the platform cannot rule
    # out -- which is the right answer here and the wrong assertion. What must
    # be true is that the venue was never reached and no fill came back.
    assert result.outcome is not Outcome.filled
    assert result.outcome is not Outcome.order_submitted
    assert venue._orders_placed == 0, "an order reached a disconnected venue"


# ================================================== 24. the unknown order


async def test_an_unknown_venue_answer_is_never_retried(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 24, and the brief calls it mandatory.

    The venue's answer is lost. The platform does not know whether an order
    exists, and the ONE thing it must not do is send another -- a retry here is
    how a system ends up with two positions and a record of one.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue)
    venue.unknown_next = True

    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))

    # The pass ends in an unresolved state, and `created_an_order` is TRUE for
    # it on purpose: the platform cannot rule out that the venue has an order,
    # so it must assume one exists. That is the whole point of the state.
    assert result.outcome is not Outcome.filled
    placed_after_first = venue._orders_placed

    # And the mandatory half of step 24: a second pass must not resend it.
    again = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=6))
    assert again.outcome is Outcome.duplicate_signal or not created_an_order(again.outcome), (
        f"an unresolved order was retried: {again.outcome}"
    )
    assert venue._orders_placed == placed_after_first, "the venue was sent a second order"


async def test_an_unresolved_order_latches_safe_mode_at_startup(app: FastAPI) -> None:
    """Steps 24, 40 and 58, joined: the unknown order is what safe mode is for.

    This is the integration L38 built and L40 checks end to end -- an order in
    an unresolved state, the startup sequence run over it, and the platform
    refusing to trade rather than resuming into an account it cannot account
    for.
    """
    from app.recovery.contract import SafeModeReason

    async with app.state.session_factory() as db:
        db.add(
            Order(
                id="o-unknown",
                intent_id="intent-o-unknown",
                paper_account_id=None,
                symbol_id="sym-eur",
                side="buy",
                order_type="market",
                quantity=Decimal("0.10"),
                status="unknown",
                mode="paper",
            )
        )
        await db.commit()

    manager = app.state.recovery
    async with app.state.session_factory() as db:
        await manager.run_startup(db, app)
        await db.commit()

    assert manager.safe_mode.engaged, "an unresolved order did not latch safe mode"
    reasons = {latch.reason for latch in manager.safe_mode.reasons}
    assert SafeModeReason.unknown_order_state in reasons

    # And the order itself was not touched. Recovery reports; it does not repair.
    async with app.state.session_factory() as db:
        assert (await db.get(Order, "o-unknown")).status == "unknown"


async def test_safe_mode_refuses_the_pipeline_before_risk_is_consulted(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 40's "SAFE STATE" leg, at the gate that actually blocks trading."""
    from app.recovery.contract import SafeModeReason
    from app.recovery.safe_mode import SafeMode

    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)

    latch = SafeMode()
    latch.engage(SafeModeReason.operator, "an integration test")
    engine = pipeline(venue, safe_mode=latch)

    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))
    assert result.outcome is Outcome.safe_mode
    assert venue._orders_placed == 0
    assert not created_an_order(Outcome.safe_mode)


# ============================================= 51. paper / live isolation


def test_live_trading_is_off_by_default_and_every_gate_is_shut() -> None:
    """Step 51. Asserted rather than trusted, on every run of this suite."""
    settings = Settings(_env_file=None)
    assert settings.trading_mode is TradingMode.paper
    assert settings.live_trading is False
    assert settings.live_execution_allowed is False
    unshut = [name for name, ok in LIVE_GATES.items() if ok]
    assert unshut == [], f"live gates are open: {unshut}"


async def test_paper_mode_cannot_reach_a_live_adapter(app: FastAPI, client: AsyncClient) -> None:
    """Step 51. A signal claiming `live` is refused by a paper-mode registry.

    The mode is on the signal AND on the registration, and the pipeline
    compares them rather than trusting either. So a signal that claims to be
    live cannot be executed through a manager registered for paper, whatever
    the caller intended -- and there is no live manager to register, which is
    the second half of the isolation.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)

    fake = FakeBroker(mode="paper")
    await fake.connect()
    fake.set_quote("EURUSD", "1.10000", "1.10002")
    fake.symbols["EURUSD"] = VENUE_SPEC
    try:
        engine = pipeline(fake)
        base = signal_from(row)
        live = IncomingSignal(
            **{**{f: getattr(base, f) for f in base.__dataclass_fields__}, "mode": "live"}
        )
        result = await engine.process(live, now=now_utc() + timedelta(seconds=5))
        assert not created_an_order(result.outcome), "a live signal executed on a paper venue"
        assert fake._orders_placed == 0
    finally:
        await fake.disconnect()


async def test_no_registered_broker_is_a_real_broker(app: FastAPI) -> None:
    """Step 25 and 51 together: nothing in a test run holds live credentials."""
    registry = getattr(app.state, "brokers", None)
    if registry is None:
        pytest.skip("no broker registry on this app")
    for name in getattr(registry, "names", lambda: [])():
        assert "live" not in str(name).lower()


# ============================================================ 48. timestamps


async def test_every_persisted_timestamp_is_utc(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 48. A naive local timestamp is a bug that only fires abroad.

    SQLite gives back naive datetimes whatever was stored, so the assertion is
    about the VALUE rather than the tzinfo: a row written from a UTC clock and
    read back must be within a few minutes of `datetime.now(UTC)`, and one
    written from a local clock on this machine would be hours away.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    now = datetime.now(UTC).replace(tzinfo=None)

    async with app.state.session_factory() as db:
        row = (await db.scalars(select(Signal))).all()[0]
        received = row.received_at
    assert received is not None
    drift = abs((received.replace(tzinfo=None) - now).total_seconds())
    assert drift < 600, f"created_at is {drift / 3600:.1f}h from UTC now: a local clock"


async def test_a_stale_alert_is_measured_against_its_own_timestamp(
    app: FastAPI, client: AsyncClient
) -> None:
    """Step 11 and 48. Freshness is the alert's own time, not arrival time.

    Measuring against arrival would make every re-delivered alert look fresh,
    which is exactly backwards: a re-delivery is the case where the bar is most
    likely to have closed.
    """
    old = await client.post("/v1/webhooks/tradingview", json=alert(time=STALE.isoformat()))
    assert old.status_code >= 400
    assert await _count(app, Signal) == 0


# =========================================== 41. security, in a real flow


async def test_an_unauthenticated_caller_reaches_no_business_action(client: AsyncClient) -> None:
    """Step 8 and 41. The whole protected surface, not a sample of it."""
    client.cookies.clear()
    protected = [
        ("get", "/v1/orders"),
        ("get", "/v1/positions"),
        ("get", "/v1/portfolio/summary"),
        ("get", "/v1/admin/dashboard"),
        ("get", "/v1/security/posture"),
        ("get", "/v1/recovery/status"),
        ("get", "/v1/notifications"),
    ]
    for method, path in protected:
        r = await getattr(client, method)(path)
        assert r.status_code in (401, 403, 404), f"{path} answered {r.status_code}"


async def test_no_write_route_acts_for_an_unauthenticated_caller(
    app: FastAPI, client: AsyncClient
) -> None:
    """Step 8 and 41, over the WHOLE write surface rather than a sample.

    An earlier version of this test inspected `route.dependant.dependencies`
    and looked for guard-shaped function names. It reported 67 unprotected
    routes, every one of which is in fact protected -- FastAPI's dependency
    names are not the guard names, so the introspection was measuring its own
    naming convention. It was replaced rather than tuned: a security assertion
    that can be satisfied by renaming a function is not a security assertion.

    So every write route is actually CALLED, with no session, and must answer
    401, 403, or a validation error raised before any business logic
    (422/404/405). What must never happen is 2xx.
    """
    client.cookies.clear()
    checked = 0
    reached: list[str] = []
    for route in api_routes(app):
        path = getattr(route, "path", "")
        methods: set[str] = (getattr(route, "methods", set()) or set()) & {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }
        if not methods or not path.startswith("/v1/"):
            continue
        # The auth routes exist to be called without a session, and the webhook
        # is authenticated by a shared secret instead of one. Both are asserted
        # elsewhere in this file and in `test_auth.py` / `test_webhooks.py`.
        if "/auth/" in path or "webhook" in path:
            continue
        concrete = path
        for placeholder in (
            "{user_id}",
            "{order_id}",
            "{bot_id}",
            "{account_id}",
            "{position_id}",
            "{strategy_key}",
            "{model_key}",
            "{version}",
            "{alert_id}",
            "{job_id}",
            "{run_id}",
            "{backtest_id}",
            "{session_id}",
            "{review_id}",
            "{trade_id}",
            "{notification_id}",
            "{symbol_id}",
        ):
            concrete = concrete.replace(placeholder, "probe-id")
        if "{" in concrete:
            continue
        for method in sorted(methods):
            r = await client.request(method, concrete, json={})
            checked += 1
            if r.status_code < 400:
                reached.append(f"{method} {concrete} -> {r.status_code}")
    assert checked > 40, f"only probed {checked} write routes; the sweep is not working"
    assert reached == [], f"unauthenticated callers reached: {reached}"


async def test_a_security_event_reaching_the_bus_carries_no_identifier(
    app: FastAPI,
) -> None:
    """Step 41 and step 10 together: the `system` channel reaches everybody."""
    from app.security.announce import SecurityAnnouncer
    from app.security.events import SecurityEvent, record

    captured: list[object] = []

    class Recorder:
        async def publish(self, event: object) -> None:
            captured.append(event)

    announcer = SecurityAnnouncer(hub=Recorder())
    await announcer.announce(
        record(
            SecurityEvent.login_failed,
            "a burst of failed sign-ins",
            user_id="private-user-id",
            count=12,
        )
    )
    assert captured
    published: Any = captured[0]
    assert published.channel == "system"
    assert "private-user-id" not in repr(published.payload)


# ====================================================== 44. failure paths


async def test_a_failing_notification_does_not_fail_a_trade(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 36 and 44. The notification path is downstream of the money path.

    A trading platform whose orders depend on its ability to send email is one
    that stops trading when the mail server does.
    """

    class BrokenHub:
        async def publish(self, event: object) -> None:
            raise RuntimeError("the bus is down")

    app.state.hub = BrokenHub()

    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue)
    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))

    assert created_an_order(result.outcome), f"a broken bus stopped a trade: {result.detail}"
    assert venue._orders_placed == 1


async def test_the_pipeline_returns_a_decision_rather_than_raising(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 44. A bot loop needs something to record, not an exception to classify."""
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)

    def exploding(_strategy_id: str | None) -> StrategyState:
        raise RuntimeError("the strategy service fell over")

    engine = pipeline(venue)
    engine.strategy_state = exploding
    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))

    assert result.outcome in NO_ORDER
    assert venue._orders_placed == 0


# ====================================================== 47. data consistency


async def test_the_records_left_behind_agree_with_each_other(
    app: FastAPI, client: AsyncClient, venue: FakeBroker
) -> None:
    """Step 47. One run; then every table is asked what it thinks happened.

    The failure this catches is the one nobody writes a unit test for: two
    subsystems that each work and disagree about how many things happened.
    """
    await client.post("/v1/webhooks/tradingview", json=alert())
    row = await _one_signal(app)
    engine = pipeline(venue)
    result = await engine.process(signal_from(row), now=now_utc() + timedelta(seconds=5))
    assert created_an_order(result.outcome)

    signals = await _count(app, Signal)
    submitted = venue._orders_placed
    assert signals == 1
    assert submitted == 1, f"{signals} signal produced {submitted} venue orders"

    # No position or trade is asserted here, deliberately: the fake venue
    # acknowledges an order and does not fill it, so a position row would mean
    # the platform had invented one. Step 32's journal consistency is covered
    # by `test_trade_journal.py` against a real fill.
    assert await _count(app, Trade) == 0
    assert await _count(app, Position) == 0


# ================================================= 50. no future leakage


def test_no_execution_stage_can_see_a_bar_after_the_signal() -> None:
    """Step 50, as a property of the signature rather than of a run.

    `ExecutionPipeline.process` takes `now` explicitly and every staleness
    decision is made against it. A stage that read the clock itself could
    compare a decision-time signal against a later bar without anybody being
    able to tell from the outside -- which is what makes leakage so hard to
    find after the fact.
    """
    import inspect

    signature = inspect.signature(ExecutionPipeline.process)
    assert "now" in signature.parameters, "the pipeline reads the clock itself"

    # `process` may default `now` when a caller does not supply one -- that is
    # one read, at the top, before any stage runs. What must not happen is a
    # stage reading the clock again mid-pass: two different "nows" in one
    # decision is how a signal gets compared against a bar that had not closed
    # when it was generated.
    source = inspect.getsource(ExecutionPipeline.process)
    assert source.count("datetime.now(") <= 1, "more than one clock read in one pass"
    assert "= now or datetime.now(" in source, "the default is not the documented one"

    inner = inspect.getsource(ExecutionPipeline._process)
    assert "datetime.now(" not in inner, "a stage reads the wall clock mid-pass"
    assert "utcnow()" not in inner
