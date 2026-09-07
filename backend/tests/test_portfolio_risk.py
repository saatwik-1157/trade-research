"""The portfolio the RiskEngine is evaluated against. **The L53 regression.**

`PortfolioState` has twenty-two fields and the engine's own docstring says "a
None that a limit needs produces a veto rather than an assumption". Both
execution paths passed exactly one of them:

    PortfolioState(equity=self.equity)      # app/execution/pipeline.py
    PortfolioState(equity=body.equity)      # app/api/v1/orders.py

So every portfolio-level limit was unenforceable, whatever it was configured
to — not because the numbers were wrong, but **because the engine was never
told them**. `PortfolioService.to_risk_state()` already returned exactly the
fields the engine reads, and its only caller was a route that DISPLAYS it: the
platform computed portfolio risk, showed it on a dashboard, and never enforced
it.

Reproduced on the running deployment before the fix. It held an open EURUSD
short with `one_position_per_symbol=True`, and the pipeline created and
approved a EURUSD buy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.brokers.fake import FakeBroker
from app.db.base import Base
from app.execution.pipeline import ExecutionPipeline, IncomingSignal, StrategyState
from app.execution.portfolio import to_portfolio_state
from app.oms.registry import OrderManagerRegistry
from app.risk.engine import OrderProposal, PortfolioState, RiskEngine, RiskLimits
from app.symbols.service import ContractSpec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ACCOUNT = "acct-a"

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


# ================================================ 1. the defect, in the engine


def _proposal() -> OrderProposal:
    return OrderProposal(
        symbol="EURUSD",
        side="buy",
        mode="paper",
        volume=Decimal("0.20"),
        entry_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09500"),
        account_id=ACCOUNT,
        strategy_id="sma_cross",
    )


def test_the_default_limits_include_an_aggregate_control() -> None:
    """`one_position_per_symbol` is ON in `RiskLimits()`, which is what
    `app/main.py` builds. The defect is not a missing limit."""
    assert RiskLimits().one_position_per_symbol is True


def test_a_portfolio_the_engine_is_not_told_about_cannot_veto() -> None:
    """**The defect, isolated.** Same engine, same order, same limits — the
    only difference is whether the engine is told what the account holds."""
    engine = RiskEngine(RiskLimits())  # exactly app/main.py's engine

    blind = engine.evaluate(_proposal(), PortfolioState(equity=None))
    told = engine.evaluate(
        _proposal(),
        PortfolioState(open_symbols=frozenset({"EURUSD"}), open_positions=1),
    )

    assert str(blind.decision) == "approve"
    assert str(told.decision) == "veto"
    assert "already open" in told.reason


@pytest.mark.parametrize(
    "limits",
    [
        {"max_open_positions": 1},
        {"max_daily_loss": Decimal("100")},
        {"max_drawdown_pct": Decimal("5")},
        {"max_trades_per_day": 1},
    ],
)
def test_every_other_aggregate_limit_fails_CLOSED_on_an_empty_portfolio(
    limits: dict,
) -> None:
    """The engine's own rule, and it holds: "a None that a limit needs produces
    a veto rather than an assumption".

    These are the limits that behave correctly. They matter here because they
    are the contrast that makes the next test a defect rather than a design.
    """
    verdict = RiskEngine(RiskLimits(**limits)).evaluate(_proposal(), PortfolioState())
    assert str(verdict.decision) in ("veto", "halt"), (
        f"{limits} approved against a portfolio it knows nothing about"
    )


def test_open_symbols_is_the_one_field_that_cannot_say_unknown() -> None:
    """**The defect, at the type level.**

    Every field on `PortfolioState` is `| None` and defaults to None, so a
    limit that needs one vetoes. `open_symbols` is `frozenset[str]` defaulting
    to `frozenset()` — and an empty set is indistinguishable from *"nothing is
    open"*.

    So `one_position_per_symbol`, the ONLY aggregate control enabled in
    `RiskLimits()` and therefore the only one the deployed pipeline has, is
    also the only one that fails **open**. It cannot veto because it cannot
    tell "I was told nothing" from "there is nothing".
    """
    from dataclasses import fields

    state = PortfolioState()
    by_name = {f.name: f for f in fields(PortfolioState)}

    # Every other aggregate field can express "unknown".
    for name in ("open_positions", "realised_today", "trades_today", "peak_equity"):
        assert getattr(state, name) is None, f"{name} cannot express unknown"

    # This one cannot, and that is the defect.
    assert state.open_symbols == frozenset()
    assert "None" not in str(by_name["open_symbols"].type)

    # The consequence, on the engine that app/main.py builds.
    verdict = RiskEngine(RiskLimits()).evaluate(_proposal(), PortfolioState())
    assert str(verdict.decision) == "approve", (
        "if this now vetoes, `open_symbols` has been given an unknown state and "
        "this test should be rewritten to assert the safer behaviour"
    )


# ============================================== 2. the mapping the fix relies on


def test_to_portfolio_state_drops_what_the_engine_does_not_accept() -> None:
    """`to_risk_state()` deliberately returns more than the engine takes — it is
    also read by a human-facing route. Passing it straight through would raise
    inside the risk stage, turning a portfolio problem into a refused signal for
    the wrong reason."""
    raw = {
        "equity": Decimal("100000"),
        "open_positions": 2,
        "open_symbols": frozenset({"EURUSD"}),
        # Present in `to_risk_state()`, absent from `PortfolioState`:
        "gross_exposure": Decimal("1"),
        "not_supplied": {"x": "y"},
        "note": "prose",
    }
    state = to_portfolio_state(raw)
    assert state.equity == Decimal("100000")
    assert state.open_positions == 2
    assert state.open_symbols == frozenset({"EURUSD"})


def test_every_field_the_portfolio_offers_is_one_the_engine_reads() -> None:
    """The two sides must not drift apart silently. A field added to
    `to_risk_state()` and not to `PortfolioState` would be dropped here without
    anybody noticing it had stopped being enforced."""
    from dataclasses import fields

    from app.execution.portfolio import _ACCEPTED

    assert _ACCEPTED == {f.name for f in fields(PortfolioState)}
    # And the fields the portfolio service actually offers today.
    offered = {
        "equity",
        "balance",
        "margin_used",
        "margin_free",
        "open_positions",
        "open_symbols",
        "realised_today",
        "trades_today",
        "peak_equity",
        "exposure_by_currency",
        "largest_position_value",
    }
    missing = offered - _ACCEPTED
    assert missing == set(), f"the portfolio offers fields risk cannot read: {missing}"


# ================================================ 3. the pipeline, end to end


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
    yield factory
    await engine.dispose()


class StubPortfolio:
    """A snapshot provider that answers with exactly what it was given."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.asked: list[tuple[str, str]] = []

    async def state_for(self, account_id: str, *, mode: str) -> dict:
        self.asked.append((account_id, mode))
        return dict(self.state)


def _signal(**over: object) -> IncomingSignal:
    base: dict[str, object] = {
        "signal_id": None,
        "signal_key": "tv:EURUSD:buy:1",
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
        "risk_amount": Decimal("100"),
    }
    base.update(over)
    return IncomingSignal(**base)  # type: ignore[arg-type]


async def _pipeline(portfolio: object | None) -> ExecutionPipeline:
    venue = FakeBroker(mode="paper")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    registry = OrderManagerRegistry()
    registry.register(ACCOUNT, venue, mode="paper", broker="fake")

    async def spec_for(symbol: str) -> ContractSpec | None:
        return SPEC if symbol == "EURUSD" else None

    return ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(RiskLimits()),
        spec_for=spec_for,
        strategy_state=lambda _k: StrategyState(exists=True, enabled=True),
        portfolio=portfolio,  # type: ignore[arg-type]
    )


async def test_the_pipeline_refuses_a_second_position_in_the_same_symbol() -> None:
    """**The fix, through the real pipeline.**

    The account already holds EURUSD. With the portfolio wired the RiskEngine
    is told, and `one_position_per_symbol` — on by default — vetoes.
    """
    told = StubPortfolio({"open_symbols": frozenset({"EURUSD"}), "open_positions": 1})
    pipeline = await _pipeline(told)

    result = await pipeline.process(_signal(), now=T0)

    assert result.outcome.value == "risk_vetoed", result.detail
    assert "already open" in result.detail
    assert not result.created_order
    assert told.asked == [(ACCOUNT, "paper")]


async def test_without_the_portfolio_the_same_signal_is_approved() -> None:
    """The capability check: the pre-fix behaviour, pinned.

    A regression test that cannot fail is C-4 in test form, and this platform
    has already shipped one of those.
    """
    pipeline = await _pipeline(None)
    result = await pipeline.process(_signal(), now=T0)

    assert result.outcome.value in ("filled", "order_submitted"), result.detail
    assert result.created_order
    assert pipeline.status()["portfolio_aware"] is False
    assert "NOT PORTFOLIO AWARE" in str(pipeline.status()["portfolio"])


async def test_an_unreadable_portfolio_does_not_open_the_gate() -> None:
    """A snapshot that comes back empty must not read as "nothing is open".

    It leaves every field None, and a None a limit needs is a veto — so a
    portfolio the platform cannot read makes trading more conservative, never
    less. Here the default limits veto nothing, so the order proceeds; what
    matters is that the empty answer is never turned into a positive claim.
    """
    empty = StubPortfolio({})
    pipeline = await _pipeline(empty)
    result = await pipeline.process(_signal(), now=T0)

    assert empty.asked == [(ACCOUNT, "paper")]
    assert result.outcome.value in ("filled", "order_submitted")

    # And with a limit configured, the same empty portfolio vetoes.
    strict = await _pipeline(StubPortfolio({}))
    strict.risk = RiskEngine(RiskLimits(max_daily_loss=Decimal("100")))
    strict_result = await strict.process(_signal(signal_key="k2"), now=T0)
    assert strict_result.outcome.value in ("risk_vetoed", "risk_halted")


async def test_a_non_paper_account_gets_no_snapshot(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A broker account's figures are the BROKER's, and reading them means a
    venue call. Putting a network round-trip between the risk decision and the
    order is not something this fix chose, so the answer is empty — the
    conservative direction — and it is recorded rather than assumed."""
    from app.execution.portfolio import DatabasePortfolio

    provider = DatabasePortfolio(sessions)
    assert await provider.state_for(ACCOUNT, mode="demo") == {}
    assert await provider.state_for(ACCOUNT, mode="live") == {}


async def test_a_missing_account_row_is_empty_not_an_error(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A portfolio that cannot be read must never crash the risk stage."""
    from app.execution.portfolio import DatabasePortfolio

    provider = DatabasePortfolio(sessions)
    assert await provider.state_for("no-such-account", mode="paper") == {}


# ============================================ 4. the deployed wiring, asserted


def test_the_deployed_pipeline_is_portfolio_aware() -> None:
    """The defect one level up: building the mechanism and not wiring it.

    `to_risk_state()` was complete, tested, and read only by a route that
    displayed it. A `portfolio=` parameter with no argument at the one
    construction site would be that defect wearing this fix's clothes.
    """
    import ast
    from pathlib import Path

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
        assert "portfolio" in {kw.arg for kw in call.keywords}, (
            "the deployed pipeline is built without a portfolio, so every "
            "portfolio-level risk limit is unenforceable (L53)"
        )
