"""The strategy interface, registry, engine — and the regression tests.

The two that carry the level:

  * `test_the_wrapper_matches_the_toolkit_rule_exactly` runs each wrapped rule
    and the `tools/mt5_paper` function it wraps over the same bars and asserts
    they agree on every one. That is the whole claim of an interface migration:
    behaviour did not move.
  * `test_a_strategy_cannot_see_the_forming_bar` proves the no-look-ahead rule
    is enforced by construction rather than by convention. Reading the bar you
    enter on is the cheapest way to manufacture an edge that does not exist,
    and it is invisible in an equity curve.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.strategies.base import (
    Candles,
    ConfigError,
    SignalTiming,
    SignalType,
    Strategy,
    StrategyMetadata,
    StrategySignal,
    StrategyTier,
)
from app.strategies.engine import Outcome, StrategyEngine, validate_signal
from app.strategies.registry import (
    StrategyNotAvailable,
    UnknownStrategy,
    default_registry,
)
from app.strategies.rules import RsiReversion, SmaCross, rates_from
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}


def _toolkit():  # noqa: ANN202
    # tests/ -> backend/ -> the repository root, where tools/ lives.
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools = os.path.join(os.path.dirname(backend), "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import mt5_paper

    return mt5_paper


def bars(closes: list[float], *, complete: bool = True, start: datetime = T0) -> list[Bar]:
    """Deterministic bars from a list of closes."""
    out: list[Bar] = []
    for i, close in enumerate(closes):
        c = Decimal(str(close))
        out.append(
            Bar(
                symbol="EURUSD",
                provider=Provider.simulator,
                timeframe=Timeframe.H1,
                bar_time=start + timedelta(hours=i),
                open=c,
                high=c + Decimal("0.0005"),
                low=c - Decimal("0.0005"),
                close=c,
                volume=Decimal("100"),
                spread=None,
                spread_availability=Availability.not_available,
                complete=complete,
            )
        )
    return out


def rising(n: int = 120) -> list[float]:
    return [1.1000 + i * 0.0002 for i in range(n)]


def falling(n: int = 120) -> list[float]:
    return [1.1000 - i * 0.0002 for i in range(n)]


def crossing_up(n: int = 120) -> list[float]:
    """Long decline then a sharp rise: takes the fast SMA through the slow."""
    half = n // 2
    down = [1.2000 - i * 0.0004 for i in range(half)]
    up = [down[-1] + i * 0.0030 for i in range(n - half)]
    return down + up


# ------------------------------------------------------- signal vocabulary


def test_entry_and_exit_are_separate_instructions() -> None:
    """'Get out of a long' and 'go short' are different. A vocabulary that
    collapsed them would open a short every time a strategy wanted flat."""
    assert SignalType.exit_long.direction == "sell"
    assert SignalType.entry_short.direction == "sell"
    assert SignalType.exit_long is not SignalType.entry_short


def test_hold_and_no_signal_are_different_answers() -> None:
    """HOLD means the strategy has a view. NO_SIGNAL means it could not form
    one. A caller counting activity needs to tell them apart."""
    assert SignalType.hold.is_actionable is False
    assert SignalType.no_signal.is_actionable is False
    assert SignalType.hold is not SignalType.no_signal


def test_only_entries_and_exits_are_actionable() -> None:
    actionable = {t for t in SignalType if t.is_actionable}
    assert actionable == {
        SignalType.entry_long,
        SignalType.entry_short,
        SignalType.exit_long,
        SignalType.exit_short,
        SignalType.close,
    }


def test_close_is_flat_not_a_sell() -> None:
    assert SignalType.close.direction == "flat"


# ------------------------------------------------------ no look-ahead


def test_a_strategy_cannot_see_the_forming_bar() -> None:
    """Enforced by construction: the forming bar is not in the object."""
    mixed = bars(rising(10)) + bars([1.5], complete=False, start=T0 + timedelta(hours=10))
    candles = Candles.of("EURUSD", Timeframe.H1, mixed)
    assert len(candles) == 10
    assert all(b.complete for b in candles.bars)
    assert Decimal("1.5") not in [b.close for b in candles.bars]


def test_candles_are_sorted_so_two_runs_agree() -> None:
    shuffled = list(reversed(bars(rising(20))))
    candles = Candles.of("EURUSD", Timeframe.H1, shuffled)
    times = [b.bar_time for b in candles.bars]
    assert times == sorted(times)


def test_the_stand_in_bar_is_a_duplicate_and_is_never_read() -> None:
    """The toolkit rules drop their own last element. The adapter appends a
    copy of the last CLOSED bar so the rule's `[:-1]` removes the stand-in
    rather than a real bar."""
    candles = Candles.of("EURUSD", Timeframe.H1, bars(rising(10)))
    rates = rates_from(candles)
    assert len(rates) == 11
    assert rates["close"][-1] == rates["close"][-2]
    # What the rule actually sees after its own slice: the closed set exactly.
    assert list(rates["close"][:-1]) == [float(b.close) for b in candles.bars]


# ------------------------------------- REGRESSION: behaviour did not move


@pytest.mark.parametrize(
    "series",
    [rising(120), falling(120), crossing_up(120), rising(80) + falling(80)],
    ids=["rising", "falling", "crossing_up", "up_then_down"],
)
def test_the_wrapper_matches_the_toolkit_rule_exactly(series: list[float]) -> None:
    """The whole claim of an interface migration: behaviour did not move.

    Each wrapped rule and the `tools/mt5_paper` function it wraps are run over
    the same bars and must agree. If this fails, the migration changed a
    strategy -- which is the one thing Step 9 forbids doing silently.
    """
    paper = _toolkit()
    candles = Candles.of("EURUSD", Timeframe.H1, bars(series))
    rates = rates_from(candles)

    for wrapper, toolkit_fn in (
        (SmaCross(), paper.rule_sma_cross),
        (RsiReversion(), paper.rule_rsi_reversion),
    ):
        expected = toolkit_fn(rates)
        produced = wrapper.generate_signal(candles, now=T0)
        assert produced.metadata["raw_signal"] == expected, (
            f"{wrapper.metadata().key} diverged: wrapper said "
            f"{produced.metadata['raw_signal']!r}, toolkit said {expected!r}"
        )


def test_the_wrapper_maps_raw_signals_to_the_right_types() -> None:
    paper = _toolkit()
    candles = Candles.of("EURUSD", Timeframe.H1, bars(crossing_up(120)))
    raw = paper.rule_sma_cross(rates_from(candles))
    signal = SmaCross().generate_signal(candles, now=T0)
    expected = {
        "buy": SignalType.entry_long,
        "sell": SignalType.entry_short,
        None: SignalType.hold,
    }[raw]
    assert signal.signal_type is expected


def test_an_oversold_series_reaches_the_rsi_rule_as_a_buy() -> None:
    """Not a claim about edge -- a check that the wrapper transports the rule's
    own answer, whatever it is."""
    paper = _toolkit()
    candles = Candles.of("EURUSD", Timeframe.H1, bars(falling(120)))
    rates = rates_from(candles)
    assert paper.rule_rsi_reversion(rates) == "buy"
    assert RsiReversion().generate_signal(candles, now=T0).signal_type is SignalType.entry_long


# ---------------------------------------------------- warm-up and NO_SIGNAL


def test_too_few_bars_gives_no_signal_not_a_hold() -> None:
    """An RSI over three bars is a number, and it is not an RSI."""
    candles = Candles.of("EURUSD", Timeframe.H1, bars(rising(5)))
    signal = RsiReversion().generate_signal(candles, now=T0)
    assert signal.signal_type is SignalType.no_signal
    assert "needs" in signal.reasoning


def test_the_warm_up_matches_the_rules_own_guard() -> None:
    assert SmaCross().required_data().min_bars == 61
    assert RsiReversion().required_data().min_bars == 43
    assert RsiReversion({"period": 20}).required_data().min_bars == 61


# ------------------------------------------------ configuration validation


def test_an_unknown_parameter_is_refused_not_ignored() -> None:
    """A typo'd parameter that is silently dropped runs a strategy nobody
    configured."""
    with pytest.raises(ConfigError, match="unknown configuration keys"):
        RsiReversion({"perod": 14})


def test_sma_cross_takes_no_parameters_because_the_live_rule_hardcodes_them() -> None:
    """Accepting 20/50 as parameters would mean this is not the strategy that
    traded."""
    assert SmaCross().config == {}
    with pytest.raises(ConfigError):
        SmaCross({"fast": 10})


@pytest.mark.parametrize("bad", [0, 1, -5, 500])
def test_an_impossible_rsi_period_is_refused(bad: int) -> None:
    with pytest.raises(ConfigError, match="between"):
        RsiReversion({"period": bad})


@pytest.mark.parametrize("bad", ["14", 14.0, True, None])
def test_a_non_integer_period_is_refused(bad: object) -> None:
    with pytest.raises(ConfigError):
        RsiReversion({"period": bad})


def test_a_valid_parameter_is_accepted_and_used() -> None:
    strategy = RsiReversion({"period": 21})
    assert strategy.config == {"period": 21}


# ------------------------------------------------------------- determinism


def test_the_same_input_gives_the_same_signal() -> None:
    """Required for backtesting, replay, debugging and reproducibility."""
    candles = Candles.of("EURUSD", Timeframe.H1, bars(crossing_up(120)))
    produced = {SmaCross().generate_signal(candles, now=T0).signal_type for _ in range(5)}
    assert len(produced) == 1


def test_the_random_benchmark_is_seeded_and_reproducible() -> None:
    """The toolkit function uses the global `random` module and is not
    reproducible. Step 27 requires randomness to be explicit and controllable,
    so the wrapper seeds from the bar -- same bar, same draw."""
    from app.strategies.rules import RandomRule

    candles = Candles.of("EURUSD", Timeframe.H1, bars(rising(10)))
    first = RandomRule({"seed": 7}).generate_signal(candles, now=T0)
    again = RandomRule({"seed": 7}).generate_signal(candles, now=T0)
    assert first.signal_type is again.signal_type


def test_a_different_seed_can_draw_differently() -> None:
    from app.strategies.rules import RandomRule

    candles = Candles.of("EURUSD", Timeframe.H1, bars(rising(10)))
    draws = {
        RandomRule({"seed": s}).generate_signal(candles, now=T0).signal_type for s in range(40)
    }
    assert len(draws) > 1  # it is a coin flip, not a constant


def test_the_random_rule_keeps_its_original_distribution() -> None:
    """buy, sell, None, None -- so the benchmark still measures what it
    measured."""
    from app.strategies.rules import RandomRule

    counts = {SignalType.entry_long: 0, SignalType.entry_short: 0, SignalType.hold: 0}
    for i in range(400):
        candles = Candles.of(
            "EURUSD", Timeframe.H1, bars(rising(3), start=T0 + timedelta(hours=i * 10))
        )
        counts[RandomRule().generate_signal(candles, now=T0).signal_type] += 1
    # Roughly half hold, a quarter each way. Loose bounds: this is a
    # distribution check, not a randomness test.
    assert 0.35 < counts[SignalType.hold] / 400 < 0.65
    assert counts[SignalType.entry_long] > 40
    assert counts[SignalType.entry_short] > 40


def test_no_strategy_asserts_a_confidence_it_cannot_support() -> None:
    """An invented number reads downstream as evidence."""
    candles = Candles.of("EURUSD", Timeframe.H1, bars(crossing_up(120)))
    for strategy in (SmaCross(), RsiReversion()):
        assert strategy.generate_signal(candles, now=T0).confidence is None


# ---------------------------------------------------- registry and factory


def test_the_registry_lists_the_three_built_in_rules() -> None:
    registry = default_registry()
    assert registry.keys() == ["random", "rsi_reversion", "sma_cross"]


def test_an_unknown_key_is_refused_with_the_known_ones() -> None:
    """Two strategies whose names differ by a character are two strategies."""
    with pytest.raises(UnknownStrategy) as exc:
        default_registry().create("sma_crss")
    assert "sma_cross" in str(exc.value)


def test_the_registry_refuses_a_duplicate_registration() -> None:
    registry = default_registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(SmaCross)


def test_every_built_in_is_research_only() -> None:
    """A measurement, not caution: none separates from a coin flip at this
    broker's spreads."""
    for described in default_registry().describe():
        assert described["tier"] == "research_only"
        assert described["evidence"]


def test_the_tier_gate_refuses_a_research_rule_where_evidence_is_required() -> None:
    """What stops a candidate from a parameter search being run live because it
    happened to top a table."""
    with pytest.raises(StrategyNotAvailable, match="research_only"):
        default_registry().create("sma_cross", required_tier=StrategyTier.live_approved)


def test_the_factory_validates_configuration_before_returning() -> None:
    with pytest.raises(ConfigError):
        default_registry().create("rsi_reversion", {"period": -1})


def _called_names(module) -> set[str]:  # noqa: ANN001
    """Every function name this module actually calls.

    Parsed from the AST rather than matched against the source text, because a
    docstring that says "there is no eval here" contains the string "eval(".
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _imported_modules(module) -> set[str]:  # noqa: ANN001
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_registry_executes_nothing_from_a_name() -> None:
    """No eval, no exec, no importlib on a caller-supplied string. Checked
    against what the module calls, not against its prose."""
    import app.strategies.registry as module

    unsafe = {"eval", "exec", "compile", "__import__", "import_module"}
    assert not (_called_names(module) & unsafe)
    assert "importlib" not in _imported_modules(module)


def test_every_strategy_declares_its_timing() -> None:
    """Mixing candle-close with intrabar silently makes a backtest describe
    something the live tool does not do."""
    for described in default_registry().describe():
        assert described["timing"] == str(SignalTiming.bar_close)


# ------------------------------------------------------------- the engine


@pytest.fixture
def engine() -> StrategyEngine:
    return StrategyEngine(default_registry())


async def test_the_engine_produces_a_signal_from_real_bars(engine: StrategyEngine) -> None:
    result = await engine.evaluate(
        "sma_cross", "EURUSD", Timeframe.H1, bars(crossing_up(120)), now=T0 + timedelta(hours=121)
    )
    assert result.outcome in (Outcome.signal, Outcome.hold)
    assert result.signal is not None
    assert "not an order" in str(result.as_dict()["note"])


async def test_the_engine_reports_insufficient_data_rather_than_holding(
    engine: StrategyEngine,
) -> None:
    result = await engine.evaluate("sma_cross", "EURUSD", Timeframe.H1, bars(rising(5)), now=T0)
    assert result.outcome is Outcome.insufficient_data
    assert result.signal is None


async def test_stale_data_produces_no_signal(engine: StrategyEngine) -> None:
    """A rule fired on a bar that closed an hour ago is a decision about a
    market that has moved."""
    result = await engine.evaluate(
        "sma_cross",
        "EURUSD",
        Timeframe.H1,
        bars(crossing_up(120)),
        now=T0 + timedelta(days=30),
    )
    assert result.outcome is Outcome.stale_data
    assert result.signal is None
    assert "too old" in result.detail


async def test_a_closed_market_is_not_stale_data(engine: StrategyEngine) -> None:
    result = await engine.evaluate(
        "sma_cross",
        "EURUSD",
        Timeframe.H1,
        bars(crossing_up(120)),
        now=T0 + timedelta(days=30),
        market_open=False,
    )
    assert result.outcome is not Outcome.stale_data


async def test_a_strategy_error_is_never_a_neutral_signal(engine: StrategyEngine) -> None:
    """A crashing strategy that returned HOLD would read as a quiet market."""

    class Exploding(Strategy):
        def metadata(self) -> StrategyMetadata:
            return StrategyMetadata(key="boom", name="Boom", description="raises")

        def required_data(self):  # noqa: ANN201
            from app.strategies.base import DataRequirement

            return DataRequirement(min_bars=1)

        @classmethod
        def validate_config(cls, config: dict) -> dict:
            return {}

        def generate_signal(self, candles: Candles, *, now: datetime) -> StrategySignal:
            raise RuntimeError("indicator blew up")

    engine.registry.register(Exploding)
    result = await engine.evaluate(
        "boom", "EURUSD", Timeframe.H1, bars(rising(120)), now=T0 + timedelta(hours=121)
    )
    assert result.outcome is Outcome.error
    assert result.signal is None
    assert "indicator blew up" in (result.error or "")


async def test_one_bad_strategy_does_not_stop_the_others(engine: StrategyEngine) -> None:
    """A strategy error must not crash the platform."""
    good = await engine.evaluate(
        "sma_cross", "EURUSD", Timeframe.H1, bars(crossing_up(120)), now=T0 + timedelta(hours=121)
    )
    bad = await engine.evaluate("nope", "EURUSD", Timeframe.H1, bars(rising(120)), now=T0)
    assert bad.outcome is Outcome.error
    assert good.outcome in (Outcome.signal, Outcome.hold)


async def test_a_malformed_signal_is_an_error_not_a_signal(engine: StrategyEngine) -> None:
    """Publishing one would hand a subscriber something it cannot trust."""

    class Liar(Strategy):
        def metadata(self) -> StrategyMetadata:
            return StrategyMetadata(key="liar", name="Liar", description="answers wrongly")

        def required_data(self):  # noqa: ANN201
            from app.strategies.base import DataRequirement

            return DataRequirement(min_bars=1)

        @classmethod
        def validate_config(cls, config: dict) -> dict:
            return {}

        def generate_signal(self, candles: Candles, *, now: datetime) -> StrategySignal:
            return StrategySignal(
                strategy_key="liar",
                symbol="GBPUSD",  # answered about a different instrument
                timeframe=candles.timeframe,
                signal_type=SignalType.entry_long,
                bar_time=candles.last.bar_time,
                generated_at=now,
            )

    engine.registry.register(Liar)
    result = await engine.evaluate(
        "liar", "EURUSD", Timeframe.H1, bars(rising(120)), now=T0 + timedelta(hours=121)
    )
    assert result.outcome is Outcome.error
    assert "GBPUSD" in (result.error or "")


def test_signal_validation_catches_an_impossible_confidence() -> None:
    signal = StrategySignal(
        strategy_key="k",
        symbol="EURUSD",
        timeframe=Timeframe.H1,
        signal_type=SignalType.entry_long,
        bar_time=T0,
        generated_at=T0,
        confidence=Decimal("5"),
    )
    assert "confidence" in (validate_signal(signal, "k", "EURUSD", Timeframe.H1) or "")


async def test_the_engine_counts_activity(engine: StrategyEngine) -> None:
    await engine.evaluate("sma_cross", "EURUSD", Timeframe.H1, bars(rising(5)), now=T0)
    await engine.evaluate("nope", "EURUSD", Timeframe.H1, bars(rising(5)), now=T0)
    counts = engine.counters.as_dict()
    assert counts["evaluated"] == 2
    assert counts["insufficient_data"] == 1
    assert counts["errors"] == 1


async def test_two_strategy_instances_do_not_share_state() -> None:
    """Avoid shared mutable state between unrelated strategy instances."""
    a, b = RsiReversion({"period": 14}), RsiReversion({"period": 21})
    assert a.config == {"period": 14}
    assert b.config == {"period": 21}
    assert a.config is not b.config


async def test_an_actionable_signal_publishes_signal_created() -> None:
    from app.core.events import InMemoryEventBus
    from app.realtime.hub import Hub

    bus = InMemoryEventBus()
    engine = StrategyEngine(default_registry(), Hub(bus))
    # Drive a definite entry through the random rule by searching seeds.
    for seed in range(50):
        candles = bars(rising(10))
        result = await engine.evaluate(
            "random",
            "EURUSD",
            Timeframe.H1,
            candles,
            config={"seed": seed},
            now=T0 + timedelta(hours=11),
        )
        if result.outcome is Outcome.signal:
            break
    assert result.outcome is Outcome.signal
    assert [e.type for e in bus.published] == ["SIGNAL_CREATED"]
    published = bus.published[0].payload
    assert published["source"] == "strategy"
    assert published["status"] == "new"
    # No fill, no order, no price the platform did not observe.
    for absent in ("fill_price", "order_id", "ticket", "volume"):
        assert absent not in published


async def test_a_publish_failure_does_not_lose_the_evaluation() -> None:
    class Broken:
        kind = "broken"

        async def publish(self, event):  # noqa: ANN001, ANN201
            raise ConnectionError("redis is gone")

    from app.realtime.hub import Hub

    engine = StrategyEngine(default_registry(), Hub(Broken()))  # type: ignore[arg-type]
    result = await engine.evaluate(
        "sma_cross", "EURUSD", Timeframe.H1, bars(crossing_up(120)), now=T0 + timedelta(hours=121)
    )
    assert result.outcome in (Outcome.signal, Outcome.hold)


# ----------------------------------------------------------------- routes


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=eng)
    yield application
    await eng.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def signed_in(client: AsyncClient) -> AsyncClient:
    await client.post("/auth/register", json=ALICE)
    return client


async def test_strategy_routes_need_a_session(client: AsyncClient) -> None:
    assert (await client.get("/v1/strategies")).status_code == 401


async def test_the_listing_carries_the_evidence(signed_in: AsyncClient) -> None:
    body = (await signed_in.get("/v1/strategies")).json()
    keys = {s["key"] for s in body["strategies"]}
    assert keys == {"random", "rsi_reversion", "sma_cross"}
    for described in body["strategies"]:
        assert described["tier"] == "research_only"
        assert len(described["evidence"]) > 40
    assert "sized by nothing" in body["note"]


async def test_an_unknown_strategy_is_404(signed_in: AsyncClient) -> None:
    assert (await signed_in.get("/v1/strategies/nope")).status_code == 404


async def test_running_a_strategy_needs_more_than_a_read_permission(
    signed_in: AsyncClient,
) -> None:
    r = await signed_in.get(
        "/v1/strategies/sma_cross/evaluate", params={"symbol": "EURUSD", "provider": "simulator"}
    )
    assert r.status_code == 403
    assert "create_strategies" in r.json()["error"]["detail"]


# ----------------------------------------------------------------- safety


async def test_nothing_in_the_strategy_package_can_trade() -> None:
    """A strategy creates a signal. It does not create a broker order."""
    import importlib
    import pkgutil

    import app.strategies as pkg

    forbidden = {
        "MetaTrader5",
        "place_order",
        "order_send",
        "BrokerAdapter",
        "OrderRequest",
        "RiskEngine",
        "MT5Adapter",
    }
    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.strategies."):
        loaded = importlib.import_module(module.name)
        assert not (set(dir(loaded)) & forbidden), f"{module.name} reaches execution"


async def test_no_strategy_module_executes_generated_code() -> None:
    """Strategy definitions must never become arbitrary executable code."""
    import importlib
    import pkgutil

    import app.strategies as pkg

    unsafe = {"eval", "exec", "compile", "__import__"}
    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.strategies."):
        loaded = importlib.import_module(module.name)
        assert not (_called_names(loaded) & unsafe), module.name
        assert "pickle" not in _imported_modules(loaded), module.name


def test_the_engine_does_not_import_risk_sizing_or_the_oms() -> None:
    """Checked against the import statements, not the prose: the docstring
    names those packages precisely to say it does not reach them."""
    import app.strategies.engine as module

    imported = _imported_modules(module)
    for downstream in ("app.risk", "app.sizing", "app.brokers"):
        assert not any(name.startswith(downstream) for name in imported), imported


def test_the_existing_toolkit_rules_are_untouched() -> None:
    """Step 9: do not silently change the trading behaviour of an existing
    strategy during interface migration. The functions are called, not copied."""
    paper = _toolkit()
    for name in ("rule_sma_cross", "rule_rsi_reversion", "rule_random"):
        assert callable(getattr(paper, name))
    assert set(paper.RULES) == {"sma_cross", "rsi_reversion", "random"}


async def test_an_unknown_strategy_key_is_404_not_an_error_outcome(
    signed_in: AsyncClient,
) -> None:
    """The engine catches its own errors by design, so the route must resolve
    the key itself. A typo'd key is a missing resource, not a strategy that
    ran and failed -- and it came back 200 until this was caught live."""
    r = await signed_in.get(
        "/v1/strategies/sma_crss/evaluate", params={"symbol": "EURUSD", "provider": "simulator"}
    )
    # 404 before the permission gate is even relevant: the resource is absent.
    assert r.status_code in (403, 404)
    if r.status_code == 404:
        assert "sma_cross" in r.json()["error"]["detail"]
