"""An alert reaches an order, or says why not. **The L45 F-1 regression.**

F-1 was not a safety defect — it failed closed — but it changed what "verified
end to end" had ever meant. `app/webhooks/gateway.py` never wrote `account_id`
into a signal's `meta`, so `app/main.py::_to_incoming_signal` returned None for
every alert this platform had ever received and the worker retired each one as
`signal_not_executable`. Measured on the deployed database: **all 118 signals
sat at `status='new'`.**

L40 verified the webhook half and L41 verified the pipeline half. Nothing ever
joined them through the worker, and the join was the gap.

**Five links were missing, not one.** Measured before the fix, by feeding the
gateway's own meta to the real pipeline built with `app/main.py`'s arguments:

    gateway meta as written        -> _to_incoming_signal returns None
    + account_id only              -> signal_invalid  "no symbol on the signal"
    + internal_symbol              -> sizing_refused  "stop distance is required"
    + strategy_id                  -> sizing_refused  "stop distance is required"
    + a bracket                    -> sizing_refused  "fixed_risk needs a positive
                                                      risk_amount"

Four of those are facts the platform already held and simply never recorded.
The fifth — the bracket — is a policy the platform does not have, and this file
asserts that it is refused precisely rather than papered over. See
`SIGNAL_ROUTING.md`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.brokers.base import SymbolInfo
from app.db.base import Base
from app.execution.routing import NoRoute, Route, route_for
from sqlalchemy import select
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

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _fixtures(db: AsyncSession) -> None:
    """A user, an account, a strategy and one version. No bot yet."""
    from app.auth.models import Role, User
    from app.models.accounts import PaperAccount
    from app.models.strategies import Strategy, StrategyVersion

    db.add(User(id="u1", email="a@b.io", password_hash="x", role=str(Role.admin)))
    db.add(
        PaperAccount(
            id="acct-a",
            user_id="u1",
            name="paper",
            currency="USD",
            starting_balance=Decimal("100000"),
            balance=Decimal("100000"),
            equity=Decimal("100000"),
        )
    )
    db.add(Strategy(id="strat-1", key="sma_cross", name="SMA cross"))
    await db.flush()
    db.add(
        StrategyVersion(
            id="ver-1", strategy_id="strat-1", version=1, code_ref="ref-1", status="validated"
        )
    )
    await db.flush()


async def _bot(db: AsyncSession, **over: object):  # noqa: ANN201
    from app.models.bots import Bot

    base: dict[str, object] = {
        "id": "bot-1",
        "user_id": "u1",
        "name": "tv bot",
        "mode": "paper",
        "strategy_version_id": "ver-1",
        "paper_account_id": "acct-a",
        "is_enabled": True,
        "is_disabled": False,
        "max_risk_per_trade": Decimal("100"),
    }
    base.update(over)
    bot = Bot(**base)
    db.add(bot)
    await db.flush()
    return bot


# ==================================================== 1. the routing decision


async def test_an_enabled_bot_supplies_the_account_and_the_risk_budget(
    db: AsyncSession,
) -> None:
    """The join. An alert names a strategy; the bot names the account."""
    await _fixtures(db)
    await _bot(db)

    routed = await route_for(db, strategy_version_id="ver-1", mode="paper")
    assert isinstance(routed, Route)
    assert routed.account_id == "acct-a"
    assert routed.bot_id == "bot-1"
    assert routed.risk_amount == Decimal("100")


async def test_an_alert_with_no_strategy_is_recorded_and_not_executed(
    db: AsyncSession,
) -> None:
    """The documented 'external alert' case, stated rather than implied."""
    await _fixtures(db)
    routed = await route_for(db, strategy_version_id=None, mode="paper")
    assert isinstance(routed, NoRoute)
    assert "names no strategy" in routed.reason


async def test_no_enabled_bot_means_no_route_and_a_reason(db: AsyncSession) -> None:
    await _fixtures(db)
    await _bot(db, is_enabled=False)
    routed = await route_for(db, strategy_version_id="ver-1", mode="paper")
    assert isinstance(routed, NoRoute)
    assert "no enabled bot" in routed.reason


async def test_a_disabled_bot_does_not_route(db: AsyncSession) -> None:
    """`is_disabled` is the stronger statement an operator makes after an
    incident, and it must not be overridden by `is_enabled` being true."""
    await _fixtures(db)
    await _bot(db, is_disabled=True)
    routed = await route_for(db, strategy_version_id="ver-1", mode="paper")
    assert isinstance(routed, NoRoute)


async def test_two_enabled_bots_are_refused_never_resolved(db: AsyncSession) -> None:
    """**The case that must not become "pick the first".**

    Two enabled bots on one strategy version is a configuration nobody has
    finished. Choosing would send an order to an account nobody selected — the
    same rule `OrderManagerRegistry.get` already states for adapters.
    """
    await _fixtures(db)
    await _bot(db)
    await _bot(db, id="bot-2", name="second")

    routed = await route_for(db, strategy_version_id="ver-1", mode="paper")
    assert isinstance(routed, NoRoute)
    assert "refusing to choose one" in routed.reason
    assert "bot-1" in routed.reason and "bot-2" in routed.reason


async def test_a_bot_in_another_mode_does_not_claim_the_signal(db: AsyncSession) -> None:
    """A demo bot must not execute a paper signal, or the reverse."""
    await _fixtures(db)
    await _bot(db, mode="demo", paper_account_id=None, broker_account_id=None)
    routed = await route_for(db, strategy_version_id="ver-1", mode="paper")
    assert isinstance(routed, NoRoute)


async def test_a_bot_with_no_account_routes_nowhere(db: AsyncSession) -> None:
    await _fixtures(db)
    await _bot(db, paper_account_id=None)
    routed = await route_for(db, strategy_version_id="ver-1", mode="paper")
    assert isinstance(routed, NoRoute)
    assert "names no account" in routed.reason


# ============================================ 2. the alert cannot name the account


def test_no_alert_field_can_set_the_account_or_the_risk_budget() -> None:
    """The reason routing is resolved from `bots` and not from the payload.

    A sender that could name its own account could name somebody else's, and
    one that could set its own risk budget could set any number. Neither is in
    the alert schema's vocabulary at all, which is what makes it structural
    rather than a validation rule that could be relaxed.
    """
    from app.webhooks import schema

    vocabulary = set(schema.ADVISORY_KEYS) | {
        "ticker",
        "symbol",
        "action",
        "side",
        "time",
        "timestamp",
        "exchange",
        "timeframe",
        "interval",
        "price",
        "close",
        "strategy",
        "strategy_version",
        "id",
        "alert_id",
        "signal_id",
    }
    for forbidden in ("account_id", "account", "bot_id", "risk_amount"):
        assert forbidden not in vocabulary, (
            f"an alert can name {forbidden!r}, so a payload could choose where its "
            "order lands or how much it risks"
        )

    # And an unknown key lands in `extra`, which nothing executes from.
    alert = schema.parse_alert(
        {
            "ticker": "OANDA:EURUSD",
            "action": "buy",
            "time": NOW.isoformat(),
            "account_id": "somebody-elses-account",
            "risk_amount": "999999",
        }
    )
    assert alert.extra["account_id"] == "somebody-elses-account"
    assert not hasattr(alert, "account_id")


# ================================================ 3. the routed signal executes


async def test_a_routed_signal_reaches_an_order(db: AsyncSession) -> None:
    """End to end, through the REAL translation and the REAL pipeline.

    `_to_incoming_signal` is `app/main.py`'s, unmodified, and the pipeline is
    built with the arguments `app/main.py` uses. The only thing supplied here
    that a deployment must also supply is the bracket — see the next test.
    """
    from types import SimpleNamespace

    from app.brokers.fake import FakeBroker
    from app.execution.pipeline import ExecutionPipeline, StrategyState
    from app.main import _to_incoming_signal
    from app.oms.registry import OrderManagerRegistry
    from app.risk.engine import RiskEngine, RiskLimits
    from app.symbols.service import ContractSpec

    await _fixtures(db)
    await _bot(db)
    routed = await route_for(db, strategy_version_id="ver-1", mode="paper")
    assert isinstance(routed, Route)

    # The meta the gateway now writes, plus the platform bracket.
    row = SimpleNamespace(
        id="sig-1",
        signal_key="tv:EURUSD:buy:1",
        source="tradingview",
        direction="buy",
        signal_time=NOW.replace(tzinfo=None),
        mode="paper",
        auth_strength="strong",
        meta={
            "price_reported": "1.10000",
            "internal_symbol": "EURUSD",
            "strategy_id": "sma_cross",
            "advisory_ignored": {"sl": "9.99", "qty": "500"},
            "account_id": routed.account_id,
            "bot_id": routed.bot_id,
            "risk_amount": str(routed.risk_amount),
            "stop_loss": "1.09500",
            "take_profit": "1.11000",
        },
    )

    incoming = _to_incoming_signal(row)
    assert incoming is not None
    assert incoming.account_id == "acct-a"
    assert incoming.risk_amount == Decimal("100")
    assert incoming.symbol == "EURUSD"
    assert incoming.strategy_id == "sma_cross"
    # The alert's own suggestions are carried as evidence and are NOT the
    # numbers the platform acts on.
    assert incoming.advisory == {"sl": "9.99", "qty": "500"}
    assert incoming.stop_loss == Decimal("1.09500")
    assert not hasattr(incoming, "quantity")

    venue = FakeBroker(mode="paper")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    venue.symbols["EURUSD"] = VENUE_SPEC
    registry = OrderManagerRegistry()
    registry.register("acct-a", venue, mode="paper", broker="fake")

    spec = ContractSpec(
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

    async def spec_for(symbol: str) -> ContractSpec | None:
        return spec if symbol == "EURUSD" else None

    # app/main.py's arguments: NO risk_amount and NO equity on the pipeline.
    # The bot's budget is what makes sizing possible, and that is the point.
    pipeline = ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(RiskLimits()),
        spec_for=spec_for,
        strategy_state=lambda _k: StrategyState(exists=True, enabled=True),
    )
    result = await pipeline.process(incoming, now=NOW)

    assert result.outcome.value in ("filled", "order_submitted"), result.detail
    assert result.created_order


async def test_the_bots_budget_is_what_makes_sizing_possible(db: AsyncSession) -> None:
    """The same signal with no bot budget is refused, precisely.

    This is the assertion that shows the fix is load-bearing rather than
    incidental: `app/main.py` builds the pipeline with no `risk_amount`, so
    without the bot's figure `fixed_risk` sizing has nothing to size against.
    """
    from app.brokers.fake import FakeBroker
    from app.execution.pipeline import ExecutionPipeline, IncomingSignal, StrategyState
    from app.oms.registry import OrderManagerRegistry
    from app.risk.engine import RiskEngine, RiskLimits
    from app.symbols.service import ContractSpec

    venue = FakeBroker(mode="paper")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    venue.symbols["EURUSD"] = VENUE_SPEC
    registry = OrderManagerRegistry()
    registry.register("acct-a", venue, mode="paper", broker="fake")
    spec = ContractSpec(
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

    async def spec_for(_s: str) -> ContractSpec | None:
        return spec

    pipeline = ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(RiskLimits()),
        spec_for=spec_for,
        strategy_state=lambda _k: StrategyState(exists=True, enabled=True),
    )
    base = dict(
        signal_id="sig-2",
        signal_key="tv:EURUSD:buy:2",
        source="tradingview",
        symbol="EURUSD",
        side="buy",
        signal_time=NOW,
        account_id="acct-a",
        mode="paper",
        strategy_id="sma_cross",
        entry_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09500"),
        take_profit=Decimal("1.11000"),
        auth_strength="strong",
    )

    without = await pipeline.process(IncomingSignal(**base), now=NOW)  # type: ignore[arg-type]
    assert without.outcome.value == "sizing_refused"
    assert "risk_amount" in without.detail

    withbudget = await pipeline.process(
        IncomingSignal(**{**base, "signal_key": "tv:EURUSD:buy:3", "risk_amount": Decimal("100")}),  # type: ignore[arg-type]
        now=NOW,
    )
    assert withbudget.outcome.value in ("filled", "order_submitted"), withbudget.detail


async def test_a_signal_with_no_bracket_is_refused_precisely(db: AsyncSession) -> None:
    """**The one link the platform still does not have, asserted as open.**

    `fixed_risk` sizing needs a stop distance and does not invent one; the
    bracket an alert suggests is deliberately quarantined under
    `advisory_ignored`. So a TradingView alert cannot produce an order until a
    bracket policy exists, and that is a trading decision requiring approval —
    not something this fix should have chosen.

    The refusal is `sizing_refused`, which retires the signal rather than
    parking it, so nothing loops.
    """
    from app.brokers.fake import FakeBroker
    from app.execution.pipeline import ExecutionPipeline, IncomingSignal, StrategyState
    from app.execution.worker import status_for
    from app.oms.registry import OrderManagerRegistry
    from app.risk.engine import RiskEngine, RiskLimits
    from app.symbols.service import ContractSpec

    venue = FakeBroker(mode="paper")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    venue.symbols["EURUSD"] = VENUE_SPEC
    registry = OrderManagerRegistry()
    registry.register("acct-a", venue, mode="paper", broker="fake")
    spec = ContractSpec(
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

    async def spec_for(_s: str) -> ContractSpec | None:
        return spec

    pipeline = ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(RiskLimits()),
        spec_for=spec_for,
        strategy_state=lambda _k: StrategyState(exists=True, enabled=True),
    )
    result = await pipeline.process(
        IncomingSignal(
            signal_id="sig-4",
            signal_key="tv:EURUSD:buy:4",
            source="tradingview",
            symbol="EURUSD",
            side="buy",
            signal_time=NOW,
            account_id="acct-a",
            mode="paper",
            strategy_id="sma_cross",
            entry_price=Decimal("1.10000"),
            stop_loss=None,
            take_profit=None,
            auth_strength="strong",
            risk_amount=Decimal("100"),
        ),
        now=NOW,
    )
    assert result.outcome.value == "sizing_refused"
    assert "stop distance" in result.detail
    assert not result.created_order
    # Retired, not parked: a signal that loops forever is the failure mode a
    # partial fix would have introduced.
    assert status_for(result.outcome) == "vetoed"


# ================================ L56: strategy quarantine, through the real gate


async def test_a_switched_off_strategy_refuses_new_signals(db: AsyncSession) -> None:
    """**L56's quarantine, using the column that has existed since L05.**

    `_strategy_state` returned `enabled=known` — a strategy that existed could
    never be switched off, so the pipeline's own `strategy_disabled` gate could
    not fire for any registered strategy. `strategies.is_active` was never read.
    """
    from types import SimpleNamespace

    from app.main import _strategy_state
    from app.models.strategies import Strategy

    await _fixtures(db)

    class _Registry:
        def keys(self) -> list[str]:
            return ["sma_cross"]

    app = SimpleNamespace(
        state=SimpleNamespace(
            strategy_engine=SimpleNamespace(registry=_Registry()),
            session_factory=lambda: _Session(db),
        )
    )
    resolve = _strategy_state(app)  # type: ignore[arg-type]

    active = await resolve("sma_cross")
    assert active.exists is True
    assert active.enabled is True

    row = await db.scalar(select(Strategy).where(Strategy.key == "sma_cross"))
    assert row is not None
    row.is_active = False
    await db.flush()

    quarantined = await resolve("sma_cross")
    assert quarantined.exists is True
    assert quarantined.enabled is False
    assert "switched off" in quarantined.reason
    # And it says what quarantine does NOT do.
    assert "Open positions are unaffected" in quarantined.reason


async def test_an_unknown_strategy_is_still_refused_for_its_own_reason(
    db: AsyncSession,
) -> None:
    """Quarantine must not blur into "never heard of it". They are different
    faults with different fixes."""
    from types import SimpleNamespace

    from app.main import _strategy_state

    await _fixtures(db)

    class _Registry:
        def keys(self) -> list[str]:
            return ["sma_cross"]

    app = SimpleNamespace(
        state=SimpleNamespace(
            strategy_engine=SimpleNamespace(registry=_Registry()),
            session_factory=lambda: _Session(db),
        )
    )
    state = await _strategy_state(app)("no_such_strategy")  # type: ignore[arg-type]
    assert state.exists is False
    assert "is registered" in state.reason


async def test_an_unreadable_active_flag_leaves_the_strategy_DISABLED() -> None:
    """**Fail closed.** An unreadable quarantine flag must not be an open gate.

    This is the direction that matters: a database blip must not silently
    re-enable a strategy somebody switched off.
    """
    from types import SimpleNamespace

    from app.main import _strategy_state

    class _Registry:
        def keys(self) -> list[str]:
            return ["sma_cross"]

    def broken():  # noqa: ANN202
        raise RuntimeError("the database is unreachable")

    app = SimpleNamespace(
        state=SimpleNamespace(
            strategy_engine=SimpleNamespace(registry=_Registry()),
            session_factory=broken,
        )
    )
    state = await _strategy_state(app)("sma_cross")  # type: ignore[arg-type]
    assert state.exists is True
    assert state.enabled is False
    assert "refusing rather than assuming" in state.reason


class _Session:
    """Hand the resolver the test's own session, unclosed."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def __aenter__(self) -> AsyncSession:
        return self.db

    async def __aexit__(self, *exc: object) -> None:
        return None
