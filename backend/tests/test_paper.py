"""Paper trading: the safety boundary, the money, and the pipeline.

Four tests carry this level, and each answers a question that cannot be
answered by inspection:

  * `test_paper_execution_never_reaches_a_broker` — the level's mandatory
    safety test. A full pipeline pass with every broker entry point armed to
    fail the test if it is called.
  * `test_paper_stays_paper_under_every_configuration` — the 2x2 of
    TRADING_MODE and LIVE_TRADING, plus live credentials present.
  * `test_the_oms_cannot_be_called_without_an_approval` — Risk is not
    bypassable, because the type system will not allow it.
  * `test_the_same_signal_produces_one_order` — idempotency, including across
    a simulated restart.

This file is also where `app.risk` and `app.sizing` acquire their first tests.
Both modules existed before L16 as complete implementations that nothing
imported and nothing exercised; L16 is their first consumer, so it brings the
coverage with it.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import math
import pkgutil
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.backtest.config import CostModel
from app.core.settings import Settings, TradingMode
from app.db.base import Base
from app.main import create_app
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.paper.engine import AiVerdict, Outcome, PaperEngine
from app.paper.execution import ExecutionRefused, PaperExecution, Reference
from app.paper.oms import (
    IllegalOrderTransition,
    OrderRefused,
    OrderStatus,
    PaperOMS,
)
from app.paper.portfolio import (
    AccountNotTradeable,
    AccountState,
    IllegalAccountTransition,
    PaperPortfolio,
    SpecIncomplete,
    check_transition,
    value_per_price_unit,
)
from app.paper.router import (
    PROVIDER_FOR_MODE,
    ExecutionMode,
    ExecutionProvider,
    ExecutionRouteRefused,
    assert_paper_execution,
    provider_for,
)
from app.risk.engine import (
    Approval,
    KillSwitches,
    LimitKind,
    OrderProposal,
    PortfolioState,
    RiskDecision,
    RiskEngine,
    RiskLimits,
)
from app.sizing.calculator import SizingMethod
from app.strategies.base import (
    Candles,
    DataRequirement,
    SignalTiming,
    SignalType,
    Strategy,
    StrategyMetadata,
    StrategySignal,
    StrategyTier,
)
from app.symbols.service import ContractSpec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
BOB = {"email": "bob@tr-platform.io", "password": "another good passphrase"}


# ================================================================= fixtures


def spec(**overrides: object) -> ContractSpec:
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


def bars(closes: list[float], *, start: datetime = T0, step: timedelta | None = None) -> list[Bar]:
    step = step or timedelta(hours=1)
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
                bar_time=start + step * i,
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


def wave(n: int = 200) -> list[float]:
    return [1.1000 + 0.02 * math.sin(i / 9.0) + i * 0.00002 for i in range(n)]


class AlwaysLong(Strategy):
    """Fires ENTRY_LONG once, then holds. Deterministic, so a P&L test can
    check against arithmetic rather than against the engine's own output."""

    def __init__(self, at_index: int = 61, signal: SignalType = SignalType.entry_long) -> None:
        self.at_index = at_index
        self.signal = signal

    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            key="always_long",
            name="Always long",
            description="test double",
            tier=StrategyTier.research_only,
            timing=SignalTiming.bar_close,
        )

    def required_data(self) -> DataRequirement:
        return DataRequirement(min_bars=20)

    @classmethod
    def validate_config(cls, config: dict) -> dict:
        return {}

    def generate_signal(self, candles: Candles, *, now: datetime) -> StrategySignal:
        kind = self.signal if len(candles.bars) == self.at_index else SignalType.hold
        last = candles.bars[-1]
        return StrategySignal(
            strategy_key="always_long",
            symbol=last.symbol,
            timeframe=last.timeframe,
            signal_type=kind,
            bar_time=last.bar_time,
            generated_at=last.bar_time,
            reference_price=last.close,
            metadata={"atr": 0.002},
        )


class Exploding(Strategy):
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            key="boom",
            name="Boom",
            description="raises",
            tier=StrategyTier.research_only,
            timing=SignalTiming.bar_close,
        )

    def required_data(self) -> DataRequirement:
        return DataRequirement(min_bars=20)

    @classmethod
    def validate_config(cls, config: dict) -> dict:
        return {}

    def generate_signal(self, candles: Candles, *, now: datetime) -> StrategySignal:
        raise RuntimeError("the strategy is broken")


def portfolio(account_id: str = "acct-a", balance: str = "100000") -> PaperPortfolio:
    p = PaperPortfolio(account_id=account_id, currency="USD", starting_balance=Decimal(balance))
    p.state = AccountState.active
    return p


def engine(**overrides: object) -> PaperEngine:
    base: dict[str, object] = {
        "portfolio": portfolio(),
        "strategy": AlwaysLong(),
        "spec": spec(),
        "risk": RiskEngine(RiskLimits(require_stop_loss=False, max_signal_age_seconds=None)),
        "costs": CostModel(spread_points=Decimal("0.0002")),
        "timeframe": Timeframe.H1,
        "sizing_method": SizingMethod.fixed_quantity,
        "quantity": Decimal("1"),
    }
    base.update(overrides)
    return PaperEngine(**base)  # type: ignore[arg-type]


def now_for(data: list[Bar]) -> datetime:
    """A wall clock just after the newest bar, so nothing reads as stale."""
    return data[-1].bar_time + timedelta(minutes=1)


# ====================================== THE MANDATORY LIVE-EXECUTION TEST


class BrokerTripwire:
    """Anything that touches a broker fails the test loudly.

    Not a mock that records calls for later inspection -- a call raises here
    and now, so a pipeline that reached a broker cannot produce a passing test
    with an unchecked assertion.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):  # noqa: ANN204
        def explode(*args: object, **kwargs: object) -> None:
            self.calls.append(name)
            raise AssertionError(
                f"PAPER EXECUTION REACHED A BROKER: {name}() was called. "
                "This is the failure the whole level exists to prevent."
            )

        return explode


def test_paper_execution_never_reaches_a_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE mandatory safety test.

    A complete pipeline pass -- market data, strategy, risk, sizing, OMS,
    execution, position -- with MetaTrader5, the broker adapters and the
    toolkit's live order path all armed to fail on any call.
    """
    tripwire = BrokerTripwire()
    monkeypatch.setitem(sys.modules, "MetaTrader5", tripwire)

    import app.brokers.base as broker_base
    import app.brokers.mt5 as broker_mt5

    for module, name in (
        (broker_mt5, "MT5Adapter"),
        (broker_base, "BrokerAdapter"),
    ):
        monkeypatch.setattr(module, name, tripwire, raising=False)

    # `tools/` reaches sys.path through the rules module's loader, which is
    # how the application does it -- not by a second path hack here.
    from app.strategies.rules import _toolkit

    _toolkit()
    import mt5_paper

    for name in ("place", "close_position", "connect", "assert_demo"):
        monkeypatch.setattr(mt5_paper, name, getattr(tripwire, name), raising=False)

    data = bars(wave(120))
    eng = engine()
    result = None
    for i in range(60, len(data)):
        result = eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        if result.outcome is Outcome.filled:
            break

    # Paper execution WAS called, and produced a real fill.
    assert result is not None and result.outcome is Outcome.filled, result
    assert result.order is not None and result.order.fill is not None
    assert result.order.fill.execution_provider == str(ExecutionProvider.paper_execution_only)
    assert result.order.fill.fill_source == "simulator"
    assert eng.portfolio.positions  # a position exists

    # And no broker entry point was touched.
    assert tripwire.calls == [], f"a broker was called: {tripwire.calls}"


def test_no_module_in_the_paper_package_can_reach_a_broker() -> None:
    """Parsed, not grepped.

    Searching the source text for `MetaTrader5` matches this file's own
    docstrings saying there is none. Walking the import nodes cannot.
    """
    import app.paper

    forbidden = {"app.brokers", "MetaTrader5", "mt5_paper", "mt5_account", "subprocess"}
    checked = 0
    for info in pkgutil.iter_modules(app.paper.__path__):
        module = importlib.import_module(f"app.paper.{info.name}")
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                assert name not in forbidden and f"app.{root}" not in forbidden, (
                    f"app.paper.{info.name} imports {name}"
                )
                assert not name.startswith("app.brokers"), f"app.paper.{info.name} imports {name}"
        checked += 1
    assert checked >= 5, f"only {checked} paper modules were parsed"


def test_no_eval_or_exec_anywhere_in_the_paper_package() -> None:
    import app.paper

    for info in pkgutil.iter_modules(app.paper.__path__):
        module = importlib.import_module(f"app.paper.{info.name}")
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("eval", "exec", "compile"), (
                    f"app.paper.{info.name} calls {node.func.id}"
                )


# ============================================ PAPER/LIVE CONFIGURATION MATRIX


@pytest.mark.parametrize(
    ("mode", "live_flag"),
    [("paper", False), ("paper", True), ("demo", False), ("demo", True)],
)
def test_paper_stays_paper_under_every_configuration(mode: str, live_flag: bool) -> None:
    """LIVE_TRADING=true does not promote paper into live.

    The flag is a gate on the live route, never a promotion of another one.
    The routing function has no configuration parameter at all, which is why
    this holds for values nobody has thought of yet.
    """
    settings = Settings(_env_file=None, trading_mode=mode, live_trading=live_flag)
    assert provider_for(ExecutionMode.paper) is ExecutionProvider.paper_execution_only
    assert settings.live_execution_allowed is False
    assert settings.trading_mode is not TradingMode.live


def test_live_mode_without_the_flag_is_refused() -> None:
    """The contradictory configuration fails closed, at construction."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, trading_mode="live", live_trading=False)


def test_live_credentials_do_not_change_the_paper_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in (
        ("MT5_LOGIN", "123456"),
        ("MT5_PASSWORD", "not-a-real-password"),
        ("MT5_SERVER", "SomeBroker-Live"),
    ):
        monkeypatch.setenv(name, value)
    assert provider_for(ExecutionMode.paper) is ExecutionProvider.paper_execution_only


def test_the_router_takes_no_configuration_parameter() -> None:
    """The structural guarantee, asserted rather than described.

    A function whose only parameter is the mode cannot be overridden by
    configuration, because there is no parameter through which configuration
    could arrive.
    """
    params = list(inspect.signature(provider_for).parameters)
    assert params == ["mode"], params


def test_every_execution_mode_has_an_explicit_route() -> None:
    """No default. A mode added later must fail loudly, because a default here
    would be a default execution path."""
    assert set(PROVIDER_FOR_MODE) == set(ExecutionMode)
    for mode in ExecutionMode:
        assert provider_for(mode) is PROVIDER_FOR_MODE[mode]


@pytest.mark.parametrize("mode", [ExecutionMode.live, ExecutionMode.replay, ExecutionMode.backtest])
def test_the_paper_provider_refuses_a_non_paper_mode(mode: ExecutionMode) -> None:
    with pytest.raises(ExecutionRouteRefused):
        assert_paper_execution(mode)

    execution = PaperExecution(CostModel(spread_points=Decimal("0.0002")))
    with pytest.raises(ExecutionRouteRefused):
        execution.fill(
            mode=mode,
            side="buy",
            quantity=Decimal("1"),
            reference=Reference.from_bar(bars([1.1])[0]),
        )


# ==================================================== RISK IS NOT BYPASSABLE


def approval_for(**overrides: object) -> Approval:
    base: dict[str, object] = {
        "symbol": "EURUSD",
        "side": "buy",
        "mode": "paper",
        "volume": Decimal("1"),
        "entry_price": Decimal("1.1000"),
        "stop_loss": Decimal("1.0980"),
        "account_id": "acct-a",
    }
    base.update(overrides)
    engine_ = RiskEngine(RiskLimits(max_signal_age_seconds=None))
    approval, verdict = engine_.approve(OrderProposal(**base))  # type: ignore[arg-type]
    assert approval is not None, verdict.reason
    return approval


def test_the_oms_cannot_be_called_without_an_approval() -> None:
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    with pytest.raises(TypeError):
        oms.submit(  # type: ignore[call-arg]
            order_id="o1", intent_id="i1", account_id="acct-a"
        )
    for impostor in (None, "approved", {"approved": True}, object()):
        with pytest.raises(OrderRefused):
            oms.submit(impostor, order_id="o1", intent_id="i1", account_id="acct-a")  # type: ignore[arg-type]


def test_risk_cannot_be_bypassed_by_forging_an_approval() -> None:
    """A hand-built Approval carrying a veto is still refused."""
    engine_ = RiskEngine(RiskLimits(max_open_positions=0))
    _, verdict = engine_.approve(
        OrderProposal(
            symbol="EURUSD",
            side="buy",
            mode="paper",
            volume=Decimal("1"),
            stop_loss=Decimal("1.09"),
            entry_price=Decimal("1.1"),
        ),
        PortfolioState(open_positions=5),
    )
    assert not verdict.approved
    forged = Approval(verdict=verdict, approved_volume=Decimal("1"), approved_at=verdict.at)
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    with pytest.raises(OrderRefused, match="verdict"):
        oms.submit(forged, order_id="o1", intent_id="i1", account_id="acct-a")


def test_an_approval_only_comes_from_the_risk_engine() -> None:
    """`RiskEngine.approve` is the only producer of an Approval in the app."""
    import app.paper.engine as paper_engine

    tree = ast.parse(inspect.getsource(paper_engine))
    constructed = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Approval"
    ]
    assert constructed == [], "the paper engine constructs an Approval itself"


# ================================================================ RISK TEST


@pytest.mark.parametrize(
    ("limits", "state", "expect"),
    [
        (
            RiskLimits(max_open_positions=1),
            PortfolioState(open_positions=1),
            LimitKind.max_open_positions,
        ),
        (
            RiskLimits(max_trades_per_day=2),
            PortfolioState(trades_today=2),
            LimitKind.max_trades_per_day,
        ),
        (
            RiskLimits(max_daily_loss=Decimal("100")),
            PortfolioState(realised_today=Decimal("-150")),
            LimitKind.max_daily_loss,
        ),
        (
            RiskLimits(max_drawdown_pct=Decimal("10")),
            PortfolioState(equity=Decimal("80"), peak_equity=Decimal("100")),
            LimitKind.max_drawdown,
        ),
        (
            RiskLimits(max_exposure_per_currency=Decimal("1000")),
            PortfolioState(exposure_by_currency={"USD": Decimal("5000")}),
            LimitKind.max_exposure,
        ),
    ],
)
def test_the_risk_engine_vetoes_each_limit(
    limits: RiskLimits, state: PortfolioState, expect: LimitKind
) -> None:
    """`app.risk` had no tests before this level. These are them."""
    eng = RiskEngine(limits)
    proposal = OrderProposal(
        symbol="EURUSD",
        side="buy",
        mode="paper",
        volume=Decimal("1"),
        entry_price=Decimal("1.1"),
        stop_loss=Decimal("1.09"),
        base_currency="USD",
        account_id="acct-a",
    )
    approval, verdict = eng.approve(proposal, state)
    assert approval is None
    assert expect in {c.limit for c in verdict.failed}, verdict.reason


def test_a_breached_daily_loss_halts_rather_than_vetoes() -> None:
    """The next order would breach it too, so the session stops."""
    eng = RiskEngine(RiskLimits(max_daily_loss=Decimal("100")))
    _, verdict = eng.approve(
        OrderProposal(
            symbol="EURUSD",
            side="buy",
            mode="paper",
            volume=Decimal("1"),
            entry_price=Decimal("1.1"),
            stop_loss=Decimal("1.09"),
        ),
        PortfolioState(realised_today=Decimal("-500")),
    )
    assert verdict.decision is RiskDecision.halt


def test_a_risk_veto_creates_no_paper_order() -> None:
    """The pipeline-level assertion: a veto means no order exists at all."""
    eng = engine(
        risk=RiskEngine(
            RiskLimits(max_open_positions=0, require_stop_loss=False, max_signal_age_seconds=None)
        )
    )
    data = bars(wave(120))
    outcomes = []
    for i in range(60, 75):
        outcomes.append(eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1)))
    vetoed = [o for o in outcomes if o.outcome in (Outcome.risk_vetoed, Outcome.risk_halted)]
    assert vetoed, [str(o.outcome) for o in outcomes]
    assert eng.oms.orders == {}
    assert eng.portfolio.positions == {}
    assert eng.portfolio.balance == eng.portfolio.starting_balance
    # And the refusal is recorded, approvals included.
    assert eng.risk_events and eng.risk_events[0]["decision"] in ("veto", "halt")


def test_a_kill_switch_stops_new_orders() -> None:
    eng = engine(
        risk=RiskEngine(
            RiskLimits(require_stop_loss=False, max_signal_age_seconds=None),
            KillSwitches(global_stop=True, global_reason="maintenance"),
        )
    )
    data = bars(wave(120))
    results = [
        eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        for i in range(60, 70)
    ]
    assert any(r.outcome is Outcome.kill_switch for r in results)
    assert eng.oms.orders == {}


def test_an_unenforced_limit_is_reported_rather_than_passed() -> None:
    """ "Not enforced" must never read as "passed"."""
    eng = RiskEngine(RiskLimits(max_daily_loss=Decimal("100")))
    _, verdict = eng.approve(
        OrderProposal(
            symbol="EURUSD",
            side="buy",
            mode="paper",
            volume=Decimal("1"),
            entry_price=Decimal("1.1"),
            stop_loss=Decimal("1.09"),
        ),
        PortfolioState(realised_today=None),
    )
    assert verdict.not_enforced or not verdict.approved


# ================================================================ THE AI SEAT


class AlwaysYes:
    def score(self, signal: StrategySignal) -> AiVerdict:
        return AiVerdict(accept=True, confidence=Decimal("1"), model="always-yes")


class AlwaysNo:
    def score(self, signal: StrategySignal) -> AiVerdict:
        return AiVerdict(accept=False, reason="declined", model="always-no")


def test_the_ai_filter_can_decline_a_signal() -> None:
    eng = engine(ai=AlwaysNo())
    data = bars(wave(120))
    results = [
        eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        for i in range(60, 70)
    ]
    assert any(r.outcome is Outcome.ai_rejected for r in results)
    assert eng.oms.orders == {}


def test_the_ai_cannot_overturn_a_risk_veto() -> None:
    """An AI that approves everything still cannot get a vetoed signal through.

    It runs before risk and cannot see it, so there is no order of operations
    in which its opinion could matter to the veto.
    """
    eng = engine(
        ai=AlwaysYes(),
        risk=RiskEngine(
            RiskLimits(max_open_positions=0, require_stop_loss=False, max_signal_age_seconds=None)
        ),
    )
    data = bars(wave(120))
    results = [
        eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        for i in range(60, 70)
    ]
    assert any(r.outcome in (Outcome.risk_vetoed, Outcome.risk_halted) for r in results)
    assert eng.oms.orders == {}


def test_the_ai_verdict_has_no_field_that_could_raise_a_limit() -> None:
    fields = set(AiVerdict.__dataclass_fields__)
    assert fields == {"accept", "confidence", "reason", "model"}, fields


# =========================================================== DUPLICATE ORDERS


def test_the_same_signal_produces_one_order() -> None:
    eng = engine()
    data = bars(wave(120))
    first = None
    for i in range(60, 120):
        result = eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        if result.outcome is Outcome.filled:
            first = result
            break
    assert first is not None and first.order is not None
    orders_after_first = len(eng.oms.orders)

    # The identical bar again, twice.
    index = len(data) - 1
    for _ in range(2):
        again = eng.process(data[: index + 1], now=data[index].bar_time + timedelta(minutes=1))
        assert again.outcome is not Outcome.filled or again.order is first.order
    assert len(eng.oms.orders) == orders_after_first


def test_a_repeat_submission_returns_the_existing_order() -> None:
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    approval = approval_for()
    a = oms.submit(approval, order_id="o1", intent_id="same", account_id="acct-a")
    b = oms.submit(approval, order_id="o2", intent_id="same", account_id="acct-a")
    assert a.created and b.duplicate
    assert a.order is b.order
    assert len(oms.orders) == 1


def test_a_restart_cannot_reorder_a_bar_it_already_ordered() -> None:
    """Restart recovery: the intent ids are reloaded, so the same bar is inert.

    This is the in-memory half. The database half is `orders.intent_id`'s
    unique index, which holds even if this set were ever wrong.
    """
    data = bars(wave(120))
    first = engine()
    filled = None
    for i in range(60, 120):
        result = first.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        if result.outcome is Outcome.filled:
            filled = result
            break
    assert filled is not None and filled.signal_key is not None

    # A fresh process: same account, same strategy, recovered intents.
    restarted = engine()
    restarted.seen_signals = {filled.signal_key}
    index = data.index(next(b for b in data if b.bar_time == filled.signal.bar_time))  # type: ignore[union-attr]
    again = restarted.process(data[: index + 1], now=data[index].bar_time + timedelta(minutes=1))
    assert again.outcome is Outcome.duplicate_signal
    assert restarted.oms.orders == {}


def test_the_signal_key_is_stable_across_instances() -> None:
    """A random id would make every restart look like new information."""
    data = bars(wave(70))
    a, b = engine(), engine()
    sa = a.strategy.generate_signal(Candles.of("EURUSD", Timeframe.H1, data), now=T0)
    sb = b.strategy.generate_signal(Candles.of("EURUSD", Timeframe.H1, data), now=T0)
    assert a.signal_key(sa) == b.signal_key(sb)


def test_two_accounts_produce_different_signal_keys() -> None:
    data = bars(wave(70))
    a = engine(portfolio=portfolio("acct-a"))
    b = engine(portfolio=portfolio("acct-b"))
    signal = a.strategy.generate_signal(Candles.of("EURUSD", Timeframe.H1, data), now=T0)
    assert a.signal_key(signal) != b.signal_key(signal)


# ================================================== MARKET DATA INTERRUPTION


def test_stale_market_data_blocks_new_orders() -> None:
    eng = engine()
    data = bars(wave(120))
    # A wall clock days after the newest bar: the feed has stopped.
    result = eng.process(data, now=data[-1].bar_time + timedelta(days=3))
    assert result.outcome is Outcome.market_data_stale
    assert eng.oms.orders == {}
    assert "refusing to open on data this old" in result.detail


def test_trading_resumes_when_the_feed_comes_back() -> None:
    eng = engine()
    data = bars(wave(120))
    stale = eng.process(data[:100], now=data[99].bar_time + timedelta(days=3))
    assert stale.outcome is Outcome.market_data_stale
    fresh = [
        eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        for i in range(60, 120)
    ]
    assert any(r.outcome is Outcome.filled for r in fresh)


def test_a_stale_feed_does_not_close_an_open_position() -> None:
    """Closing on stale data is also trading on it."""
    eng = engine()
    data = bars(wave(120))
    for i in range(60, 120):
        if eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1)).outcome is (
            Outcome.filled
        ):
            break
    assert eng.portfolio.positions
    eng.process(data, now=data[-1].bar_time + timedelta(days=3))
    assert eng.portfolio.positions, "a stale feed closed a position"


# ============================================================== THE STRATEGY


def test_a_broken_strategy_does_not_crash_the_engine() -> None:
    eng = engine(strategy=Exploding())
    data = bars(wave(120))
    result = eng.process(data, now=now_for(data))
    assert result.outcome is Outcome.strategy_error
    assert "RuntimeError" in result.detail
    # The account is untouched and the engine still works.
    assert eng.portfolio.balance == eng.portfolio.starting_balance


def test_a_short_history_warms_up_rather_than_claiming_no_signal() -> None:
    eng = engine()
    result = eng.process(bars(wave(10)), now=T0 + timedelta(hours=11))
    assert result.outcome is Outcome.warming_up


# ================================================================== EXECUTION


def test_a_buy_fills_at_the_ask_and_a_sell_at_the_bid() -> None:
    execution = PaperExecution(CostModel(spread_points=Decimal("0.0002")))
    from app.marketdata.types import Quote

    quote = Quote(
        symbol="EURUSD",
        provider=Provider.simulator,
        at=T0,
        received_at=T0,
        bid=Decimal("1.10000"),
        ask=Decimal("1.10020"),
        spread_availability=Availability.available,
    )
    reference = Reference.from_quote(quote)
    buy = execution.fill(
        mode=ExecutionMode.paper, side="buy", quantity=Decimal("1"), reference=reference
    )
    sell = execution.fill(
        mode=ExecutionMode.paper, side="sell", quantity=Decimal("1"), reference=reference
    )
    assert buy.price == Decimal("1.10020")
    assert sell.price == Decimal("1.10000")
    assert buy.bid_ask is Availability.available


def test_a_round_trip_pays_the_spread_exactly_once() -> None:
    """Filling at the right side of the book IS the spread charge. Subtracting
    it again as well would double it."""
    spread = Decimal("0.0002")
    execution = PaperExecution(CostModel(spread_points=spread))
    reference = Reference.from_bar(bars([1.1])[0])
    buy = execution.fill(
        mode=ExecutionMode.paper, side="buy", quantity=Decimal("1"), reference=reference
    )
    sell = execution.fill(
        mode=ExecutionMode.paper, side="sell", quantity=Decimal("1"), reference=reference
    )
    assert float(buy.price - sell.price) == pytest.approx(float(spread), abs=1e-9)


def test_a_modelled_book_is_never_reported_as_measured() -> None:
    execution = PaperExecution(CostModel(spread_points=Decimal("0.0002")))
    fill = execution.fill(
        mode=ExecutionMode.paper,
        side="buy",
        quantity=Decimal("1"),
        reference=Reference.from_bar(bars([1.1])[0]),
    )
    assert fill.bid_ask is Availability.not_available
    assert fill.reference.source == "bar_close"


def test_slippage_is_always_adverse() -> None:
    costs = CostModel(spread_points=Decimal("0.0002"), slippage_points=Decimal("0.0001"))
    execution = PaperExecution(costs)
    reference = Reference.from_bar(bars([1.1])[0])
    plain = PaperExecution(CostModel(spread_points=Decimal("0.0002")))
    for side, worse in (("buy", 1), ("sell", -1)):
        with_slip = execution.fill(
            mode=ExecutionMode.paper, side=side, quantity=Decimal("1"), reference=reference
        )
        without = plain.fill(
            mode=ExecutionMode.paper, side=side, quantity=Decimal("1"), reference=reference
        )
        assert (with_slip.price - without.price) * worse > 0, side


def test_a_reference_with_no_price_refuses_rather_than_guessing() -> None:
    execution = PaperExecution(CostModel(spread_points=Decimal("0.0002")))
    empty = Reference(
        at=T0,
        bid=None,
        ask=None,
        last=None,
        source="quote",
        bid_ask=Availability.not_available,
    )
    with pytest.raises(ExecutionRefused, match="nothing to fill against"):
        execution.fill(mode=ExecutionMode.paper, side="buy", quantity=Decimal("1"), reference=empty)


def test_an_inverted_book_is_refused() -> None:
    execution = PaperExecution(CostModel(spread_points=Decimal("0.0002")))
    bad = Reference(
        at=T0,
        bid=Decimal("1.2"),
        ask=Decimal("1.1"),
        last=Decimal("1.15"),
        source="quote",
        bid_ask=Availability.available,
    )
    with pytest.raises(ExecutionRefused, match="inverted book"):
        execution.fill(mode=ExecutionMode.paper, side="buy", quantity=Decimal("1"), reference=bad)


def test_execution_is_deterministic() -> None:
    execution = PaperExecution(CostModel(spread_points=Decimal("0.0002")))
    reference = Reference.from_bar(bars([1.1])[0])
    fills = [
        execution.fill(
            mode=ExecutionMode.paper, side="buy", quantity=Decimal("1"), reference=reference
        )
        for _ in range(5)
    ]
    assert len({f.price for f in fills}) == 1


# ======================================================= POSITION ACCOUNTING


def per_unit() -> Decimal:
    return value_per_price_unit(Decimal("1"), Decimal("0.00001"))


def apply(p: PaperPortfolio, side: str, qty: str, price: str, commission: str = "0") -> list:
    return p.apply_fill(
        symbol="EURUSD",
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        commission=Decimal(commission),
        at=T0,
        value_per_unit=per_unit(),
    )


def test_open_add_reduce_close_long() -> None:
    p = portfolio()
    apply(p, "buy", "1", "1.10000")
    assert p.positions["EURUSD"].quantity == Decimal("1")
    assert p.positions["EURUSD"].average_entry == Decimal("1.10000")

    apply(p, "buy", "1", "1.10200")
    position = p.positions["EURUSD"]
    assert position.quantity == Decimal("2")
    assert position.average_entry == Decimal("1.10100")

    closed = apply(p, "sell", "1", "1.10300")
    assert len(closed) == 1
    assert p.positions["EURUSD"].quantity == Decimal("1")
    # (1.10300 - 1.10100) * 1 * 100000 = 200
    assert closed[0].gross_pnl == pytest.approx(200.0, abs=1e-6)

    closed = apply(p, "sell", "1", "1.10100")
    assert "EURUSD" not in p.positions
    assert closed[0].gross_pnl == pytest.approx(0.0, abs=1e-6)


def test_open_add_close_short() -> None:
    p = portfolio()
    apply(p, "sell", "2", "1.10000")
    apply(p, "sell", "2", "1.10400")
    assert p.positions["EURUSD"].side == "short"
    assert p.positions["EURUSD"].average_entry == Decimal("1.10200")
    closed = apply(p, "buy", "4", "1.10000")
    assert "EURUSD" not in p.positions
    # short: (entry - exit) * qty * per_unit = 0.00200 * 4 * 100000
    assert closed[0].gross_pnl == pytest.approx(800.0, abs=1e-6)


def test_a_reversal_closes_and_opens_the_other_side() -> None:
    p = portfolio()
    apply(p, "buy", "1", "1.10000")
    closed = apply(p, "sell", "3", "1.10100")
    assert len(closed) == 1
    assert closed[0].side == "long"
    assert closed[0].quantity == Decimal("1")
    position = p.positions["EURUSD"]
    assert position.side == "short"
    assert position.quantity == Decimal("2")
    assert position.average_entry == Decimal("1.10100")


def test_a_position_quantity_can_never_go_negative() -> None:
    p = portfolio()
    apply(p, "buy", "1", "1.10000")
    apply(p, "sell", "5", "1.10000")
    assert p.positions["EURUSD"].quantity > 0
    assert p.positions["EURUSD"].side == "short"


def test_a_zero_or_negative_fill_quantity_is_refused() -> None:
    from app.paper.portfolio import PositionError

    p = portfolio()
    for bad in ("0", "-1"):
        with pytest.raises(PositionError):
            apply(p, "buy", bad, "1.10000")


# ================================================================== P&L TEST


def test_pnl_matches_an_independent_calculation() -> None:
    """Checked against arithmetic done here, not against the engine's own output."""
    p = portfolio(balance="100000")
    apply(p, "buy", "2", "1.10000", commission="7")
    closed = apply(p, "sell", "2", "1.10500", commission="7")

    # gross = (1.10500 - 1.10000) * 2 * (1 / 0.00001) = 0.005 * 2 * 100000 = 1000
    expected_gross = Decimal("1000")
    assert closed[0].gross_pnl == pytest.approx(float(expected_gross), abs=1e-6)
    # Both commissions have left the balance; the trade carries the exit one.
    assert p.commission_paid == Decimal("14")
    assert p.balance == pytest.approx(float(Decimal("100000") + expected_gross - 14), abs=1e-6)
    assert p.realized_pnl == pytest.approx(float(expected_gross - 14), abs=1e-6)


def test_equity_is_balance_plus_unrealized() -> None:
    p = portfolio(balance="100000")
    apply(p, "buy", "1", "1.10000")
    prices = {"EURUSD": Decimal("1.10300")}
    # unrealized = 0.003 * 1 * 100000 = 300
    assert p.unrealized(prices) == pytest.approx(300.0, abs=1e-6)
    assert p.equity(prices) == pytest.approx(100300.0, abs=1e-6)


def test_an_unpriced_position_is_reported_not_assumed_flat() -> None:
    p = portfolio()
    apply(p, "buy", "1", "1.10000")
    assert p.marks_missing({}) == ("EURUSD",)
    assert p.unrealized({}) == Decimal("0")
    snapshot = p.snapshot({})
    assert snapshot["marks_missing"] == ["EURUSD"]


def test_drawdown_is_measured_from_the_running_peak() -> None:
    p = portfolio(balance="100000")
    p.equity({})
    p.balance = Decimal("110000")
    p.equity({})  # peak now 110000
    p.balance = Decimal("104500")
    fall, pct = p.drawdown({})
    assert fall == pytest.approx(5500.0, abs=1e-6)
    assert pct == pytest.approx(5.0, abs=1e-4)


def test_the_day_figure_only_counts_trades_closed_that_day() -> None:
    p = portfolio()
    apply(p, "buy", "1", "1.10000")
    p.apply_fill(
        symbol="EURUSD",
        side="sell",
        quantity=Decimal("1"),
        price=Decimal("1.10100"),
        commission=Decimal("0"),
        at=T0,
        value_per_unit=per_unit(),
    )
    assert p.trades_since(T0) == 1
    assert p.trades_since(T0 + timedelta(days=1)) == 0


def test_value_per_price_unit_refuses_a_missing_spec_field() -> None:
    with pytest.raises(SpecIncomplete):
        value_per_price_unit(None, Decimal("0.00001"))
    with pytest.raises(SpecIncomplete):
        value_per_price_unit(Decimal("1"), Decimal("0"))


# ============================================================ THE BRACKET


def test_the_stop_is_checked_before_the_target() -> None:
    """A bar covering both books the LOSS -- L14's rule, and L15's."""
    eng = engine()
    p = eng.portfolio
    p.apply_fill(
        symbol="EURUSD",
        side="buy",
        quantity=Decimal("1"),
        price=Decimal("1.10000"),
        commission=Decimal("0"),
        at=T0,
        value_per_unit=per_unit(),
    )
    position = p.positions["EURUSD"]
    position.stop_loss = Decimal("1.09800")
    position.take_profit = Decimal("1.10200")
    straddle = Bar(
        symbol="EURUSD",
        provider=Provider.simulator,
        timeframe=Timeframe.H1,
        bar_time=T0 + timedelta(hours=1),
        open=Decimal("1.10000"),
        high=Decimal("1.10500"),
        low=Decimal("1.09500"),
        close=Decimal("1.10000"),
        volume=Decimal("1"),
        spread=None,
        spread_availability=Availability.not_available,
        complete=True,
    )
    result = eng.check_brackets(straddle, T0 + timedelta(hours=1))
    assert result is not None
    assert result.closed[0].exit_reason == "stop_loss"
    assert result.closed[0].net_pnl < 0


# ========================================================== ACCOUNT LIFECYCLE


def test_the_account_lifecycle_refuses_illegal_moves() -> None:
    check_transition(AccountState.created, AccountState.active)
    check_transition(AccountState.active, AccountState.paused)
    check_transition(AccountState.paused, AccountState.active)
    for current, wanted in (
        (AccountState.created, AccountState.paused),
        (AccountState.closed, AccountState.active),
        (AccountState.disabled, AccountState.active),
    ):
        with pytest.raises(IllegalAccountTransition):
            check_transition(current, wanted)


@pytest.mark.parametrize(
    "state", [AccountState.created, AccountState.paused, AccountState.disabled, AccountState.closed]
)
def test_only_an_active_account_accepts_an_order(state: AccountState) -> None:
    eng = engine()
    eng.portfolio.state = state
    data = bars(wave(120))
    results = [
        eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        for i in range(60, 70)
    ]
    assert any(r.outcome is Outcome.account_not_tradeable for r in results)
    assert eng.oms.orders == {}


def test_a_paused_account_can_still_close_a_position() -> None:
    """Flattening is not a new order, and an account that cannot flatten is a trap."""
    eng = engine(strategy=AlwaysLong(at_index=61))
    data = bars(wave(120))
    for i in range(60, 120):
        if eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1)).outcome is (
            Outcome.filled
        ):
            break
    assert eng.portfolio.positions
    eng.portfolio.state = AccountState.paused
    eng.strategy = AlwaysLong(at_index=len(data), signal=SignalType.exit_long)
    result = eng.process(data, now=now_for(data))
    assert result.outcome is Outcome.position_closed
    assert not eng.portfolio.positions


def test_check_tradeable_names_the_state() -> None:
    from app.paper.portfolio import check_tradeable

    with pytest.raises(AccountNotTradeable, match="paused"):
        check_tradeable(AccountState.paused)


# ============================================================== ORDER STATES


def test_the_order_states_are_the_tables_own() -> None:
    from app.models.execution import ORDER_STATUSES

    assert {s.value for s in OrderStatus} <= set(ORDER_STATUSES)


def test_an_order_cannot_skip_states() -> None:
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    submission = oms.submit(approval_for(), order_id="o1", intent_id="i1", account_id="acct-a")
    order = submission.order
    assert order.status is OrderStatus.submitted
    with pytest.raises(IllegalOrderTransition):
        order.move(OrderStatus.submitted)
    oms.execute(order, Reference.from_bar(bars([1.1])[0]), tick_size=Decimal("0.00001"))
    assert order.status is OrderStatus.filled
    with pytest.raises(IllegalOrderTransition):
        oms.cancel(order)


def test_an_unknown_order_is_parked_and_never_retried() -> None:
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    order = oms.submit(approval_for(), order_id="o1", intent_id="i1", account_id="acct-a").order
    oms.mark_unknown(order, "the outcome is genuinely uncertain")
    assert order.status is OrderStatus.unknown
    # It cannot be re-executed: execute() only accepts a submitted order.
    with pytest.raises(IllegalOrderTransition):
        oms.execute(order, Reference.from_bar(bars([1.1])[0]))


def test_an_unfillable_order_is_rejected_with_the_reason() -> None:
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    order = oms.submit(approval_for(), order_id="o1", intent_id="i1", account_id="acct-a").order
    empty = Reference(
        at=T0,
        bid=None,
        ask=None,
        last=None,
        source="quote",
        bid_ask=Availability.not_available,
    )
    oms.execute(order, empty)
    assert order.status is OrderStatus.rejected
    assert "nothing to fill against" in order.reason


def test_every_paper_order_carries_its_execution_mode() -> None:
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    order = oms.submit(approval_for(), order_id="o1", intent_id="i1", account_id="acct-a").order
    assert order.as_dict()["execution_mode"] == "paper"


# =============================================================== CONCURRENCY


def test_two_paper_accounts_do_not_share_state() -> None:
    a, b = engine(portfolio=portfolio("acct-a")), engine(portfolio=portfolio("acct-b", "50000"))
    data = bars(wave(120))
    for i in range(60, 120):
        if a.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1)).outcome is (
            Outcome.filled
        ):
            break
    assert a.portfolio.positions
    assert b.portfolio.positions == {}
    assert b.portfolio.balance == Decimal("50000")
    assert b.oms.orders == {}
    assert a.oms is not b.oms


def test_no_module_level_mutable_state_in_the_paper_package() -> None:
    """Isolation is structural, so it is checked structurally."""
    import app.paper

    for info in pkgutil.iter_modules(app.paper.__path__):
        module = importlib.import_module(f"app.paper.{info.name}")
        for name, value in vars(module).items():
            if name.startswith("_") or name.isupper():
                continue
            assert not isinstance(value, (list, dict, set)), (
                f"app.paper.{info.name}.{name} is module-level mutable state"
            )


async def test_two_engines_run_concurrently_without_crossing() -> None:
    a, b = engine(portfolio=portfolio("acct-a")), engine(portfolio=portfolio("acct-b"))
    data = bars(wave(120))

    async def drive(eng: PaperEngine) -> None:
        for i in range(60, 120):
            eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
            await asyncio.sleep(0)

    await asyncio.gather(drive(a), drive(b))
    for eng in (a, b):
        for order in eng.oms.orders.values():
            assert order.account_id == eng.portfolio.account_id


# ================================================================== SIZING


def test_sizing_is_the_existing_calculator_not_a_second_one() -> None:
    """`app.sizing` had no consumer before this level; the engine calls it."""
    import app.paper.engine as paper_engine

    source = inspect.getsource(paper_engine)
    assert "from app.sizing.calculator import" in source
    tree = ast.parse(source)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "calculate" not in defined, "the paper engine defines its own sizing"


def test_a_refused_size_creates_no_order() -> None:
    eng = engine(
        sizing_method=SizingMethod.fixed_risk,
        quantity=None,
        risk_amount=Decimal("100"),
    )
    data = bars(wave(120))
    # fixed_risk needs a stop distance; the strategy supplies an ATR so one
    # exists. Removing the tick value makes the spec incomplete instead.
    eng.spec = spec(tick_value=None)
    results = [
        eng.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
        for i in range(60, 70)
    ]
    assert any(r.outcome in (Outcome.sizing_refused, Outcome.spec_incomplete) for r in results), [
        str(r.outcome) for r in results
    ]
    assert eng.oms.orders == {}


# ============================================================== DOMAIN TIME


def test_the_engine_works_without_an_explicit_now() -> None:
    """The gap that let a real bug through to the live run.

    Every other test passes `now=` explicitly, so nothing exercised the
    default. `app.auth.models.utcnow` is naive by design -- it is the helper
    for a DateTime column -- and comparing it with an aware bar time raises
    `TypeError: can't subtract offset-naive and offset-aware datetimes`. The
    pipeline now takes its time from `app.paper.clock.now_utc`.
    """
    eng = engine()
    result = eng.process(bars(wave(120)))
    # Bars from 2026-01-01 really are stale against the wall clock; what
    # matters is that the comparison happened at all rather than raising.
    assert result.outcome is Outcome.market_data_stale


def test_domain_time_is_aware_and_storage_time_is_naive() -> None:
    from app.auth.models import utcnow
    from app.paper.clock import from_storage, now_utc, to_storage

    assert now_utc().tzinfo is not None
    assert utcnow().tzinfo is None
    assert to_storage(now_utc()).tzinfo is None
    assert from_storage(utcnow()).tzinfo is not None


def test_an_order_event_carries_an_aware_timestamp() -> None:
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    order = oms.submit(approval_for(), order_id="o1", intent_id="i1", account_id="acct-a").order
    assert order.events
    assert all(at.tzinfo is not None for at, _, _ in order.events)


# ============================================================ THE DAY BOUNDARY


def test_the_trading_day_is_not_the_machines_local_midnight() -> None:
    eng = engine()
    noon = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    assert eng.day_start(noon) == datetime(2026, 5, 5, tzinfo=UTC)


def test_the_day_boundary_can_be_configured() -> None:
    fixed = datetime(2026, 5, 5, 22, 0, tzinfo=UTC)
    eng = engine(day_start=fixed)
    assert eng.day_start(datetime(2026, 5, 6, 3, 0, tzinfo=UTC)) == fixed


# ==================================================================== THE API


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    from app.marketdata.providers.simulator import SimulatorMarketData
    from app.marketdata.service import MarketDataService
    from app.paper.service import PaperService
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
        await symbols.upsert_mapping(
            db,
            "EURUSD",
            "simulator",
            "EURUSD",
            contract_size=Decimal("100000"),
            tick_size=Decimal("0.00001"),
            tick_value=Decimal("1"),
            minimum_volume=Decimal("0.01"),
            maximum_volume=Decimal("100"),
            volume_step=Decimal("0.01"),
            price_precision=5,
            volume_precision=2,
        )
        await db.commit()
    application.state.market_data = MarketDataService({Provider.simulator: SimulatorMarketData()})
    application.state.paper = PaperService(
        application.state.session_factory,
        application.state.market_data,
        default_registry(),
        symbols,
        None,
    )
    yield application
    await application.state.paper.shutdown()
    await eng.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER

    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _promote(app: FastAPI, email: str, role: str) -> None:
    from app.auth.models import User
    from sqlalchemy import select

    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role
        await db.commit()


@pytest.fixture
async def alice(client: AsyncClient, app: FastAPI) -> AsyncClient:
    """A TRADER: `start_paper_bots` is trader-and-above."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], "trader")
    return client


async def _account(client: AsyncClient, **overrides: str) -> dict:
    body: dict[str, str] = {"name": "Test", "currency": "USD", "starting_balance": "100000"}
    body.update(overrides)
    r = await client.post("/v1/paper-trading/accounts", json=body, headers=_csrf(client))
    assert r.status_code == 201, r.text
    return r.json()


async def test_a_new_account_starts_created_and_untradeable(alice: AsyncClient) -> None:
    body = await _account(alice)
    assert body["status"] == "created"
    assert body["balance"] == "100000.0000"
    assert body["execution_mode"] == "PAPER"
    assert body["execution_provider"] == "PAPER_EXECUTION_ONLY"
    assert "No real money" in body["note"]


async def test_the_account_lifecycle_over_the_api(alice: AsyncClient) -> None:
    account = await _account(alice)
    aid = account["account_id"]
    for name, expect in (
        ("activate", "active"),
        ("pause", "paused"),
        ("resume", "active"),
        ("disable", "disabled"),
        ("close", "closed"),
    ):
        r = await alice.post(f"/v1/paper-trading/accounts/{aid}/{name}", headers=_csrf(alice))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == expect
    # Terminal.
    r = await alice.post(f"/v1/paper-trading/accounts/{aid}/activate", headers=_csrf(alice))
    assert r.status_code == 409


async def test_a_reset_must_be_confirmed(alice: AsyncClient) -> None:
    account = await _account(alice)
    aid = account["account_id"]
    r = await alice.post(
        f"/v1/paper-trading/accounts/{aid}/reset",
        json={"confirm": False},
        headers=_csrf(alice),
    )
    assert r.status_code == 422
    r = await alice.post(
        f"/v1/paper-trading/accounts/{aid}/reset",
        json={"confirm": True},
        headers=_csrf(alice),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reset_count"] == 1
    assert "archived in place" in body["reset"]["note"]


async def test_initial_capital_must_be_positive(alice: AsyncClient) -> None:
    r = await alice.post(
        "/v1/paper-trading/accounts",
        json={"name": "x", "starting_balance": "0"},
        headers=_csrf(alice),
    )
    assert r.status_code == 422


async def test_another_user_cannot_see_or_drive_an_account(
    client: AsyncClient, alice: AsyncClient, app: FastAPI
) -> None:
    account = await _account(alice)
    aid = account["account_id"]
    await alice.post("/auth/logout", headers=_csrf(alice))
    await client.post("/auth/register", json=BOB)
    # A TRADER as well, so a 404 here is ownership and not a missing permission.
    await _promote(app, BOB["email"], "trader")
    for verb, path in (
        ("GET", f"/v1/paper-trading/accounts/{aid}"),
        ("POST", f"/v1/paper-trading/accounts/{aid}/activate"),
        ("GET", f"/v1/paper-trading/accounts/{aid}/trades"),
        ("GET", f"/v1/paper-trading/accounts/{aid}/positions"),
    ):
        r = await client.request(verb, path, headers=_csrf(client))
        assert r.status_code == 404, f"{verb} {path} -> {r.status_code}"
    listing = await client.get("/v1/paper-trading/accounts")
    assert listing.json()["count"] == 0


async def test_a_bot_cannot_be_created_as_anything_but_paper(alice: AsyncClient) -> None:
    account = await _account(alice)
    r = await alice.post(
        "/v1/paper-trading/bots",
        json={
            "name": "b1",
            "paper_account_id": account["account_id"],
            "strategy_key": "sma_cross",
            "symbol": "EURUSD",
            "provider": "simulator",
            "costs": {"spread_points": "0.0002"},
            # A caller cannot set the mode; the router hardcodes paper.
            "mode": "live",
        },
        headers=_csrf(alice),
    )
    assert r.status_code == 201, r.text
    assert r.json()["mode"] == "paper"
    assert r.json()["execution_mode"] == "PAPER"


async def test_an_unknown_strategy_is_a_404(alice: AsyncClient) -> None:
    account = await _account(alice)
    r = await alice.post(
        "/v1/paper-trading/bots",
        json={
            "name": "b1",
            "paper_account_id": account["account_id"],
            "strategy_key": "does_not_exist",
            "symbol": "EURUSD",
            "costs": {"spread_points": "0.0002"},
        },
        headers=_csrf(alice),
    )
    assert r.status_code == 404


async def test_a_bot_will_not_start_on_an_inactive_account(alice: AsyncClient) -> None:
    account = await _account(alice)
    created = await alice.post(
        "/v1/paper-trading/bots",
        json={
            "name": "b1",
            "paper_account_id": account["account_id"],
            "strategy_key": "sma_cross",
            "symbol": "EURUSD",
            "provider": "simulator",
            "costs": {"spread_points": "0.0002"},
        },
        headers=_csrf(alice),
    )
    bot_id = created.json()["bot_id"]
    r = await alice.post(f"/v1/paper-trading/bots/{bot_id}/start", headers=_csrf(alice))
    assert r.status_code == 409
    assert "activate it" in r.text


async def test_a_kill_switch_stops_a_bot_from_being_started(
    alice: AsyncClient, app: FastAPI
) -> None:
    """The live run found this: the switch stopped running bots but not new ones.

    Every order would still have been vetoed by risk, which is safe -- but a
    kill switch that looks off when it is on is worse than one that refuses.
    """
    from app.risk.engine import KillSwitches

    account = await _account(alice)
    aid = account["account_id"]
    await alice.post(f"/v1/paper-trading/accounts/{aid}/activate", headers=_csrf(alice))
    created = await alice.post(
        "/v1/paper-trading/bots",
        json={
            "name": "b1",
            "paper_account_id": aid,
            "strategy_key": "sma_cross",
            "symbol": "EURUSD",
            "provider": "simulator",
            "costs": {"spread_points": "0.0002"},
        },
        headers=_csrf(alice),
    )
    bot_id = created.json()["bot_id"]

    service = app.state.paper
    for switches, expect in (
        (KillSwitches(global_stop=True, global_reason="maintenance"), "maintenance"),
        (KillSwitches(accounts=frozenset({aid})), "kill switch on account"),
    ):
        service.switches = switches
        r = await alice.post(f"/v1/paper-trading/bots/{bot_id}/start", headers=_csrf(alice))
        assert r.status_code == 409, r.text
        assert expect in r.text
    service.switches = KillSwitches()


async def test_an_emergency_stop_blocks_a_start(alice: AsyncClient, app: FastAPI) -> None:
    account = await _account(alice)
    aid = account["account_id"]
    await alice.post(f"/v1/paper-trading/accounts/{aid}/activate", headers=_csrf(alice))
    created = await alice.post(
        "/v1/paper-trading/bots",
        json={
            "name": "b1",
            "paper_account_id": aid,
            "strategy_key": "sma_cross",
            "symbol": "EURUSD",
            "provider": "simulator",
            "costs": {"spread_points": "0.0002"},
        },
        headers=_csrf(alice),
    )
    bot_id = created.json()["bot_id"]
    report = await app.state.paper.engage_emergency_stop("live drill")
    assert report["engaged"] is True
    assert "Nothing is closed automatically" in report["positions"]
    r = await alice.post(f"/v1/paper-trading/bots/{bot_id}/start", headers=_csrf(alice))
    assert r.status_code == 409
    assert "emergency stop" in r.text


async def test_the_execution_model_is_published(alice: AsyncClient) -> None:
    r = await alice.get("/v1/paper-trading/execution-model")
    assert r.status_code == 200
    body = r.json()
    assert body["execution_provider"] == "PAPER_EXECUTION_ONLY"
    assert body["routing"]["routes"]["paper"] == "PAPER_EXECUTION_ONLY"
    assert "LIVE_TRADING=true does NOT turn paper into live" in body["routing"]["live_trading_flag"]
    assert "Mandatory" in body["risk"]
    assert body["limitations"]


async def test_a_kill_switch_needs_the_risk_permission(client: AsyncClient, app: FastAPI) -> None:
    """A plain USER cannot engage one; a TRADER can."""
    await client.post("/auth/register", json=BOB)
    denied = await client.post(
        "/v1/paper-trading/kill-switch",
        json={"scope": "global", "reason": "test"},
        headers=_csrf(client),
    )
    assert denied.status_code == 403

    await _promote(app, BOB["email"], "trader")
    allowed = await client.post(
        "/v1/paper-trading/kill-switch",
        json={"scope": "global", "reason": "maintenance"},
        headers=_csrf(client),
    )
    assert allowed.status_code == 200
    assert allowed.json()["switches"]["global"] is True
    assert "never closes a position" in allowed.json()["positions"]


async def test_the_paper_pending_stub_is_gone(alice: AsyncClient) -> None:
    """L06 promised this path at level 16. It is served now, not 501."""
    r = await alice.get("/v1/paper-trading/accounts")
    assert r.status_code == 200
    assert r.status_code != 501
