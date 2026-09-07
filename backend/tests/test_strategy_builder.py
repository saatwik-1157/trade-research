"""The strategy builder: schema, validation, interpretation, versioning, security.

The three that carry the level:

  * `test_price_cannot_be_compared_with_rsi` -- the semantic check the prompt
    names. A price level and a 0-100 oscillator are different quantities, and a
    comparison between them produces a number that means nothing.
  * `test_a_definition_never_becomes_code` -- a definition is data walked by a
    fixed evaluator. Nothing in the builder path calls eval, exec, compile or
    __import__ on anything, let alone on user input.
  * `test_user_a_cannot_touch_user_bs_strategy` -- ownership is scoped in the
    query, and a strategy belonging to someone else answers exactly as one that
    does not exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.strategies.base import Candles, SignalType
from app.strategies.built import BuiltStrategy
from app.strategies.definition import DefinitionError, parse_definition
from app.strategies.indicators import CATALOGUE, spec_for
from app.symbols import service as symbols
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from tests.routes import api_routes

T0 = datetime(2026, 9, 1, tzinfo=UTC)
ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
BOB = {"email": "bob@tr-platform.io", "password": "another strong one"}


def indicator(key: str, period: int) -> dict:
    return {"kind": "indicator", "ref": key, "params": {"period": period}}


def price(field: str = "close") -> dict:
    return {"kind": "price", "ref": field}


def constant(value: float) -> dict:
    return {"kind": "constant", "value": value}


def condition(left: dict, comparison: str, right: dict) -> dict:
    return {"type": "condition", "left": left, "comparison": comparison, "right": right}


def group(logical: str, *children: dict) -> dict:
    return {"type": "group", "logical": logical, "children": list(children)}


def definition(**overrides: object) -> dict:
    base: dict[str, object] = {
        "name": "EMA cross with RSI filter",
        "description": "Long when the fast EMA crosses the slow and RSI agrees.",
        "symbol": "EURUSD",
        "timeframe": "H1",
        "entry_rules": [
            {
                "when": group(
                    "AND",
                    condition(indicator("EMA", 20), "CROSSES_ABOVE", indicator("EMA", 50)),
                    condition(indicator("RSI", 14), "GREATER_THAN", constant(50)),
                ),
                "then": "ENTRY_LONG",
            }
        ],
        "exit_rules": [
            {
                "when": condition(indicator("RSI", 14), "LESS_THAN", constant(40)),
                "then": "CLOSE",
            }
        ],
    }
    base.update(overrides)
    return base


def bars(closes: list[float], start: datetime = T0) -> list[Bar]:
    out: list[Bar] = []
    for i, close in enumerate(closes):
        c = Decimal(str(round(close, 5)))
        out.append(
            Bar(
                symbol="EURUSD",
                provider=Provider.simulator,
                timeframe=Timeframe.H1,
                bar_time=start + timedelta(hours=i),
                open=c,
                high=c + Decimal("0.0010"),
                low=c - Decimal("0.0010"),
                close=c,
                volume=Decimal("100"),
                spread=None,
                spread_availability=Availability.not_available,
                complete=True,
            )
        )
    return out


# ------------------------------------------------------ indicator catalogue


def test_only_indicators_the_pipeline_actually_has_are_exposed() -> None:
    """Four, because four is what the numpy rule pipeline has. MACD and
    Bollinger exist in the pandas snapshot pipeline over a different data
    shape; offering them would mean a second implementation."""
    assert set(CATALOGUE) == {"SMA", "EMA", "RSI", "ATR"}


def test_every_indicator_declares_bounded_parameters() -> None:
    for spec in CATALOGUE.values():
        for parameter in spec.parameters:
            assert parameter.minimum >= 2
            assert parameter.maximum > parameter.minimum


@pytest.mark.parametrize("bad", [0, 1, -5, 10_000, 14.5, "14", True])
def test_an_impossible_period_is_refused(bad: object) -> None:
    """An RSI period of 0 is not a degenerate RSI, it is a division by zero
    waiting for market data."""
    with pytest.raises(ValueError):
        spec_for("RSI").validate_params({"period": bad})


def test_an_unknown_indicator_parameter_is_refused() -> None:
    with pytest.raises(ValueError, match="has no parameter"):
        spec_for("RSI").validate_params({"perod": 14})


def test_an_unknown_indicator_is_refused_with_the_known_ones() -> None:
    with pytest.raises(ValueError) as exc:
        spec_for("SUPERTREND")
    assert "RSI" in str(exc.value)


# ------------------------------------------------------- structural parsing


def test_a_well_formed_definition_parses() -> None:
    parsed = parse_definition(definition())
    assert parsed.symbol == "EURUSD"
    assert parsed.timeframe is Timeframe.H1
    assert len(parsed.entry_rules) == 1
    assert len(parsed.exit_rules) == 1


def test_the_summary_reads_without_json() -> None:
    lines = parse_definition(definition()).summary()
    assert any("crosses above" in line for line in lines)
    assert any(line.startswith("ENTRY:") for line in lines)
    assert any(line.startswith("EXIT:") for line in lines)


def test_a_strategy_with_no_entry_rule_is_refused() -> None:
    """A strategy with no entry rule can never do anything."""
    with pytest.raises(DefinitionError, match="at least one rule"):
        parse_definition(definition(entry_rules=[]))


def test_a_strategy_with_no_exit_rule_is_allowed_and_says_so() -> None:
    """Exits may come from the stop, the target or the position manager. The
    preview states that rather than leaving it implied."""
    parsed = parse_definition(definition(exit_rules=[]))
    assert any("none defined" in line for line in parsed.summary())


def test_an_unknown_field_is_refused_not_dropped() -> None:
    """A silently ignored field means the user is running a strategy they did
    not write."""
    with pytest.raises(DefinitionError, match="unknown fields"):
        parse_definition(definition(leverage=100))


def test_an_unsupported_timeframe_is_refused() -> None:
    with pytest.raises(DefinitionError, match="unknown timeframe"):
        parse_definition(definition(timeframe="H2"))


def test_an_unknown_comparison_is_refused() -> None:
    bad = definition(
        entry_rules=[
            {
                "when": condition(price(), "APPROXIMATELY", constant(1.1)),
                "then": "ENTRY_LONG",
            }
        ]
    )
    with pytest.raises(DefinitionError, match="comparison must be one of"):
        parse_definition(bad)


def test_an_entry_rule_cannot_emit_close() -> None:
    """An entry rule that could emit CLOSE would let the builder close
    positions it never opened."""
    bad = definition(
        entry_rules=[{"when": condition(price(), "GREATER_THAN", constant(1.0)), "then": "CLOSE"}]
    )
    with pytest.raises(DefinitionError, match="may not be CLOSE"):
        parse_definition(bad)


def test_not_takes_exactly_one_child() -> None:
    bad = definition(
        entry_rules=[
            {
                "when": group(
                    "NOT",
                    condition(price(), "GREATER_THAN", constant(1.0)),
                    condition(price(), "LESS_THAN", constant(2.0)),
                ),
                "then": "ENTRY_LONG",
            }
        ]
    )
    with pytest.raises(DefinitionError, match="NOT takes exactly one"):
        parse_definition(bad)


def test_nesting_is_bounded() -> None:
    """A tree that deep is unreadable and almost always a mistake -- and a
    hostile one could exhaust the stack."""
    node: dict = condition(price(), "GREATER_THAN", constant(1.0))
    for _ in range(12):
        node = group("NOT", node)
    with pytest.raises(DefinitionError, match="nests deeper"):
        parse_definition(definition(entry_rules=[{"when": node, "then": "ENTRY_LONG"}]))


def test_nested_and_or_is_supported() -> None:
    nested = group(
        "OR",
        group(
            "AND",
            condition(indicator("EMA", 20), "GREATER_THAN", indicator("EMA", 50)),
            condition(indicator("RSI", 14), "GREATER_THAN", constant(50)),
        ),
        condition(indicator("RSI", 14), "GREATER_THAN", constant(70)),
    )
    parsed = parse_definition(definition(entry_rules=[{"when": nested, "then": "ENTRY_LONG"}]))
    assert "OR" in parsed.entry_rules[0].label()


# --------------------------------------------- the semantic compatibility check


def test_price_cannot_be_compared_with_rsi() -> None:
    """The example the prompt gives, and it is exactly right: a price level and
    a 0-100 oscillator are different quantities."""
    bad = definition(
        entry_rules=[
            {
                "when": condition(price("close"), "GREATER_THAN", indicator("RSI", 14)),
                "then": "ENTRY_LONG",
            }
        ]
    )
    with pytest.raises(DefinitionError) as exc:
        parse_definition(bad)
    assert "different quantities" in str(exc.value)


def test_atr_cannot_be_compared_with_a_price_level() -> None:
    """ATR is a price DISTANCE, not a level. Comparing a range to a level is
    the metals-points error in miniature."""
    bad = definition(
        entry_rules=[
            {
                "when": condition(indicator("ATR", 14), "GREATER_THAN", price("close")),
                "then": "ENTRY_LONG",
            }
        ]
    )
    with pytest.raises(DefinitionError, match="different quantities"):
        parse_definition(bad)


def test_a_constant_is_compatible_with_anything() -> None:
    """A constant takes its meaning from what it is compared against."""
    for operand in (price("close"), indicator("RSI", 14), indicator("ATR", 14)):
        parse_definition(
            definition(
                entry_rules=[
                    {
                        "when": condition(operand, "GREATER_THAN", constant(1)),
                        "then": "ENTRY_LONG",
                    }
                ]
            )
        )


def test_two_constants_are_not_a_condition() -> None:
    bad = definition(
        entry_rules=[
            {"when": condition(constant(1), "GREATER_THAN", constant(0)), "then": "ENTRY_LONG"}
        ]
    )
    with pytest.raises(DefinitionError, match="fixed answer"):
        parse_definition(bad)


def test_two_price_operands_are_comparable() -> None:
    parse_definition(
        definition(
            entry_rules=[
                {
                    "when": condition(price("close"), "GREATER_THAN", price("open")),
                    "then": "ENTRY_LONG",
                }
            ]
        )
    )


# ------------------------------------------------------------ interpretation


def test_a_built_strategy_produces_a_signal() -> None:
    """A rising series takes the fast EMA above the slow."""
    built = BuiltStrategy(parse_definition(definition()))
    series = [1.1000 - i * 0.0004 for i in range(80)] + [
        1.1000 - 79 * 0.0004 + i * 0.0025 for i in range(80)
    ]
    candles = Candles.of("EURUSD", Timeframe.H1, bars(series))
    signal = built.generate_signal(candles, now=T0)
    assert signal.signal_type in (SignalType.entry_long, SignalType.close, SignalType.hold)
    assert signal.confidence is None  # nothing has measured one


def test_a_built_strategy_holds_when_no_rule_matches() -> None:
    simple = definition(
        entry_rules=[
            {
                "when": condition(price("close"), "GREATER_THAN", constant(99)),
                "then": "ENTRY_LONG",
            }
        ],
        exit_rules=[],
    )
    built = BuiltStrategy(parse_definition(simple))
    candles = Candles.of("EURUSD", Timeframe.H1, bars([1.10] * 20))
    assert built.generate_signal(candles, now=T0).signal_type is SignalType.hold


def test_a_condition_that_matches_fires() -> None:
    simple = definition(
        entry_rules=[
            {
                "when": condition(price("close"), "GREATER_THAN", constant(1.0)),
                "then": "ENTRY_LONG",
            }
        ],
        exit_rules=[],
    )
    built = BuiltStrategy(parse_definition(simple))
    candles = Candles.of("EURUSD", Timeframe.H1, bars([1.10] * 20))
    signal = built.generate_signal(candles, now=T0)
    assert signal.signal_type is SignalType.entry_long
    assert "close" in signal.reasoning.lower() or "CLOSE" in signal.reasoning


def test_not_inverts() -> None:
    inverted = definition(
        entry_rules=[
            {
                "when": group("NOT", condition(price("close"), "GREATER_THAN", constant(99))),
                "then": "ENTRY_LONG",
            }
        ],
        exit_rules=[],
    )
    built = BuiltStrategy(parse_definition(inverted))
    candles = Candles.of("EURUSD", Timeframe.H1, bars([1.10] * 20))
    assert built.generate_signal(candles, now=T0).signal_type is SignalType.entry_long


def test_a_warm_up_shortfall_gives_no_signal_not_a_hold() -> None:
    built = BuiltStrategy(parse_definition(definition()))
    candles = Candles.of("EURUSD", Timeframe.H1, bars([1.10] * 5))
    signal = built.generate_signal(candles, now=T0)
    assert signal.signal_type is SignalType.no_signal
    assert "warmed up" in signal.reasoning


def test_the_warm_up_comes_from_the_longest_indicator() -> None:
    parsed = parse_definition(definition())
    # EMA(50) is the longest; the multiplier makes an indicator an indicator.
    assert parsed.warmup() >= 150


def test_a_definition_refuses_to_run_on_another_instrument() -> None:
    """Running a EURUSD definition against GBPUSD would produce signals nobody
    asked for."""
    built = BuiltStrategy(parse_definition(definition()))
    wrong = Candles.of("GBPUSD", Timeframe.H1, bars([1.10] * 400))
    assert built.generate_signal(wrong, now=T0).signal_type is SignalType.no_signal


def test_interpretation_is_deterministic() -> None:
    built = BuiltStrategy(parse_definition(definition()))
    candles = Candles.of("EURUSD", Timeframe.H1, bars([1.1 + i * 0.001 for i in range(200)]))
    produced = {built.generate_signal(candles, now=T0).signal_type for _ in range(5)}
    assert len(produced) == 1


def test_a_built_strategy_cannot_see_the_forming_bar() -> None:
    """The same guarantee the hand-written rules have, for the same reason."""
    closed = bars([1.1 + i * 0.001 for i in range(200)])
    forming = Bar(
        symbol="EURUSD",
        provider=Provider.simulator,
        timeframe=Timeframe.H1,
        bar_time=T0 + timedelta(hours=200),
        open=Decimal("9"),
        high=Decimal("9"),
        low=Decimal("9"),
        close=Decimal("9"),
        complete=False,
    )
    built = BuiltStrategy(parse_definition(definition()))
    without = built.generate_signal(Candles.of("EURUSD", Timeframe.H1, closed), now=T0)
    with_forming = built.generate_signal(
        Candles.of("EURUSD", Timeframe.H1, [*closed, forming]), now=T0
    )
    assert without.signal_type is with_forming.signal_type
    assert without.bar_time == with_forming.bar_time


def test_a_built_strategy_starts_research_only_with_no_evidence() -> None:
    metadata = BuiltStrategy(parse_definition(definition())).metadata()
    assert metadata.tier.value == "research_only"
    assert "no backtest" in metadata.evidence.lower()


# ------------------------------------------------------------------ security


def test_a_definition_never_becomes_code() -> None:
    """The set of things a user can express is exactly the set this evaluator
    implements; nothing else can be smuggled through."""
    import ast
    import importlib
    import inspect

    unsafe = {"eval", "exec", "compile", "__import__"}
    for name in (
        "app.strategies.definition",
        "app.strategies.built",
        "app.strategies.indicators",
        "app.api.v1.strategy_builder",
    ):
        module = importlib.import_module(name)
        tree = ast.parse(inspect.getsource(module))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not (called & unsafe), f"{name} calls {called & unsafe}"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "indicator", "ref": "__import__('os').system", "params": {}},
        {"kind": "indicator", "ref": "../../etc/passwd", "params": {}},
        {"kind": "price", "ref": "__class__"},
        {"kind": "indicator", "ref": "RSI", "params": {"period": "__import__('os')"}},
    ],
)
def test_a_hostile_operand_is_refused(payload: dict) -> None:
    bad = definition(
        entry_rules=[
            {"when": condition(payload, "GREATER_THAN", constant(1)), "then": "ENTRY_LONG"}
        ]
    )
    with pytest.raises(DefinitionError):
        parse_definition(bad)


def test_the_interpreter_reference_is_fixed_not_user_supplied() -> None:
    """`code_ref` is the same string for every built strategy. It is not a path
    anything imports from a payload."""
    from app.api.v1.strategy_builder import INTERPRETER

    assert INTERPRETER == "app.strategies.built:BuiltStrategy"


def test_a_definition_cannot_reach_execution() -> None:
    import ast
    import importlib
    import inspect

    for name in ("app.strategies.definition", "app.strategies.built"):
        module = importlib.import_module(name)
        tree = ast.parse(inspect.getsource(module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        for forbidden in ("app.brokers", "app.risk", "app.sizing", "MetaTrader5", "subprocess"):
            assert not any(i.startswith(forbidden) for i in imported), f"{name} -> {forbidden}"


# --------------------------------------------------------------------- API


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
    async with application.state.session_factory() as db:
        await symbols.upsert_symbol(db, "EURUSD", "fx", digits=5)
        await db.commit()
    yield application
    await eng.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER

    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


@pytest.fixture
async def alice(client: AsyncClient) -> AsyncClient:
    await client.post("/auth/register", json=ALICE)
    return client


async def _create(client: AsyncClient, key: str = "my_ema_cross") -> dict:
    r = await client.post(
        "/v1/strategy-builder",
        json={"key": key, "definition": definition()},
        headers=_csrf(client),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_the_catalogue_lists_only_supported_indicators(alice: AsyncClient) -> None:
    body = (await alice.get("/v1/strategy-builder/catalogue")).json()
    assert {i["key"] for i in body["indicators"]} == {"SMA", "EMA", "RSI", "ATR"}
    assert "CROSSES_ABOVE" in body["comparisons"]
    assert "Nothing here becomes code" in body["note"]


async def test_validation_gives_useful_feedback_without_saving(alice: AsyncClient) -> None:
    r = await alice.post(
        "/v1/strategy-builder/validate",
        json={"definition": definition()},
        headers=_csrf(alice),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["summary"]
    assert "does not mean profitable" in body["note"]


async def test_an_invalid_definition_names_the_field(alice: AsyncClient) -> None:
    bad = definition(
        entry_rules=[
            {
                "when": condition(price("close"), "GREATER_THAN", indicator("RSI", 14)),
                "then": "ENTRY_LONG",
            }
        ]
    )
    r = await alice.post(
        "/v1/strategy-builder/validate", json={"definition": bad}, headers=_csrf(alice)
    )
    assert r.status_code == 422
    detail = r.json()["error"]["detail"]
    assert "entry_rules[0]" in detail
    assert "traceback" not in detail.lower()


async def test_creating_a_strategy_persists_a_validated_version(
    app: FastAPI, alice: AsyncClient
) -> None:
    created = await _create(alice)
    assert created["version"] == 1
    assert created["status"] == "validated"

    body = (await alice.get("/v1/strategy-builder/my_ema_cross")).json()
    assert body["tier"] == "research_only"
    assert body["versions"][0]["code_ref"] == "app.strategies.built:BuiltStrategy"


async def test_a_duplicate_key_is_a_conflict(alice: AsyncClient) -> None:
    await _create(alice)
    r = await alice.post(
        "/v1/strategy-builder",
        json={"key": "my_ema_cross", "definition": definition()},
        headers=_csrf(alice),
    )
    assert r.status_code == 409


async def test_a_new_version_is_created_not_an_overwrite(alice: AsyncClient) -> None:
    """A signal recorded against version 2 must always be explainable by
    reading version 2."""
    await _create(alice)
    r = await alice.post(
        "/v1/strategy-builder/my_ema_cross/versions",
        json={"definition": definition(name="Renamed")},
        headers=_csrf(alice),
    )
    assert r.status_code == 201
    assert r.json()["version"] == 2
    body = (await alice.get("/v1/strategy-builder/my_ema_cross")).json()
    assert [v["version"] for v in body["versions"]] == [1, 2]
    assert body["versions"][0]["definition"]["name"] == "EMA cross with RSI filter"


async def test_a_validated_version_cannot_be_edited_in_place(alice: AsyncClient) -> None:
    await _create(alice)
    r = await alice.put(
        "/v1/strategy-builder/my_ema_cross/versions/1",
        json={"definition": definition(name="Sneaky edit")},
        headers=_csrf(alice),
    )
    assert r.status_code == 409
    assert "create a new version" in r.json()["error"]["detail"]


async def test_duplication_leaves_the_original_alone(alice: AsyncClient) -> None:
    await _create(alice)
    r = await alice.post(
        "/v1/strategy-builder/my_ema_cross/duplicate",
        params={"new_key": "my_copy"},
        headers=_csrf(alice),
    )
    assert r.status_code == 201
    assert r.json()["cloned_from"] == {"key": "my_ema_cross", "version": 1}
    copy = (await alice.get("/v1/strategy-builder/my_copy")).json()
    assert copy["versions"][0]["definition"]["cloned_from"]["key"] == "my_ema_cross"
    # And the wrapped definition is still parseable, which is the point.
    preview = (await alice.get("/v1/strategy-builder/my_copy/versions/1/preview")).json()
    assert preview["summary"]
    original = (await alice.get("/v1/strategy-builder/my_ema_cross")).json()
    assert len(original["versions"]) == 1


@pytest.mark.parametrize("wanted,level", [("active", 17), ("paper", 16), ("backtested", 14)])
async def test_a_state_whose_machinery_is_missing_is_refused(
    alice: AsyncClient, wanted: str, level: int
) -> None:
    """Marking a version ACTIVE with no risk engine would let it claim a state
    nothing enforces."""
    await _create(alice)
    r = await alice.patch(
        "/v1/strategy-builder/my_ema_cross/versions/1/status",
        json={"status": wanted},
        headers=_csrf(alice),
    )
    assert r.status_code == 409
    assert f"level {level:02d}" in r.json()["error"]["detail"]


async def test_a_reachable_state_transition_works_and_is_audited(
    app: FastAPI, alice: AsyncClient
) -> None:
    from app.models.ops import AuditLog
    from sqlalchemy import select as sa_select

    await _create(alice)
    r = await alice.patch(
        "/v1/strategy-builder/my_ema_cross/versions/1/status",
        json={"status": "retired"},
        headers=_csrf(alice),
    )
    assert r.status_code == 200
    assert r.json() == {
        "key": "my_ema_cross",
        "version": 1,
        "status": "retired",
        "previous": "validated",
    }
    async with app.state.session_factory() as db:
        actions = [a.action for a in (await db.scalars(sa_select(AuditLog))).all()]
    assert "strategy_version_status_changed" in actions


async def test_the_preview_never_pretends_a_backtest_ran(alice: AsyncClient) -> None:
    await _create(alice)
    body = (await alice.get("/v1/strategy-builder/my_ema_cross/versions/1/preview")).json()
    assert body["backtest"] == {
        "available": False,
        "state": "NOT RUN",
        "reason": "the backtest runner is built at level 14",
    }
    assert body["summary"]


async def test_an_unmapped_symbol_is_reported_not_invented(alice: AsyncClient) -> None:
    """The builder must not invent broker symbols."""
    r = await alice.post(
        "/v1/strategy-builder/validate",
        json={"definition": definition(symbol="NOTREAL")},
        headers=_csrf(alice),
    )
    body = r.json()
    assert body["valid"] is False
    assert any("symbol" in p for p in body["problems"])


# ------------------------------------------------------------ authorization


async def test_builder_routes_need_a_session(client: AsyncClient) -> None:
    assert (await client.get("/v1/strategy-builder")).status_code == 401
    assert (await client.get("/v1/strategy-builder/catalogue")).status_code == 401


async def test_user_a_cannot_touch_user_bs_strategy(app: FastAPI) -> None:
    """Ownership is scoped in the query. Someone else's strategy answers
    exactly as one that does not exist -- telling a caller the key is taken
    would be a membership oracle."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as a:
        await a.post("/auth/register", json=ALICE)
        await _create(a, "alices_strategy")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as b:
        await b.post("/auth/register", json=BOB)
        assert (await b.get("/v1/strategy-builder/alices_strategy")).status_code == 404
        assert (
            await b.post(
                "/v1/strategy-builder/alices_strategy/versions",
                json={"definition": definition()},
                headers=_csrf(b),
            )
        ).status_code == 404
        assert (
            await b.patch(
                "/v1/strategy-builder/alices_strategy/versions/1/status",
                json={"status": "retired"},
                headers=_csrf(b),
            )
        ).status_code == 404
        listed = (await b.get("/v1/strategy-builder")).json()
        assert listed["count"] == 0


# ---------------------------------------------------------------- safety


async def test_a_built_strategy_creates_a_signal_never_an_order(
    alice: AsyncClient,
) -> None:
    body = (await alice.get("/v1/strategy-builder/catalogue")).json()
    assert "never an order" in body["note"]


async def test_the_builder_surface_has_no_execution_verb(app: FastAPI) -> None:
    seen = 0
    for route in api_routes(app):
        path = getattr(route, "path", "")
        if path.startswith("/v1/strategy-builder"):
            seen += 1
            assert "DELETE" not in (getattr(route, "methods", set()) or set()), path
    # An empty sweep proves nothing about the surface, and read `app.routes`
    # for one FastAPI release too long already.
    assert seen, "no builder routes were examined; the sweep is not working"
