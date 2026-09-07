"""Market replay: consistency with L14, determinism, controls and safety.

Three tests carry this level:

  * `test_replay_matches_the_backtest_exactly` — the incremental engine and
    L14's batch simulator produce the identical trade list over the same data.
    Two financial engines that disagreed would be worse than one that is slow.
  * `test_speed_does_not_change_the_result` — 1x and 100x produce the same
    trades, because `advance()` never consults speed.
  * `test_future_bars_cannot_change_an_earlier_replay_decision` — the tail is
    replaced and the decision at T is unchanged.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.backtest.config import BacktestConfig, CostModel
from app.backtest.runner import run as backtest_run
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.replay.clock import (
    SPEEDS,
    IllegalTransition,
    ReplayClock,
    ReplayState,
    SpeedError,
    check_speed,
    check_transition,
)
from app.replay.engine import EventKind, ReplayEngine, atr_for
from app.replay.service import run_to_completion
from app.strategies.rules import SmaCross
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}


def bars(closes: list[float], *, start: datetime = T0) -> list[Bar]:
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
                complete=True,
            )
        )
    return out


def wave(n: int = 400) -> list[float]:
    return [1.1000 + 0.02 * math.sin(i / 9.0) + i * 0.00002 for i in range(n)]


def config(**overrides: object) -> BacktestConfig:
    base: dict[str, object] = {
        "strategy_key": "sma_cross",
        "symbol": "EURUSD",
        "timeframe": Timeframe.H1,
        "costs": CostModel(spread_points=Decimal("0.0002")),
        "provider": Provider.simulator,
        "max_bars": 1000,
    }
    base.update(overrides)
    return BacktestConfig(**base)  # type: ignore[arg-type]


# ================================= CONSISTENCY WITH THE BACKTESTER


def _comparable(trade: dict) -> tuple:
    return (
        trade["side"],
        trade["entry_time"],
        trade["exit_time"],
        trade["exit_reason"],
        round(float(trade["net_points"]), 8),
    )


@pytest.mark.parametrize("series", [wave(400), wave(250), wave(600)], ids=["400", "250", "600"])
def test_replay_matches_the_backtest_exactly(series: list[float]) -> None:
    """The incremental engine and L14's batch simulator must agree.

    Two financial engines that disagreed would be worse than one that is slow,
    and the disagreement would be invisible until somebody compared a replay to
    a backtest and could not explain the difference.
    """
    data = bars(series)
    cfg = config()
    batch = backtest_run(cfg, SmaCross(), data)
    incremental = run_to_completion(cfg, SmaCross(), data)

    assert [_comparable(t) for t in incremental.portfolio.trades] == [
        _comparable(t) for t in batch.trades
    ]


def test_replay_and_backtest_agree_on_the_metrics() -> None:
    data = bars(wave(400))
    cfg = config()
    batch = backtest_run(cfg, SmaCross(), data)
    incremental = run_to_completion(cfg, SmaCross(), data)
    for key in ("trades", "win_rate", "net_points", "profit_factor", "exit_mix"):
        assert incremental.metrics()[key] == batch.metrics[key], key


def test_financing_is_charged_identically_by_both_engines() -> None:
    """A financed run must agree too, or `costs` means two different things.

    Replay counts nights with `swap.nights_between` -- the same function
    `simulate()` calls, not a second implementation. A weekend with one weekday
    billed triple is exactly the arithmetic that drifts when written twice.
    """
    data = bars(wave(400))
    cfg = config(
        costs=CostModel(
            spread_points=Decimal("0.0002"),
            swap_long_per_night=Decimal("-0.00002"),
            swap_short_per_night=Decimal("-0.00005"),
        )
    )
    batch = backtest_run(cfg, SmaCross(), data)
    incremental = run_to_completion(cfg, SmaCross(), data)
    assert [_comparable(t) for t in incremental.portfolio.trades] == [
        _comparable(t) for t in batch.trades
    ]
    # And it must actually cost something, or the test would pass on a no-op.
    unfinanced = run_to_completion(config(), SmaCross(), data)
    financed_net = float(incremental.metrics()["net_points"])  # type: ignore[arg-type]
    plain_net = float(unfinanced.metrics()["net_points"])  # type: ignore[arg-type]
    assert financed_net < plain_net
    assert any(t["nights"] > 0 for t in incremental.portfolio.trades)


def test_a_wider_spread_moves_both_engines_the_same_way() -> None:
    data = bars(wave(400))
    cfg = config(costs=CostModel(spread_points=Decimal("0.0015")))
    batch = backtest_run(cfg, SmaCross(), data)
    incremental = run_to_completion(cfg, SmaCross(), data)
    assert incremental.metrics()["net_points"] == batch.metrics["net_points"]


# ============================================= LOOK-AHEAD AND DETERMINISM


def test_future_bars_cannot_change_an_earlier_replay_decision() -> None:
    """Replace only the tail; the decisions before the cut must be identical."""
    original = bars(wave(300))
    cut = 200
    tampered = list(original[: cut + 1]) + bars(
        [5.0 + i for i in range(len(original) - cut - 1)], start=original[cut + 1].bar_time
    )

    a = run_to_completion(config(), SmaCross(), original)
    b = run_to_completion(config(), SmaCross(), tampered)
    assert a.signals[: cut + 1] == b.signals[: cut + 1]


def test_an_indicator_at_t_does_not_move_when_the_future_does() -> None:
    """ATR at index i depends only on bars up to i, which is why it is safe to
    compute once. This asserts that rather than assuming it."""
    original = bars(wave(300))
    cut = 200
    tampered = list(original[: cut + 1]) + bars(
        [9.0 for _ in range(len(original) - cut - 1)], start=original[cut + 1].bar_time
    )
    a, b = atr_for(original)[:cut], atr_for(tampered)[:cut]
    # NaN never equals itself, so the warm-up region is compared as such.
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        assert (math.isnan(x) and math.isnan(y)) or x == y


def test_the_same_session_replayed_twice_is_identical() -> None:
    data = bars(wave(400))
    a = run_to_completion(config(), SmaCross(), data)
    b = run_to_completion(config(), SmaCross(), data)
    assert a.portfolio.trades == b.portfolio.trades
    assert a.portfolio.equity_curve == b.portfolio.equity_curve
    assert a.metrics() == b.metrics()


def test_two_sessions_do_not_share_state() -> None:
    """No module-level mutable state, so concurrency is safe rather than merely
    untested."""
    data = bars(wave(400))
    a = ReplayEngine(config(), SmaCross(), data, atr_for(data))
    b = ReplayEngine(config(initial_capital=Decimal("50000")), SmaCross(), data, atr_for(data))
    clock = ReplayClock([x.bar_time for x in data])
    for _ in range(120):
        index = clock.advance()
        assert index is not None
        a.step(index, clock.next_sequence)
    assert b.portfolio.balance == 50000.0
    assert b.position is None
    assert a.portfolio.initial_balance != b.portfolio.initial_balance


# ======================================================== THE CLOCK


def test_simulated_time_is_the_bar_not_the_wall_clock() -> None:
    data = bars(wave(100))
    clock = ReplayClock([b.bar_time for b in data])
    assert clock.now() is None  # nothing revealed yet
    clock.advance()
    assert clock.now() == data[0].bar_time
    clock.advance()
    assert clock.now() == data[1].bar_time


def test_advance_reveals_exactly_one_bar() -> None:
    data = bars(wave(50))
    clock = ReplayClock([b.bar_time for b in data])
    for expected in range(len(data)):
        assert clock.advance() == expected
        assert clock.revealed == expected + 1
    assert clock.advance() is None  # exhausted


def test_speed_paces_but_never_advances() -> None:
    """`advance()` does not consult speed, which is why speed cannot skip,
    reorder or merge an event."""
    data = bars(wave(50))
    slow = ReplayClock([b.bar_time for b in data], speed=1.0)
    fast = ReplayClock([b.bar_time for b in data], speed=100.0)
    assert [slow.advance() for _ in range(10)] == [fast.advance() for _ in range(10)]
    assert fast.pacing_delay() < slow.pacing_delay()


@pytest.mark.parametrize("speed", list(SPEEDS))
def test_every_offered_speed_is_accepted(speed: float) -> None:
    assert check_speed(speed) == speed


@pytest.mark.parametrize("bad", [0, -1, 1000])
def test_an_unsupported_speed_is_refused_not_clamped(bad: float) -> None:
    with pytest.raises(SpeedError):
        check_speed(bad)


def test_zero_speed_is_refused_because_pausing_is_a_state() -> None:
    with pytest.raises(SpeedError, match="pausing is a state"):
        check_speed(0)


def test_out_of_order_timestamps_are_refused_not_sorted() -> None:
    """A dataset that arrived out of order is a data problem; reordering it here
    would hide it from the validation that should have caught it."""
    data = bars(wave(10))
    times = [b.bar_time for b in data]
    times[3], times[7] = times[7], times[3]
    with pytest.raises(ValueError, match="ascending"):
        ReplayClock(times)


def test_sequence_numbers_are_monotonic() -> None:
    """Frontend synchronisation needs to order a bar, a signal, a risk decision
    and a fill that all share one simulated timestamp."""
    clock = ReplayClock([b.bar_time for b in bars(wave(10))])
    produced = [clock.next_sequence() for _ in range(5)]
    assert produced == sorted(produced)
    assert len(set(produced)) == 5


# ========================================== SPEED INDEPENDENCE, END TO END


def test_speed_does_not_change_the_result() -> None:
    """The same session at 1x and 100x produces the same trades."""
    data = bars(wave(400))

    def drive(speed: float):  # noqa: ANN202
        clock = ReplayClock([b.bar_time for b in data], speed=speed)
        engine = ReplayEngine(config(), SmaCross(), data, atr_for(data))
        while True:
            index = clock.advance()
            if index is None:
                return engine
            engine.step(index, clock.next_sequence)

    slow, fast = drive(1.0), drive(100.0)
    assert slow.portfolio.trades == fast.portfolio.trades
    assert slow.metrics() == fast.metrics()


# ============================================== THE STATE MACHINE


@pytest.mark.parametrize(
    "current,wanted",
    [
        (ReplayState.created, ReplayState.running),
        (ReplayState.running, ReplayState.paused),
        (ReplayState.paused, ReplayState.running),
        (ReplayState.running, ReplayState.completed),
        (ReplayState.paused, ReplayState.stopped),
    ],
)
def test_legal_transitions(current: ReplayState, wanted: ReplayState) -> None:
    check_transition(current, wanted)


@pytest.mark.parametrize(
    "current,wanted",
    [
        (ReplayState.completed, ReplayState.running),
        (ReplayState.stopped, ReplayState.running),
        (ReplayState.failed, ReplayState.running),
        (ReplayState.created, ReplayState.paused),
    ],
)
def test_illegal_transitions_are_refused(current: ReplayState, wanted: ReplayState) -> None:
    """A completed session that could resume would produce a second, different
    result under the same id."""
    with pytest.raises(IllegalTransition):
        check_transition(current, wanted)


def test_the_replay_states_map_onto_the_schema_vocabulary() -> None:
    """Not a parallel vocabulary that would need translating at every boundary."""
    assert str(ReplayState.created) == "queued"
    assert str(ReplayState.completed) == "finished"
    assert str(ReplayState.stopped) == "cancelled"
    assert str(ReplayState.paused) == "paused"


# ================================================= EXECUTION SEMANTICS


def test_a_bar_covering_both_stop_and_target_books_the_loss() -> None:
    """Intrabar order is unknown, and resolving it in the strategy's favour is
    how a backtest flatters itself. The same rule L14 enforces."""
    data = bars(wave(400))
    engine = run_to_completion(config(), SmaCross(), data)
    # Every stop exit must be reported as a loss-side reason, never as a target.
    stops = [t for t in engine.portfolio.trades if t["exit_reason"] == "sl"]
    assert stops
    for trade in stops:
        assert trade["gross_points"] < 0


def test_positions_never_overlap() -> None:
    """Flat before the next entry: `simulate()`'s `i = j + 1`."""
    engine = run_to_completion(config(), SmaCross(), bars(wave(400)))
    trades = engine.portfolio.trades
    for earlier, later in zip(trades, trades[1:], strict=False):
        assert later["entry_time"] > earlier["exit_time"]


def test_every_trade_is_labelled_replay() -> None:
    """So a replay result can never be pooled with a live, paper or backtest
    record by accident."""
    engine = run_to_completion(config(), SmaCross(), bars(wave(400)))
    assert engine.portfolio.trades
    assert all(t["execution_mode"] == "REPLAY" for t in engine.portfolio.trades)


def test_every_entry_records_a_real_risk_decision() -> None:
    """Wired at L17. Replay used to record `not_enforced`; it now evaluates.

    Simulation is not an exemption: a replay whose risk rules differed from
    paper's would be testing a strategy under a safety architecture that does
    not exist anywhere else.
    """
    data = bars(wave(400))
    clock = ReplayClock([b.bar_time for b in data])
    engine = ReplayEngine(config(), SmaCross(), data, atr_for(data))
    decisions = []
    while True:
        index = clock.advance()
        if index is None:
            break
        for event in engine.step(index, clock.next_sequence):
            if event.kind is EventKind.risk:
                decisions.append(event.payload["decision"])
    assert decisions
    assert set(decisions) == {"approve"}, set(decisions)
    assert engine.risk_verdicts
    assert all(v.approved for v in engine.risk_verdicts)


def test_replay_risk_uses_simulated_time_not_the_wall_clock() -> None:
    """Otherwise every signal in a historical replay is years stale.

    The freshness check compares the signal time against `now`. Measured
    against the machine's clock, a 2026-01 replay run today fails every one --
    so `now` is the bar's time, exactly as it is for the strategy.
    """
    from app.risk.engine import RiskEngine, RiskLimits

    data = bars(wave(400))
    clock = ReplayClock([b.bar_time for b in data])
    strict = RiskEngine(
        RiskLimits(
            one_position_per_symbol=False,
            require_stop_loss=False,
            max_signal_age_seconds=60.0,
        )
    )
    engine = ReplayEngine(config(), SmaCross(), data, atr_for(data), risk=strict)
    while True:
        index = clock.advance()
        if index is None:
            break
        engine.step(index, clock.next_sequence)
    assert engine.risk_verdicts, "no entry was ever proposed"
    # A 60-second freshness limit against simulated time still passes, because
    # the signal is stamped with the bar being replayed.
    assert all(v.approved for v in engine.risk_verdicts)


def test_a_risk_veto_stops_a_replay_entry() -> None:
    """A veto is a veto in replay exactly as in paper."""
    from app.risk.engine import KillSwitches, RiskEngine, RiskLimits

    data = bars(wave(400))
    clock = ReplayClock([b.bar_time for b in data])
    engine = ReplayEngine(
        config(),
        SmaCross(),
        data,
        atr_for(data),
        risk=RiskEngine(
            RiskLimits(require_stop_loss=False),
            KillSwitches(global_stop=True, global_reason="drill"),
        ),
    )
    while True:
        index = clock.advance()
        if index is None:
            break
        engine.step(index, clock.next_sequence)
    assert engine.risk_verdicts
    assert not any(v.approved for v in engine.risk_verdicts)
    assert engine.portfolio.trades == []
    assert engine.position is None


def test_events_carry_simulated_time_and_a_sequence() -> None:
    data = bars(wave(200))
    clock = ReplayClock([b.bar_time for b in data])
    engine = ReplayEngine(config(), SmaCross(), data, atr_for(data))
    index = clock.advance()
    assert index is not None
    events = engine.step(index, clock.next_sequence)
    assert events
    assert all(e.at == data[0].bar_time for e in events)
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)


# ====================================================== API AND SAFETY


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    from app.marketdata.providers.simulator import SimulatorMarketData
    from app.marketdata.service import MarketDataService
    from app.replay.service import ReplayService
    from app.strategies.registry import default_registry
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
    application.state.replay = ReplayService(
        application.state.session_factory,
        application.state.market_data,
        default_registry(),
        None,
    )
    yield application
    await application.state.replay.shutdown()
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
        "strategy_key": "sma_cross",
        "symbol": "EURUSD",
        "timeframe": "H1",
        "provider": "simulator",
        "costs": {"spread_points": "0.0002"},
        "max_bars": 200,
    }
    base.update(overrides)
    return base


async def _create(client: AsyncClient) -> dict:
    r = await client.post("/v1/replay/sessions", json=payload(), headers=_csrf(client))
    assert r.status_code == 201, r.text
    return r.json()


async def test_a_new_session_starts_queued_with_nothing_revealed(
    alice: AsyncClient,
) -> None:
    body = await _create(alice)
    assert body["state"] == "queued"
    assert body["simulated_time"] is None
    assert body["bars_revealed"] == 0
    assert body["execution_mode"] == "REPLAY"
    assert "No broker adapter is reachable" in body["note"]


async def test_step_advances_exactly_one_bar(alice: AsyncClient) -> None:
    created = await _create(alice)
    sid = created["session_id"]
    for expected in range(1, 4):
        r = await alice.post(f"/v1/replay/sessions/{sid}/step", headers=_csrf(alice))
        assert r.status_code == 200
        body = r.json()
        assert body["bars_revealed"] == expected
        assert body["simulated_time"] is not None
        assert body["events"]


async def test_stepping_a_running_session_is_refused(alice: AsyncClient) -> None:
    """Stepping a running session would race the driver and reveal two bars for
    one request."""
    created = await _create(alice)
    sid = created["session_id"]
    await alice.post(f"/v1/replay/sessions/{sid}/start", headers=_csrf(alice))
    r = await alice.post(f"/v1/replay/sessions/{sid}/step", headers=_csrf(alice))
    assert r.status_code == 409
    await alice.post(f"/v1/replay/sessions/{sid}/stop", headers=_csrf(alice))


async def test_pause_stops_the_cursor_and_resume_continues_from_it(
    app: FastAPI, alice: AsyncClient
) -> None:
    created = await _create(alice)
    sid = created["session_id"]
    await alice.post(f"/v1/replay/sessions/{sid}/step", headers=_csrf(alice))
    await alice.post(f"/v1/replay/sessions/{sid}/step", headers=_csrf(alice))

    session = app.state.replay.live[sid]
    await app.state.replay.start(session)
    await app.state.replay.pause(session)
    frozen = session.snapshot()["bars_revealed"]
    await asyncio.sleep(0.2)
    # The cursor must not move while paused.
    assert session.snapshot()["bars_revealed"] == frozen

    await app.state.replay.resume(session)
    await asyncio.sleep(0.05)
    await app.state.replay.stop(session)
    assert session.snapshot()["bars_revealed"] >= frozen


async def test_a_stopped_session_cannot_be_restarted(app: FastAPI, alice: AsyncClient) -> None:
    created = await _create(alice)
    sid = created["session_id"]
    await alice.post(f"/v1/replay/sessions/{sid}/stop", headers=_csrf(alice))
    r = await alice.post(f"/v1/replay/sessions/{sid}/start", headers=_csrf(alice))
    assert r.status_code == 409


async def test_speed_can_be_changed_and_a_bad_one_is_refused(
    alice: AsyncClient,
) -> None:
    created = await _create(alice)
    sid = created["session_id"]
    ok = await alice.patch(
        f"/v1/replay/sessions/{sid}/speed", json={"speed": 10}, headers=_csrf(alice)
    )
    assert ok.status_code == 200
    assert ok.json()["speed"] == 10.0
    bad = await alice.patch(
        f"/v1/replay/sessions/{sid}/speed", json={"speed": 0}, headers=_csrf(alice)
    )
    assert bad.status_code == 422


async def test_the_spread_is_required(alice: AsyncClient) -> None:
    body = payload()
    del body["costs"]
    r = await alice.post("/v1/replay/sessions", json=body, headers=_csrf(alice))
    assert r.status_code == 422


async def test_replay_and_backtest_take_the_same_request_shape() -> None:
    """One configuration, two endpoints. A field on one and not the other would
    be a divergence nobody would notice until the results disagreed."""
    from app.api.v1.backtests import BacktestIn, CostsIn
    from app.api.v1.replay import CreateIn

    assert CreateIn.model_fields["costs"].annotation is CostsIn
    shared = set(BacktestIn.model_fields) - {"name"}
    assert shared <= set(CreateIn.model_fields), shared - set(CreateIn.model_fields)


async def test_an_unmapped_symbol_is_refused(alice: AsyncClient) -> None:
    r = await alice.post(
        "/v1/replay/sessions", json=payload(symbol="NOTMAPPED"), headers=_csrf(alice)
    )
    assert r.status_code in (404, 422, 503)


async def test_replay_routes_need_a_session(client: AsyncClient) -> None:
    assert (await client.get("/v1/replay/sessions")).status_code == 401
    assert (await client.post("/v1/replay/sessions", json=payload())).status_code == 401


async def test_a_user_cannot_reach_another_users_session(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as a:
        await a.post("/auth/register", json=ALICE)
        created = await _create(a)
        sid = created["session_id"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as b:
        registered = await b.post(
            "/auth/register",
            json={"email": "bob@tr-platform.io", "password": "another strong one"},
        )
        assert registered.status_code == 201
        assert (await b.get(f"/v1/replay/sessions/{sid}")).status_code == 404
        assert (
            await b.post(f"/v1/replay/sessions/{sid}/step", headers=_csrf(b))
        ).status_code == 404


async def test_the_assumptions_route_states_the_limitations(alice: AsyncClient) -> None:
    body = (await alice.get("/v1/replay/assumptions")).json()
    assert "identical" in body["relationship_to_backtest"]
    assert "Enforced" in body["risk"]
    assert "the BAR" in body["risk"]
    joined = " ".join(body["limitations"])
    assert "Tick replay" in joined
    assert "multi-timeframe" in joined


# ================================================================ SAFETY


async def test_nothing_in_the_replay_package_can_reach_a_broker() -> None:
    """Simulated execution is structural, not configured: there is no venue
    reference in the package to point at a broker."""
    import ast
    import importlib
    import inspect
    import pkgutil

    import app.replay as pkg

    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.replay."):
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


async def test_no_replay_module_calls_an_execution_verb() -> None:
    import ast
    import importlib
    import inspect
    import pkgutil

    import app.replay as pkg

    forbidden = {"place_order", "order_send", "close_position", "modify_order"}
    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.replay."):
        loaded = importlib.import_module(module.name)
        tree = ast.parse(inspect.getsource(loaded))
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not (called & forbidden), f"{module.name} calls {called & forbidden}"


async def test_a_replay_creates_no_order_and_no_position_row(
    app: FastAPI, alice: AsyncClient
) -> None:
    from app.models.execution import Order, Position
    from sqlalchemy import select as sa_select

    created = await _create(alice)
    sid = created["session_id"]
    for _ in range(5):
        await alice.post(f"/v1/replay/sessions/{sid}/step", headers=_csrf(alice))
    async with app.state.session_factory() as db:
        assert len((await db.scalars(sa_select(Order))).all()) == 0
        assert len((await db.scalars(sa_select(Position))).all()) == 0


def test_trading_safety_is_unchanged() -> None:
    from app.core.settings import LIVE_GATES
    from app.core.settings import Settings as S

    settings = S(_env_file=None)
    assert settings.trading_mode.value == "paper"
    assert settings.live_trading is False
    assert not any(LIVE_GATES.values())
