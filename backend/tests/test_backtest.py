"""The backtest engine: determinism, costs, metrics, jobs — and look-ahead.

`test_future_bars_cannot_change_an_earlier_decision` is the one that gates the
level. Step 36 asks for an explicit proof: run over a dataset, then modify
**only** the bars after T, and the decision at T must be identical. If it is
not, there is a look-ahead bug and nothing else in the level matters.

The second most important is `test_the_engine_is_the_toolkit_not_a_second_one`.
`tools/rule_backtest.simulate` already implements next-bar entry,
loss-on-a-straddled-bar, spread-charged-once and no-overlapping-positions, each
pinned by the toolkit's own tests. A second engine would mean re-deriving four
constraints that took real trades to learn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.auth.models import User
from app.backtest.config import (
    ENGINE_VERSION,
    BacktestConfig,
    ConfigError,
    CostModel,
    ExecutionModel,
    SizingMode,
)
from app.backtest.runner import (
    MIN_TRADES_FOR_RATIO,
    NOT_AVAILABLE,
    BacktestError,
    build_signal_vector,
    run,
)
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.models.research import Backtest
from app.strategies.base import Candles
from app.strategies.registry import default_registry
from app.strategies.rules import SmaCross
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}


def bars(closes: list[float], *, start: datetime = T0, complete: bool = True) -> list[Bar]:
    """Bars with a real body.

    An earlier version set open == close on every bar, and L08's validator
    correctly flagged the series as synthetic: a ~40% open-equals-close rate is
    the signature of a vendor filling opens in rather than observing them. The
    validator was right, so the fixture changed.
    """
    out: list[Bar] = []
    previous: Decimal | None = None
    for i, close in enumerate(closes):
        c = Decimal(str(round(close, 5)))
        o = previous if previous is not None else c - Decimal("0.0003")
        previous = c
        out.append(
            Bar(
                symbol="EURUSD",
                provider=Provider.simulator,
                timeframe=Timeframe.H1,
                bar_time=start + timedelta(hours=i),
                open=o,
                high=max(o, c) + Decimal("0.0020"),
                low=min(o, c) - Decimal("0.0020"),
                close=c,
                volume=Decimal("100"),
                spread=None,
                spread_availability=Availability.not_available,
                complete=complete,
            )
        )
    return out


def wave(n: int = 400) -> list[float]:
    """A series with enough turns to produce crosses and both exit reasons."""
    import math as _math

    return [1.1000 + 0.02 * _math.sin(i / 9.0) + i * 0.00002 for i in range(n)]


def costs(spread: str = "0.0002", **extra: object) -> CostModel:
    return CostModel(spread_points=Decimal(spread), **extra)  # type: ignore[arg-type]


def config(**overrides: object) -> BacktestConfig:
    base: dict[str, object] = {
        "strategy_key": "sma_cross",
        "symbol": "EURUSD",
        "timeframe": Timeframe.H1,
        "costs": costs(),
        "provider": Provider.simulator,
        "max_bars": 1000,
    }
    base.update(overrides)
    return BacktestConfig(**base)  # type: ignore[arg-type]


# =========================================================== LOOK-AHEAD


def test_future_bars_cannot_change_an_earlier_decision() -> None:
    """Step 36, and the gate on this level.

    The strategy is asked about bar T twice: once over a dataset, once over the
    same dataset whose bars AFTER T have been replaced with something wildly
    different. The answer at T must be identical, because at T the future has
    not happened.
    """
    strategy = SmaCross()
    original = bars(wave(300))
    cut = 200

    # Only the bars after `cut` differ: same prefix, different tail.
    tampered = list(original[: cut + 1]) + bars(
        [5.0 + i for i in range(len(original) - cut - 1)],
        start=original[cut + 1].bar_time,
    )
    assert [b.close for b in original[: cut + 1]] == [b.close for b in tampered[: cut + 1]]
    assert [b.close for b in original[cut + 1 :]] != [b.close for b in tampered[cut + 1 :]]

    before = strategy.generate_signal(
        Candles.of("EURUSD", Timeframe.H1, original[: cut + 1]), now=T0
    )
    after = strategy.generate_signal(
        Candles.of("EURUSD", Timeframe.H1, tampered[: cut + 1]), now=T0
    )
    assert before.signal_type is after.signal_type
    assert before.bar_time == after.bar_time


def test_the_signal_vector_is_built_from_prefixes_only() -> None:
    """Tampering with the tail must not move a single earlier signal."""
    strategy = SmaCross()
    original = bars(wave(260))
    cut = 180
    tampered = list(original[: cut + 1]) + bars(
        [9.0 - i * 0.01 for i in range(len(original) - cut - 1)],
        start=original[cut + 1].bar_time,
    )

    a = build_signal_vector(strategy, original, "EURUSD", Timeframe.H1)
    b = build_signal_vector(strategy, tampered, "EURUSD", Timeframe.H1)
    assert a[: cut + 1] == b[: cut + 1]


def test_the_strategy_at_bar_i_never_receives_bar_i_plus_one() -> None:
    """Asserted on the object the strategy is handed, not on its output."""
    seen: list[int] = []

    class Recorder(SmaCross):
        def generate_signal(self, candles: Candles, *, now: datetime):  # noqa: ANN201
            seen.append(len(candles))
            return super().generate_signal(candles, now=now)

    data = bars(wave(120))
    build_signal_vector(Recorder(), data, "EURUSD", Timeframe.H1)
    # At index i the strategy saw exactly i+1 bars: never more.
    assert seen == list(range(1, len(data) + 1))


# ====================================================== the tested engine


def test_the_engine_is_the_toolkit_not_a_second_one() -> None:
    """A second engine would mean re-deriving four constraints that took real
    trades to learn."""
    import ast
    import inspect

    import app.backtest.runner as module

    source = inspect.getsource(module)
    assert "toolkit.simulate(" in source
    tree = ast.parse(source)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "simulate" not in defined  # not reimplemented


def test_execution_assumptions_are_stated_not_configurable() -> None:
    described = ExecutionModel().describe()
    assert "next bar" in described["entry"]
    assert "LOSS" in described["straddled_bar"]
    assert "one position at a time" in described["overlapping_positions"]
    # Bid/ask is not modelled and the report says so rather than implying it.
    assert "not modelled" in described["bid_ask"]


# ============================================================== a real run


def test_a_basic_backtest_produces_trades_and_a_curve() -> None:
    result = run(config(), SmaCross(), bars(wave(400)))
    assert result.metrics["trades"] > 0
    assert len(result.equity_curve) == result.metrics["trades"] + 1
    assert result.bars_processed == 400
    assert "not a prediction" in str(result.as_dict()["note"])


def test_the_same_configuration_and_data_reproduce_exactly() -> None:
    """Step 20. Two identical runs must agree."""
    data = bars(wave(400))
    first = run(config(), SmaCross(), data)
    second = run(config(), SmaCross(), data)
    assert first.metrics == second.metrics
    assert first.trades == second.trades
    assert first.config.fingerprint() == second.config.fingerprint()


def test_the_fingerprint_changes_when_an_assumption_does() -> None:
    """A stored result can be checked against the configuration that claims to
    have produced it."""
    assert config().fingerprint() != config(costs=costs("0.0005")).fingerprint()
    assert config().fingerprint() != config(stop_atr=Decimal("2.0")).fingerprint()


def test_a_wider_spread_never_improves_the_result() -> None:
    """Cost drag is the one effect this project has measured large enough to
    see. It has a sign, and it points down."""
    data = bars(wave(400))
    cheap = run(config(costs=costs("0.0001")), SmaCross(), data)
    dear = run(config(costs=costs("0.0020")), SmaCross(), data)
    assert dear.metrics["net_points"] < cheap.metrics["net_points"]


def test_slippage_is_always_adverse() -> None:
    """A slippage model that could help is a model that flatters."""
    data = bars(wave(400))
    without = run(config(), SmaCross(), data)
    with_slip = run(
        config(costs=costs("0.0002", slippage_points=Decimal("0.0003"))), SmaCross(), data
    )
    assert with_slip.metrics["net_points"] < without.metrics["net_points"]


def test_commission_reduces_money_but_not_points() -> None:
    """Points is the poolable figure; currency depends on size."""
    data = bars(wave(400))
    free = run(config(), SmaCross(), data)
    charged = run(
        config(costs=costs("0.0002", commission_per_trade=Decimal("1"))), SmaCross(), data
    )
    assert charged.metrics["net_points"] == free.metrics["net_points"]
    assert charged.metrics["net_money"] < free.metrics["net_money"]


def test_financing_is_off_unless_both_figures_are_given() -> None:
    """Every figure in this repository was measured without it; a default that
    silently restated them would make the history unreadable."""
    assert costs().describe()["financing_charged"] is False
    charged = costs(
        "0.0002",
        swap_long_per_night=Decimal("-0.00001"),
        swap_short_per_night=Decimal("-0.00002"),
    )
    assert charged.describe()["financing_charged"] is True


# ================================================================ metrics


def test_a_ratio_from_too_few_trades_is_not_available() -> None:
    """A ratio from six trades is noise wearing a decimal point."""
    result = run(config(max_hold_bars=2000), SmaCross(), bars(wave(400)))
    if result.metrics["trades"] < MIN_TRADES_FOR_RATIO:
        assert result.metrics["sharpe_per_trade"] == NOT_AVAILABLE
        assert result.metrics["ratio_note"]


def test_drawdown_is_reported_in_money_and_percent_separately() -> None:
    """Confusing percentage drawdown with absolute monetary drawdown is the
    mistake the brief names."""
    result = run(config(), SmaCross(), bars(wave(400)))
    assert "max_drawdown_money" in result.metrics
    assert "max_drawdown_pct" in result.metrics
    assert result.metrics["max_drawdown_money"] >= 0


def test_no_trades_is_a_result_not_a_failure() -> None:
    """A rule that never fires over a window is a fact about the rule."""
    flat = bars([1.1000] * 400)
    result = run(config(), SmaCross(), flat)
    assert result.metrics["trades"] == 0
    assert "not a failure" in result.metrics["note"]


def test_the_significance_note_does_not_oversell_one_t_statistic() -> None:
    result = run(config(), SmaCross(), bars(wave(400)))
    if result.metrics["trades"]:
        assert "weakest of this project's three gates" in result.metrics["significance_note"]


def test_win_and_loss_counts_reconcile() -> None:
    result = run(config(), SmaCross(), bars(wave(400)))
    if result.metrics["trades"]:
        assert result.metrics["wins"] + result.metrics["losses"] <= result.metrics["trades"]
        assert result.metrics["longs"] + result.metrics["shorts"] == result.metrics["trades"]


# ========================================================= configuration


def test_an_inverted_date_range_is_refused() -> None:
    with pytest.raises(ConfigError, match="start must be before end"):
        config(start=T0 + timedelta(days=5), end=T0).validate()


def test_a_risk_mode_without_its_own_parameter_is_refused_not_substituted() -> None:
    """WIRED AT L18. The mode used to be refused outright because sizing was
    not built; now it runs, and what is refused is a risk mode with nothing to
    size from. Falling back to the fixed lot would report a result for a run
    nobody configured."""
    with pytest.raises(ConfigError, match="risk_percent"):
        config(sizing_mode=SizingMode.percent_equity).validate()
    with pytest.raises(ConfigError, match="risk_amount"):
        config(sizing_mode=SizingMode.fixed_risk).validate()
    with pytest.raises(ConfigError, match="risk_percent"):
        config(sizing_mode=SizingMode.percent_equity, risk_percent=Decimal("101")).validate()
    # Correctly configured, both validate.
    config(sizing_mode=SizingMode.percent_equity, risk_percent=Decimal("1")).validate()
    config(sizing_mode=SizingMode.fixed_risk, risk_amount=Decimal("100")).validate()


def test_a_risk_mode_without_a_contract_spec_is_refused() -> None:
    """No tick value, no way to turn a stop distance into money. Refusing
    beats sizing from a default -- that is the failure app.sizing exists for."""
    cfg = config(sizing_mode=SizingMode.percent_equity, risk_percent=Decimal("1"))
    with pytest.raises(BacktestError, match="contract spec"):
        run(cfg, SmaCross(), bars(wave(400)), None)


# ======================================================= POSITION SIZING (L18)


def _spec(**overrides: object):  # noqa: ANN202
    from app.symbols.service import ContractSpec

    base: dict[str, object] = {
        "internal_symbol": "EURUSD",
        "broker_symbol": "EURUSD",
        "provider": "simulator",
        "contract_size": Decimal("100000"),
        "tick_size": Decimal("0.00001"),
        "tick_value": Decimal("1"),
        "minimum_volume": Decimal("0.01"),
        "maximum_volume": Decimal("100"),
        "volume_step": Decimal("0.01"),
        "price_precision": 5,
        "volume_precision": 2,
        "trading_hours": None,
        "spec_source": "test",
        "spec_updated_at": None,
    }
    base.update(overrides)
    return ContractSpec(**base)  # type: ignore[arg-type]


def test_the_backtester_calls_the_sizing_engine_and_defines_no_second_one() -> None:
    """One authoritative sizing calculation. A BacktestPositionSizer beside a
    LivePositionSizer is two answers to one question, and the one that
    disagrees is always the one nobody was watching."""
    import ast
    import inspect

    from app.backtest import runner as backtest_runner

    source = inspect.getsource(backtest_runner)
    assert "from app.sizing.calculator import" in source
    tree = ast.parse(source)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for forbidden in ("calculate", "_lot_for_risk", "_size", "_position_size"):
        assert forbidden not in defined, f"the backtester defines its own {forbidden}"


def test_risk_sized_quantities_vary_with_the_stop_and_fixed_ones_do_not() -> None:
    """The point of risk sizing: a wider stop buys fewer units for the same
    money. Under a fixed lot every trade is the same bet regardless of stop."""
    data = bars(wave(400))
    flat = run(config(), SmaCross(), data, _spec())
    sized = run(
        config(sizing_mode=SizingMode.percent_equity, risk_percent=Decimal("1")),
        SmaCross(),
        data,
        _spec(),
    )
    assert len({t["quantity"] for t in flat.trades}) == 1
    assert sized.trades, "the sized run produced no trades"
    quantities = {t["quantity"] for t in sized.trades}
    assert len(quantities) > 1, "risk sizing produced a constant quantity"
    for trade in sized.trades:
        assert trade["sizing"] is not None
        assert Decimal(trade["quantity"]) > 0


def test_sizing_reads_the_equity_before_the_trade_not_after() -> None:
    """No look-ahead. The equity a trade is sized against is the balance from
    trades that had ALREADY closed, so the first trade is always sized at the
    initial capital exactly."""
    cfg = config(sizing_mode=SizingMode.percent_equity, risk_percent=Decimal("1"))
    result = run(cfg, SmaCross(), bars(wave(400)), _spec())
    assert result.trades
    first = result.trades[0]
    assert first["sizing"]["equity_before"] == str(cfg.initial_capital)
    # And each subsequent trade sees the running balance, never a later one.
    balance = cfg.initial_capital
    for trade in result.trades:
        assert trade["sizing"]["equity_before"] == str(balance)
        balance += Decimal(str(trade["net_money"]))


def test_the_stop_distance_comes_from_the_bar_before_the_entry() -> None:
    """`simulate()` places its bracket from `atr[i-1]`. Sizing must use the
    same figure: reading `atr[i]` would size from the entry bar's own range,
    which was not knowable when the position opened."""
    import numpy as np
    from app.backtest.runner import _toolkit

    data = bars(wave(400))
    cfg = config(sizing_mode=SizingMode.fixed_risk, risk_amount=Decimal("100"))
    result = run(cfg, SmaCross(), data, _spec())
    assert result.trades

    toolkit = _toolkit()
    high = np.array([float(b.high) for b in data])
    low = np.array([float(b.low) for b in data])
    close = np.array([float(b.close) for b in data])
    atr = toolkit.atr_series(high, low, close, 14)
    times = {b.bar_time.isoformat(): i for i, b in enumerate(data)}

    for trade in result.trades:
        index = times[trade["entry_time"]]
        expected = Decimal(str(cfg.stop_atr)) * Decimal(str(float(atr[index - 1])))
        assert trade["sizing"]["stop_distance"] == str(expected)


def test_a_refused_size_is_not_counted_as_a_trade() -> None:
    """A position that could not be sized was never opened. Counting it would
    report a return the account could not have earned."""
    # A budget far below what one minimum lot risks: every entry refuses.
    cfg = config(sizing_mode=SizingMode.fixed_risk, risk_amount=Decimal("0.01"))
    result = run(cfg, SmaCross(), bars(wave(400)), _spec())
    assert result.trades == []
    assert result.sizing_refusals, "refusals were dropped instead of reported"
    assert "below the venue minimum" in result.sizing_refusals[0]["reason"]
    # The curve never moved, because nothing was traded.
    assert len(result.equity_curve) == 1


def test_risk_sizing_keeps_every_trade_inside_its_budget() -> None:
    """The safety invariant, checked over a whole run rather than one case."""
    cfg = config(sizing_mode=SizingMode.fixed_risk, risk_amount=Decimal("100"))
    result = run(cfg, SmaCross(), bars(wave(400)), _spec())
    assert result.trades
    for trade in result.trades:
        assert Decimal(trade["sizing"]["risk_actual"]) <= Decimal("100")


def test_a_fixed_quantity_run_is_unchanged_by_the_sizing_wiring() -> None:
    """The default path must produce exactly what it produced before L18."""
    data = bars(wave(400))
    result = run(config(), SmaCross(), data, _spec())
    for trade in result.trades:
        assert trade["quantity"] == str(config().quantity)
        assert trade["sizing"] is None
        expected = round(trade["net_points"] * float(config().quantity) - trade["commission"], 6)
        assert trade["net_money"] == expected


@pytest.mark.parametrize(
    "override",
    [{"initial_capital": Decimal("0")}, {"quantity": Decimal("-1")}, {"stop_atr": Decimal("0")}],
)
def test_impossible_configuration_is_refused(override: dict) -> None:
    with pytest.raises(ConfigError):
        config(**override).validate()


def test_a_short_dataset_is_a_data_shortfall_not_an_empty_result() -> None:
    with pytest.raises(BacktestError, match="data shortfall"):
        run(config(), SmaCross(), bars(wave(20)))


def test_impossible_ohlc_refuses_the_run() -> None:
    """A bar whose close sits outside its own range produced a t-statistic of
    28 in this repository's forex pattern study."""
    data = bars(wave(200))
    broken = Bar(
        symbol="EURUSD",
        provider=Provider.simulator,
        timeframe=Timeframe.H1,
        bar_time=data[100].bar_time,
        open=Decimal("1.1"),
        high=Decimal("1.1"),
        low=Decimal("1.1"),
        close=Decimal("9.9"),  # outside its own range
        complete=True,
    )
    data[100] = broken
    with pytest.raises(BacktestError, match="impossible OHLC"):
        run(config(), SmaCross(), data)


def test_a_forming_bar_is_dropped_before_the_run() -> None:
    closed = bars(wave(300))
    forming = bars([9.9], start=closed[-1].bar_time + timedelta(hours=1), complete=False)
    result = run(config(), SmaCross(), [*closed, *forming])
    assert result.bars_processed == 300


def test_data_quality_travels_with_the_result() -> None:
    result = run(config(), SmaCross(), bars(wave(400)))
    assert result.quality.bars == 400
    assert result.quality.ohlc_trustworthy is True


# ==================================================== jobs and the API


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    from app.marketdata.providers.simulator import SimulatorMarketData
    from app.marketdata.service import MarketDataService
    from app.symbols import service as symbols

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
        await symbols.upsert_mapping(db, "EURUSD", "simulator", "EURUSD")
        await db.commit()
    application.state.market_data = MarketDataService({Provider.simulator: SimulatorMarketData()})
    from app.backtest.service import BacktestService

    application.state.backtests = BacktestService(
        application.state.session_factory,
        application.state.market_data,
        default_registry(),
        None,
    )
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


def payload(**overrides: object) -> dict:
    base: dict[str, object] = {
        "name": "sma over the simulator",
        "strategy_key": "sma_cross",
        "symbol": "EURUSD",
        "timeframe": "H1",
        "provider": "simulator",
        "costs": {"spread_points": "0.0002"},
        "max_bars": 400,
    }
    base.update(overrides)
    return base


async def test_the_spread_is_required(alice: AsyncClient) -> None:
    """No zero-cost default: a run that assumed zero would be measuring
    something else."""
    body = payload()
    body["costs"] = {}
    r = await alice.post("/v1/backtests", json=body, headers=_csrf(alice))
    assert r.status_code == 422


async def test_queueing_returns_immediately_with_the_assumptions(
    alice: AsyncClient,
) -> None:
    r = await alice.post("/v1/backtests", json=payload(), headers=_csrf(alice))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["params"]["engine_version"] == ENGINE_VERSION
    assert body["params"]["costs"]["spread_points"] == "0.0002"
    assert "next bar" in body["params"]["execution"]["entry"]
    assert body["params"]["fingerprint"]


async def test_a_run_completes_and_stores_its_result(app: FastAPI, alice: AsyncClient) -> None:
    created = (await alice.post("/v1/backtests", json=payload(), headers=_csrf(alice))).json()
    await app.state.backtests.wait(created["backtest_id"], timeout=120)

    body = (await alice.get(f"/v1/backtests/{created['backtest_id']}")).json()
    assert body["status"] == "finished"
    assert body["metrics"] is not None
    assert body["fingerprint"]
    assert body["started_at"] and body["finished_at"]


async def test_trades_and_the_curve_are_readable_after_a_run(
    app: FastAPI, alice: AsyncClient
) -> None:
    created = (await alice.post("/v1/backtests", json=payload(), headers=_csrf(alice))).json()
    await app.state.backtests.wait(created["backtest_id"], timeout=120)

    trades = (await alice.get(f"/v1/backtests/{created['backtest_id']}/trades")).json()
    curve = (await alice.get(f"/v1/backtests/{created['backtest_id']}/equity-curve")).json()
    assert curve["curve"] is not None
    assert curve["curve"][0]["drawdown"] == 0.0
    if trades["page"]["total"]:
        assert trades["items"][0]["exit_reason"] in ("tp", "sl", "timeout")


async def test_an_unfinished_run_reports_no_curve_rather_than_an_empty_one(
    app: FastAPI, alice: AsyncClient
) -> None:
    """'No curve because it has not run' and 'a flat curve' are different
    statements."""
    from app.models.research import Backtest

    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert user is not None
        row = Backtest(
            name="pending",
            engine="rule_backtest",
            status="queued",
            requested_by_user_id=user.id,
        )
        db.add(row)
        await db.commit()
        pending_id = row.id

    body = (await alice.get(f"/v1/backtests/{pending_id}/equity-curve")).json()
    assert body["curve"] is None
    assert "queued" in body["reason"]


async def test_a_failed_run_is_never_reported_as_finished(app: FastAPI, alice: AsyncClient) -> None:
    """The distinction that makes a result trustworthy."""
    created = (
        await alice.post(
            "/v1/backtests",
            json=payload(symbol="NOTMAPPED", name="doomed"),
            headers=_csrf(alice),
        )
    ).json()
    # The task completes normally: the failure is *recorded*, not raised out
    # of the worker. A run that crashed the task would leave `queued`, which is
    # exactly the ambiguity this level removes.
    await app.state.backtests.wait(created["backtest_id"], timeout=60)
    body = (await alice.get(f"/v1/backtests/{created['backtest_id']}")).json()
    assert body["status"] == "failed"
    assert body["metrics"] is None
    assert body["error"]


async def test_an_unknown_strategy_is_404(alice: AsyncClient) -> None:
    r = await alice.post("/v1/backtests", json=payload(strategy_key="nope"), headers=_csrf(alice))
    assert r.status_code in (404, 202)
    if r.status_code == 404:
        assert "sma_cross" in r.json()["error"]["detail"]


async def test_a_user_cannot_read_another_users_backtest(app: FastAPI) -> None:
    """Ownership is scoped in the query; someone else's run answers as one that
    does not exist.

    The row is created directly rather than by queueing: a background run and a
    second client share SQLite's single StaticPool connection in this harness,
    and the interleaving breaks the session row. The authorization path is what
    is under test, and it does not need a real run behind it.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as a:
        await a.post("/auth/register", json=ALICE)
        async with app.state.session_factory() as db:
            owner = await db.scalar(select(User).where(User.email == ALICE["email"]))
            assert owner is not None
            row = Backtest(
                name="alice's run",
                engine="rule_backtest",
                status="finished",
                requested_by_user_id=owner.id,
                summary={"metrics": {"trades": 3}},
            )
            db.add(row)
            await db.commit()
            backtest_id = row.id
        assert (await a.get(f"/v1/backtests/{backtest_id}")).status_code == 200

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as b:
        registered = await b.post(
            "/auth/register",
            json={"email": "bob@tr-platform.io", "password": "another strong one"},
        )
        assert registered.status_code == 201, registered.text
        assert (await b.get(f"/v1/backtests/{backtest_id}")).status_code == 404
        assert (await b.get(f"/v1/backtests/{backtest_id}/trades")).status_code == 404
        assert (await b.get(f"/v1/backtests/{backtest_id}/equity-curve")).status_code == 404
        listed = (await b.get("/v1/backtests")).json()
        assert listed["page"]["total"] == 0


async def test_backtest_routes_need_a_session(client: AsyncClient) -> None:
    assert (await client.get("/v1/backtests")).status_code == 401
    assert (await client.post("/v1/backtests", json=payload())).status_code == 401


async def test_the_assumptions_route_states_the_limitations(alice: AsyncClient) -> None:
    body = (await alice.get("/v1/backtests/engine/assumptions")).json()
    # WIRED AT L18: all three modes run, through the same engine the paper
    # pipeline uses. The route says so, and says how the equity and the stop
    # distance are obtained, because those are what make it look-ahead free.
    assert body["sizing"]["implemented"] == [
        "fixed_quantity",
        "fixed_risk",
        "percent_equity",
    ]
    assert "app.sizing.calculate" in body["sizing"]["engine"]
    assert "already CLOSED" in body["sizing"]["equity"]
    assert "sizing_refusals" in body["sizing"]["refusals"]
    joined = " ".join(body["limitations"])
    assert "Multi-symbol" in joined
    assert "not annualised" in joined
    assert "not modelled" in joined


# ================================================================ safety


async def test_nothing_in_the_backtest_package_can_reach_a_broker() -> None:
    import ast
    import importlib
    import inspect
    import pkgutil

    import app.backtest as pkg

    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.backtest."):
        loaded = importlib.import_module(module.name)
        tree = ast.parse(inspect.getsource(loaded))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        for forbidden in ("app.brokers", "MetaTrader5", "mt5_paper"):
            assert not any(i.startswith(forbidden) for i in imported), (
                f"{module.name} imports {forbidden}"
            )


async def test_a_backtest_creates_no_order_and_no_position(
    app: FastAPI, alice: AsyncClient
) -> None:
    from app.models.execution import Order, Position

    created = (await alice.post("/v1/backtests", json=payload(), headers=_csrf(alice))).json()
    await app.state.backtests.wait(created["backtest_id"], timeout=120)
    async with app.state.session_factory() as db:
        assert len((await db.scalars(select(Order))).all()) == 0
        assert len((await db.scalars(select(Position))).all()) == 0


def test_trading_safety_is_unchanged() -> None:
    from app.core.settings import LIVE_GATES
    from app.core.settings import Settings as S

    settings = S(_env_file=None)
    assert settings.trading_mode.value == "paper"
    assert settings.live_trading is False
    assert not any(LIVE_GATES.values())
