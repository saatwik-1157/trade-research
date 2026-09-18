"""The automated execution engine (L20).

The orchestrator's job is to run an externally-arriving signal through gates
that already exist and are already tested. So what this file tests is not the
gates — it is that none of them can be skipped, and that every way a pass can
fail produces a recorded decision rather than an exception or a silent pass.

The cases that matter most:

  * `test_a_signal_cannot_reach_a_venue_without_passing_risk` — the veto is
    not a check the orchestrator performs, it is a token it cannot forge.
  * `test_an_unresolved_intent_leaves_the_signal_unconsumed` — the one case
    where a refusal must NOT consume the signal, because the venue may be
    holding an order for it.
  * `test_the_pipeline_never_raises` — a bot loop needs a decision it can
    record, not an exception it has to classify.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.brokers.base import SymbolInfo
from app.brokers.fake import FakeBroker
from app.execution import (
    NO_ORDER,
    ExecutionPipeline,
    IncomingSignal,
    Outcome,
    StrategyState,
    created_an_order,
    status_for,
)
from app.oms.registry import OrderManagerRegistry
from app.oms.state import OrderStatus
from app.paper.engine import AiVerdict
from app.risk.engine import RiskEngine, RiskLimits
from app.sizing.calculator import SizingMethod
from app.symbols.service import ContractSpec

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
        "signal_id": "sig-1",
        "signal_key": "tv:EURUSD:buy:1",
        "source": "tradingview",
        "symbol": "EURUSD",
        "side": "buy",
        "signal_time": T0,
        "account_id": "acct-a",
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
async def venue() -> AsyncIterator[FakeBroker]:
    fake = FakeBroker(mode="paper")
    await fake.connect()
    fake.set_quote("EURUSD", "1.10000", "1.10002")
    fake.symbols["EURUSD"] = VENUE_SPEC
    yield fake
    await fake.disconnect()


def build(
    venue: FakeBroker | None,
    *,
    limits: RiskLimits | None = None,
    strategy: StrategyState | None = None,
    spec: ContractSpec | None = SPEC,
    **kwargs: object,
) -> ExecutionPipeline:
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


# ==================================================== 1, 40. end to end


async def test_a_tradingview_signal_becomes_a_paper_order(venue: FakeBroker) -> None:
    """The brief's end-to-end case: BUY EURUSD, entry 1.1000, stop 1.0950,
    through every gate to a simulated fill."""
    pipeline = build(venue)
    result = await pipeline.process(signal(), now=T0)

    assert result.outcome is Outcome.filled
    assert result.created_order
    assert result.sizing is not None
    # 100 budget, a 0.005 stop costing 500 a lot -> 0.20 lots.
    assert result.sizing.volume == Decimal("0.20")
    assert result.order is not None
    assert result.order.status is OrderStatus.filled
    assert result.order.filled_quantity == Decimal("0.20")
    # Traceable end to end by one id.
    assert result.execution_id
    assert result.order.sizing_snapshot["execution_id"] == result.execution_id
    assert result.order.risk_decision_id


async def test_the_order_carries_every_link_in_the_audit_chain(
    venue: FakeBroker,
) -> None:
    """Brief 33: a user must be able to answer why this trade happened, why
    this quantity, and why it was allowed."""
    pipeline = build(venue)
    result = await pipeline.process(signal(), now=T0)
    order = result.order
    assert order is not None
    assert order.signal_id == "sig-1"  # which alert
    assert order.strategy_id == "sma_cross"  # which strategy
    assert order.risk_decision_id  # which risk decision
    assert order.sizing_snapshot["final_quantity"] == "0.20"  # which sizing
    assert order.client_order_id == "tv:EURUSD:buy:1"  # which intent


# ============================================= 2, 34, 35. signal validation


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"side": "sideways"}, Outcome.signal_invalid),
        ({"symbol": ""}, Outcome.signal_invalid),
        ({"signal_key": ""}, Outcome.signal_invalid),
        ({"entry_price": None}, Outcome.signal_invalid),
        ({"signal_time": datetime(2026, 8, 1, 12, 0)}, Outcome.signal_invalid),
        ({"signal_time": T0 + timedelta(hours=1)}, Outcome.signal_invalid),
        ({"signal_time": T0 - timedelta(hours=1)}, Outcome.signal_stale),
        ({"auth_strength": "weak"}, Outcome.source_unauthorized),
        ({"mode": "live"}, Outcome.source_unauthorized),
    ],
)
async def test_a_malformed_or_untrusted_signal_is_refused(
    venue: FakeBroker, override: dict[str, object], expected: Outcome
) -> None:
    pipeline = build(venue)
    result = await pipeline.process(signal(**override), now=T0)
    assert result.outcome is expected, result.detail
    assert not result.created_order
    assert len(await venue.get_positions()) == 0


async def test_a_stale_signal_is_measured_against_its_own_timestamp(
    venue: FakeBroker,
) -> None:
    """Not against when we happened to read it: "the alert was late" and "we
    were slow to look" are different faults."""
    pipeline = build(venue, max_signal_age_seconds=60)
    fresh = await pipeline.process(signal(signal_time=T0 - timedelta(seconds=30)), now=T0)
    assert fresh.outcome is Outcome.filled
    stale = await pipeline.process(
        signal(signal_key="k2", signal_time=T0 - timedelta(seconds=90)), now=T0
    )
    assert stale.outcome is Outcome.signal_stale
    assert "describes a market that has moved" in stale.detail


# ================================================= 3, 22. idempotency


async def test_a_duplicate_signal_creates_no_second_order(venue: FakeBroker) -> None:
    pipeline = build(venue)
    first = await pipeline.process(signal(), now=T0)
    second = await pipeline.process(signal(), now=T0)

    assert first.outcome is Outcome.filled
    assert second.outcome is Outcome.duplicate_signal
    assert len(await venue.get_positions()) == 1


async def test_the_oms_still_refuses_a_duplicate_the_pipeline_forgot(
    venue: FakeBroker,
) -> None:
    """The in-process `seen` set is the cheap guard in FRONT of the real one.
    Clearing it must not make a second order possible."""
    pipeline = build(venue)
    await pipeline.process(signal(), now=T0)
    pipeline.seen.clear()  # simulate a restart that lost the memory

    again = await pipeline.process(signal(), now=T0)
    assert again.outcome is Outcome.duplicate_signal
    assert len(await venue.get_positions()) == 1


# ================================================== 4, 36, 37. the strategy


async def test_an_unknown_strategy_is_refused(venue: FakeBroker) -> None:
    pipeline = build(venue, strategy=StrategyState(exists=False, enabled=False))
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.strategy_unknown
    assert "nobody can attribute" in result.detail


async def test_a_disabled_strategy_stops_the_pass(venue: FakeBroker) -> None:
    pipeline = build(
        venue,
        strategy=StrategyState(exists=True, enabled=False, reason="switched off by the operator"),
    )
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.strategy_disabled
    assert "switched off" in result.detail
    assert len(await venue.get_positions()) == 0


async def test_a_paused_bot_stops_the_pass(venue: FakeBroker) -> None:
    pipeline = build(venue, strategy=StrategyState(exists=True, enabled=True, paused=True))
    assert (await pipeline.process(signal(), now=T0)).outcome is Outcome.strategy_disabled


async def test_a_disabled_strategy_consumes_the_signal(venue: FakeBroker) -> None:
    """Otherwise the moment it is re-enabled, a queue of stale signals fires
    at once."""
    pipeline = build(venue, strategy=StrategyState(exists=True, enabled=False))
    await pipeline.process(signal(), now=T0)
    assert "tv:EURUSD:buy:1" in pipeline.seen


# ================================================== 5, 6. the AI layer


async def test_the_ai_seat_can_decline(venue: FakeBroker) -> None:
    class Declining:
        def score(self, _signal: object) -> AiVerdict:
            return AiVerdict(accept=False, reason="regime looks wrong", model="stub-1")

    pipeline = build(venue, ai=Declining())
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.ai_rejected
    assert result.ai is not None
    assert result.ai.model == "stub-1"
    assert len(await venue.get_positions()) == 0


async def test_an_approving_ai_cannot_get_a_vetoed_signal_through(
    venue: FakeBroker,
) -> None:
    """The AI runs BEFORE risk and cannot see it, so there is no order in
    which an AI opinion could overturn a veto."""

    class Enthusiastic:
        def score(self, _signal: object) -> AiVerdict:
            return AiVerdict(accept=True, confidence=Decimal("1"), reason="certain")

    pipeline = build(
        venue,
        ai=Enthusiastic(),
        limits=RiskLimits(require_stop_loss=False, max_position_size=Decimal("0.01")),
    )
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.risk_vetoed
    assert len(await venue.get_positions()) == 0


async def test_an_absent_ai_is_not_an_approval(venue: FakeBroker) -> None:
    """No opinion is not approval. Risk still decides."""
    pipeline = build(
        venue, limits=RiskLimits(require_stop_loss=False, max_position_size=Decimal("0.01"))
    )
    assert pipeline.ai is None
    assert (await pipeline.process(signal(), now=T0)).outcome is Outcome.risk_vetoed


# ============================================ 7, 8, 31, 32, 33. risk gates


async def test_a_signal_cannot_reach_a_venue_without_passing_risk(
    venue: FakeBroker,
) -> None:
    """Not a check the orchestrator performs — a token it cannot forge."""
    pipeline = build(venue, limits=RiskLimits(require_stop_loss=True))
    result = await pipeline.process(signal(stop_loss=None), now=T0)
    assert result.outcome in (Outcome.risk_vetoed, Outcome.sizing_refused)
    assert len(await venue.get_positions()) == 0


async def test_the_kill_switch_stops_execution(venue: FakeBroker) -> None:
    from app.risk.engine import KillSwitches

    switches = KillSwitches(global_stop=True, global_reason="operator pulled it")
    pipeline = build(venue)
    pipeline.risk = RiskEngine(RiskLimits(require_stop_loss=False), switches)

    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.kill_switch
    assert len(await venue.get_positions()) == 0


async def test_an_exposure_limit_cannot_be_bypassed(venue: FakeBroker) -> None:
    pipeline = build(
        venue, limits=RiskLimits(require_stop_loss=False, max_position_size=Decimal("0.05"))
    )
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.risk_vetoed
    assert "max_position_size" in result.detail


async def test_a_vetoed_signal_is_consumed(venue: FakeBroker) -> None:
    """Re-proposing the identical signal next pass would re-run the same veto
    forever."""
    pipeline = build(
        venue, limits=RiskLimits(require_stop_loss=False, max_position_size=Decimal("0.01"))
    )
    await pipeline.process(signal(), now=T0)
    assert "tv:EURUSD:buy:1" in pipeline.seen


# ============================================================ 9. sizing


async def test_sizing_refuses_rather_than_trading_an_unaffordable_size(
    venue: FakeBroker,
) -> None:
    pipeline = build(venue, risk_amount=Decimal("0.01"))
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.sizing_refused
    assert "below the venue minimum" in result.detail


async def test_a_missing_contract_spec_refuses(venue: FakeBroker) -> None:
    pipeline = build(venue, spec=None)
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.spec_incomplete
    assert "absent tick value" in result.detail


async def test_the_pipeline_computes_no_quantity_of_its_own() -> None:
    """One authoritative sizing calculation, and it is not here."""
    package = Path(__file__).resolve().parents[1] / "app" / "execution"
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for forbidden in ("calculate", "_size", "lot_for_risk", "_position_size"):
            assert forbidden not in defined, f"{path.name} defines {forbidden}"


# ============================================ 16, 17, 18. venue failures


async def test_a_broker_rejection_is_recorded_not_retried(venue: FakeBroker) -> None:
    venue.fail_next = "not enough margin"
    pipeline = build(venue)
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.execution_rejected
    assert result.order is not None
    assert result.order.status is OrderStatus.rejected


async def test_an_unknown_venue_answer_is_never_retried(venue: FakeBroker) -> None:
    venue.unknown_next = True
    pipeline = build(venue)
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.execution_unknown
    assert result.order is not None
    assert result.order.status is OrderStatus.unknown
    assert len(await venue.get_positions()) == 0


async def test_an_unresolved_intent_leaves_the_signal_unconsumed(
    venue: FakeBroker,
) -> None:
    """The one refusal that must NOT consume the signal: the venue may be
    holding an order for this intent, so the next pass has to meet the guard
    rather than being allowed through."""
    venue.unknown_next = True
    pipeline = build(venue)
    await pipeline.process(signal(), now=T0)
    pipeline.seen.discard("tv:EURUSD:buy:1")

    again = await pipeline.process(signal(), now=T0)
    assert again.outcome is Outcome.execution_unknown
    assert "Reconcile before sending" in again.detail
    assert len(await venue.get_positions()) == 0


async def test_a_disconnected_venue_stops_new_orders(venue: FakeBroker) -> None:
    """MT5 disconnect: stop new orders, preserve state, do not assume."""
    await venue.disconnect()
    pipeline = build(venue)
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.execution_rejected
    assert result.order is not None
    assert result.order.status is OrderStatus.failed


async def test_no_registered_venue_refuses_with_that_reason() -> None:
    pipeline = build(None)
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.no_venue
    assert "no order manager is registered" in result.detail


async def test_a_signal_for_the_wrong_execution_mode_is_refused(
    venue: FakeBroker,
) -> None:
    pipeline = build(venue)
    result = await pipeline.process(signal(mode="demo"), now=T0)
    assert result.outcome is Outcome.source_unauthorized
    assert "did not describe" in result.detail


# ================================================ 15, 42. the live fence


async def test_live_execution_is_refused_by_the_pipeline_itself(
    venue: FakeBroker,
) -> None:
    """A defence that exists once is a defence that can be removed once, so
    this sits in the pipeline as well as in the Risk Engine."""
    pipeline = build(venue)
    result = await pipeline.process(signal(mode="live"), now=T0)
    assert result.outcome is Outcome.source_unauthorized
    assert "live execution is not enabled" in result.detail


def test_live_trading_is_still_disabled_by_default() -> None:
    from app.core.settings import LIVE_GATES, Settings

    settings = Settings(_env_file=None)
    assert settings.live_trading is False
    assert settings.live_execution_allowed is False
    assert not any(LIVE_GATES.values())


# =============================================== architecture fences


def test_the_pipeline_holds_no_broker_adapter() -> None:
    """Execution Engine -> OMS -> BrokerAdapter -> MT5, never a shortcut."""
    package = Path(__file__).resolve().parents[1] / "app" / "execution"
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith("MetaTrader5"), f"{path.name} imports {name}"
                assert "mt5" not in name.lower(), f"{path.name} imports {name}"


async def test_the_pipeline_never_raises(venue: FakeBroker) -> None:
    """A bot loop needs a decision it can record, not an exception it has to
    classify."""

    def exploding(_strategy_id: str | None) -> StrategyState:
        raise RuntimeError("the strategy service is down")

    pipeline = build(venue)
    pipeline.strategy_state = exploding
    result = await pipeline.process(signal(), now=T0)
    assert result.outcome is Outcome.strategy_error
    assert "RuntimeError" in result.detail
    assert not result.created_order


# ================================================= the outcome vocabulary


def test_one_outcome_vocabulary_across_both_pipelines() -> None:
    """A paper bot reporting `risk_vetoed` and an orchestrator reporting
    something else cannot be added together."""
    from app.execution import outcome as shared
    from app.paper import engine as paper

    assert paper.Outcome is shared.Outcome
    assert paper.NO_ORDER is shared.NO_ORDER


def test_an_outcome_with_no_decision_counts_as_maybe_an_order() -> None:
    """`created_an_order` is the inverse of NO_ORDER rather than its own list,
    so a value added without a decision reads as "an order may exist" — the
    conservative direction."""
    for value in Outcome:
        assert created_an_order(value) is (value not in NO_ORDER)


def test_every_settled_outcome_maps_to_a_signal_status() -> None:
    """And the unsettled ones deliberately map to nothing."""
    unmapped = {o for o in Outcome if status_for(o) is None}
    assert unmapped == {
        Outcome.execution_unknown,
        Outcome.no_venue,
        Outcome.spec_incomplete,
        Outcome.strategy_error,
        # Strategy-driven outcomes: a bar that produced no signal never
        # becomes a `signals` row, so it has no status to be given.
        Outcome.warming_up,
        Outcome.no_signal,
        Outcome.hold,
        Outcome.market_data_stale,
        Outcome.account_not_tradeable,
        Outcome.position_closed,
        # L38. A latched condition somebody will resolve, so the signal is
        # PARKED rather than vetoed -- the same reasoning as `no_venue`.
        # Marking it finished would discard it for a reason that goes away.
        Outcome.safe_mode,
        # L45 C-1. The order could not be written down, so it was never sent.
        # A database that is briefly unreachable is a condition that clears,
        # and retiring the signal would throw away a real trading alert for a
        # fault on our side. PARKED for the same reason as `no_venue`.
        Outcome.not_recorded,
    }


def test_a_parked_signal_is_one_a_later_pass_can_still_act_on() -> None:
    """Marking an unknown execution finished would throw away a signal
    because a dependency was briefly down."""
    for outcome in (Outcome.execution_unknown, Outcome.no_venue, Outcome.spec_incomplete):
        assert status_for(outcome) is None


# ======================================================== 29. counters


async def test_the_counters_cover_the_whole_pipeline(venue: FakeBroker) -> None:
    pipeline = build(venue)
    await pipeline.process(signal(), now=T0)
    await pipeline.process(signal(signal_key="k2", side="sideways"), now=T0)

    status = pipeline.status()
    counts = status["counts"]
    assert isinstance(counts, dict)
    assert counts["signals_received"] == 2
    assert counts["orders_created"] == 1
    assert counts["signals_rejected"] == 1
    assert counts["filled"] == 1
    assert counts["signal_invalid"] == 1
    assert "app.risk, app.sizing and the OMS do" in str(status["authority"])


# ========================================= 19, 21, 26. the background worker


@pytest.fixture
async def db_session():  # noqa: ANN201
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401
    from app.db.base import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        from app.models.market import Symbol

        session.add(
            Symbol(
                id="sym-eur",
                code="EURUSD",
                asset_class="fx",
                digits=5,
                point_size=Decimal("0.00001"),
                unit_class="points",
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


def _row(**overrides: object):  # noqa: ANN202
    from app.models.signals import Signal

    base: dict[str, object] = {
        "id": "sig-1",
        "signal_key": "tv:EURUSD:buy:1",
        "source": "tradingview",
        "symbol_id": "sym-eur",
        "direction": "buy",
        "mode": "paper",
        "signal_time": datetime(2026, 8, 1, 12, 0),
        "received_at": datetime(2026, 8, 1, 12, 0),
        "auth_strength": "strong",
        "status": "new",
        "meta": {
            "account_id": "acct-a",
            "internal_symbol": "EURUSD",
            "strategy_id": "sma_cross",
            "price_reported": "1.10000",
            "stop_loss": "1.09500",
        },
    }
    base.update(overrides)
    return Signal(**base)


def _worker(factory, pipeline):  # noqa: ANN001, ANN202
    from app.execution.worker import ExecutionWorker
    from app.main import _to_incoming_signal

    return ExecutionWorker(factory, pipeline, to_signal=_to_incoming_signal)


async def test_a_recorded_signal_becomes_an_order_with_no_request_in_flight(
    db_session, venue: FakeBroker
) -> None:
    """Brief 19: the browser is a control surface, not the execution path.
    Nothing in this test issues an HTTP request."""
    async with db_session() as db:
        db.add(_row())
        await db.commit()

    pipeline = build(venue, max_signal_age_seconds=10**9)
    worker = _worker(db_session, pipeline)
    await worker.tick()

    assert len(await venue.get_positions()) == 1
    assert worker.results[0].outcome is Outcome.filled

    async with db_session() as db:
        from app.models.signals import Signal

        row = await db.get(Signal, "sig-1")
        assert row is not None
        assert row.status == "executed"
        # The correlation id, on the signal, pointing at the execution.
        assert row.meta["execution_id"] == worker.results[0].execution_id


async def test_the_worker_does_not_replay_a_settled_signal(db_session, venue: FakeBroker) -> None:
    """A restart resumes rather than re-runs."""
    async with db_session() as db:
        db.add(_row())
        await db.commit()

    pipeline = build(venue, max_signal_age_seconds=10**9)
    worker = _worker(db_session, pipeline)
    await worker.tick()
    # A fresh worker with no memory: the row's own status is what stops it.
    await _worker(db_session, build(venue, max_signal_age_seconds=10**9)).tick()

    assert len(await venue.get_positions()) == 1


async def test_a_signal_that_cannot_be_prepared_is_retired_not_retried(
    db_session, venue: FakeBroker
) -> None:
    """No account on the alert means no order manager to route it to. It is
    recorded as vetoed rather than picked up again every two seconds."""
    async with db_session() as db:
        db.add(_row(meta={"internal_symbol": "EURUSD", "price_reported": "1.1"}))
        await db.commit()

    worker = _worker(db_session, build(venue))
    await worker.tick()

    async with db_session() as db:
        from app.models.signals import Signal

        row = await db.get(Signal, "sig-1")
        assert row is not None
        assert row.status == "vetoed"
    assert len(await venue.get_positions()) == 0


async def test_an_unresolved_execution_leaves_the_signal_for_a_later_pass(
    db_session, venue: FakeBroker
) -> None:
    """Marking it finished would throw a signal away because a venue was
    briefly unclear."""
    async with db_session() as db:
        db.add(_row())
        await db.commit()

    venue.unknown_next = True
    worker = _worker(db_session, build(venue, max_signal_age_seconds=10**9))
    await worker.tick()

    assert worker.results[0].outcome is Outcome.execution_unknown
    async with db_session() as db:
        from app.models.signals import Signal

        row = await db.get(Signal, "sig-1")
        assert row is not None
        assert row.status == "new"  # parked, not finished


async def test_the_worker_is_a_supervised_loop_with_a_heartbeat(
    db_session, venue: FakeBroker
) -> None:
    """It is `app.workers.Worker`, not a second worker system: the same
    heartbeat the health check already reads."""
    from app.workers.base import Worker

    worker = _worker(db_session, build(venue))
    assert isinstance(worker, Worker)
    passes = await worker.run(max_passes=2)
    assert passes == 2
    assert worker.status.passes == 2
    assert worker.status.last_heartbeat is not None
    assert worker.status.running is False  # it stopped when told to


# ================================= the wiring between storage and the pipeline


async def test_the_strategy_state_helper_reads_the_real_registry() -> None:
    """The glue in `app/main.py` is where a typo would be invisible: it is
    only exercised when a signal actually arrives.

    **Async since L56**, because "may this strategy trade?" is now a question
    about `strategies.is_active` and that means reading the database. It is
    given a real one here rather than a stub, so the query itself is exercised.
    """
    from app.db.base import Base
    from app.main import _strategy_state, create_app
    from app.models.strategies import Strategy
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = create_app(engine=engine)
    state = _strategy_state(app)

    known = app.state.strategy_engine.registry.keys()
    assert known, "the platform registers no strategies at all"
    key = known[0]

    # No `strategies` row: nobody has switched it off, and the registry stays
    # the authority for whether it exists.
    first = await state(key)
    assert first.exists is True
    assert first.enabled is True

    # Switched off -- L56's quarantine, through the real query.
    async with app.state.session_factory() as db:
        db.add(Strategy(id="s-1", key=key, name=key))
        await db.commit()
        row = await db.get(Strategy, "s-1")
        assert row is not None
        row.is_active = False
        await db.commit()

    off = await state(key)
    assert off.exists is True
    assert off.enabled is False
    assert "switched off" in off.reason

    missing = await state("no_such_strategy")
    assert missing.exists is False
    assert "no strategy" in missing.reason

    # A signal naming no strategy is not attributable, so it does not execute.
    none_named = await state(None)
    assert none_named.exists is False
    assert "named no strategy" in none_named.reason

    await engine.dispose()


def test_a_signal_row_without_an_account_or_price_is_not_actionable() -> None:
    """`_to_incoming_signal` returns None rather than raising: a recorded
    signal that cannot become an order is retired with a reason, not retried
    forever."""
    from app.main import _to_incoming_signal

    assert _to_incoming_signal(_row(meta={"internal_symbol": "EURUSD"})) is None
    assert _to_incoming_signal(_row(meta={"account_id": "a"})) is None
    assert (
        _to_incoming_signal(_row(meta={"account_id": "a", "price_reported": "not a number"}))
        is None
    )
    ok = _to_incoming_signal(_row())
    assert ok is not None
    assert ok.entry_price == Decimal("1.10000")
    assert ok.signal_time.tzinfo is not None  # aware, as every gate expects


def test_the_advisory_from_an_alert_is_carried_and_not_obeyed() -> None:
    """A size or bracket suggested by TradingView is evidence about the alert,
    never an instruction to the platform."""
    from app.main import _to_incoming_signal

    row = _row(
        meta={
            "account_id": "acct-a",
            "internal_symbol": "EURUSD",
            "price_reported": "1.10000",
            "advisory_ignored": {"qty": "50", "sl": "1.0"},
        }
    )
    incoming = _to_incoming_signal(row)
    assert incoming is not None
    assert incoming.advisory == {"qty": "50", "sl": "1.0"}
    # There is no field on the pipeline's input by which a quantity could
    # reach the OMS, which is what makes "never obeyed" structural.
    assert not hasattr(incoming, "quantity")
