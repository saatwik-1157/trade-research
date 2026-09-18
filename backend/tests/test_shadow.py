"""Shadow execution (L44): the same decisions, and no venue.

The ones that carry the level:

  * `test_shadow_and_paper_make_the_same_decision` -- the whole claim, measured
    stage by stage rather than asserted.
  * `test_shadow_never_opens_a_position` -- the whole point.
  * `test_shadow_presents_as_paper_because_risk_refuses_an_unknown_mode` -- the
    design decision, with the reason it went that way.
  * `test_no_pipeline_stage_knows_about_shadow` -- structural. Shadow is an
    adapter, not a branch.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.brokers.base import BrokerAdapter, NotConnected, OrderRequest, OrderStatus, SymbolInfo
from app.brokers.fake import FakeBroker
from app.brokers.shadow import ShadowBroker, ShadowDecision
from app.execution import ExecutionPipeline, IncomingSignal, StrategyState, created_an_order
from app.oms.registry import OrderManagerRegistry
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

BACKEND = Path(__file__).resolve().parents[1]

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


@pytest.fixture
async def shadow() -> AsyncIterator[ShadowBroker]:
    broker = ShadowBroker()
    await broker.connect()
    # The shadow venue needs the spec for the same reason the paper one does:
    # OrderManager.submit validates against the adapter's contract terms, and
    # this test's whole claim is that both adapters agree up to execution. A
    # shadow refused for a missing spec while paper filled would be a
    # difference in the harness, not in the venue.
    broker.symbols["EURUSD"] = VENUE_SPEC
    yield broker
    await broker.disconnect()


def _pipeline(venue: BrokerAdapter) -> ExecutionPipeline:
    """The REAL pipeline. Nothing about it knows which adapter it holds."""
    registry = OrderManagerRegistry()
    registry.register("acct-a", venue, mode="paper", broker=venue.name)

    async def spec_for(_symbol: str) -> ContractSpec:
        return SPEC

    def strategy_state(_strategy_id: str | None) -> StrategyState:
        return StrategyState(exists=True, enabled=True)

    return ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(RiskLimits(require_stop_loss=False)),
        spec_for=spec_for,
        strategy_state=strategy_state,
        sizing_method=SizingMethod.fixed_risk,
        risk_amount=Decimal("100"),
    )


def _signal(key: str = "k1") -> IncomingSignal:
    now = datetime.now(UTC)
    return IncomingSignal(
        signal_id="s1",
        signal_key=key,
        source="tradingview",
        symbol="EURUSD",
        side="buy",
        signal_time=now,
        account_id="acct-a",
        mode="paper",
        strategy_id="sma_cross",
        entry_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09500"),
        take_profit=Decimal("1.11000"),
        auth_strength="strong",
    )


def _request() -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        side="buy",
        volume=Decimal("0.20"),
        stop_loss=Decimal("1.09500"),
        take_profit=Decimal("1.11000"),
        intent_id="tv:fp:abc",
        magic=770315,
    )


# ================================================= 1. the adapter in isolation


async def test_shadow_acknowledges_without_filling(shadow: ShadowBroker) -> None:
    """Accepted, so the OMS records an order -- and nothing else.

    No fill price, no filled volume, no position id. The acknowledgement exists
    to exercise the decision chain end to end; the absence of a fill is what
    makes it shadow.
    """
    result = await shadow.place_order(_request())
    assert result.status is OrderStatus.accepted
    assert result.fill_price is None
    assert result.filled_volume is None
    assert result.position_id is None
    assert result.fill_source == "shadow", "a shadow ack must be traceable to shadow"


async def test_shadow_never_opens_a_position(shadow: ShadowBroker) -> None:
    """The whole point. Ten orders, no positions and no venue orders."""
    for _ in range(10):
        await shadow.place_order(_request())
    assert await shadow.get_positions() == []
    assert await shadow.get_orders() == []
    assert await shadow.get_order_history(since=datetime.now(UTC)) == []
    assert len(shadow.decisions) == 10


async def test_shadow_refuses_to_close_what_was_never_opened(
    shadow: ShadowBroker,
) -> None:
    """A polite success here would let a position manager believe it flattened
    something. That is the one lie a shadow adapter must not tell."""
    with pytest.raises(NotConnected, match="nothing to close"):
        await shadow.close_position("anything")


async def test_shadow_records_the_whole_intent(shadow: ShadowBroker) -> None:
    """Enough to answer "what would it have sent?" and to join back."""
    await shadow.place_order(_request())
    decision = shadow.decisions[0].as_dict()
    assert decision["symbol"] == "EURUSD"
    assert decision["side"] == "buy"
    assert decision["volume"] == "0.20"
    assert decision["stop_loss"] == "1.09500"
    assert decision["take_profit"] == "1.11000"
    # The join back to the ExecutionResult carrying AI, risk and sizing.
    assert decision["intent_id"] == "tv:fp:abc"
    assert decision["executed"] is False


async def test_a_disconnected_shadow_adapter_refuses(shadow: ShadowBroker) -> None:
    """It behaves like an adapter, including when it is down."""
    await shadow.disconnect()
    with pytest.raises(NotConnected):
        await shadow.place_order(_request())


async def test_shadow_reports_no_profit_and_loss(shadow: ShadowBroker) -> None:
    """`balance == equity`, always. Nothing is open, so nothing floats.

    A shadow run answers "what would this platform have decided?" and never
    "what would it have earned?".
    """
    await shadow.place_order(_request())
    account = await shadow.get_account()
    assert account.balance == account.equity
    from app.brokers.shadow import SHADOW_BALANCE_UNAVAILABLE

    assert "no balance and no P&L" in SHADOW_BALANCE_UNAVAILABLE
    assert shadow.as_dict()["filled"] == 0
    assert shadow.as_dict()["positions"] == 0


# ==================================================== 2. through the pipeline


async def test_shadow_and_paper_make_the_same_decision(shadow: ShadowBroker) -> None:
    """Phase 4, measured stage by stage.

    Same pipeline, same risk limits, same sizing method, same signal -- one
    adapter that fills and one that does not. Everything up to the venue must
    agree; only execution may differ. A shadow run whose risk verdict differed
    from paper's would be validating a system nobody is going to run.
    """
    paper = FakeBroker(mode="paper")
    await paper.connect()
    paper.set_quote("EURUSD", "1.10000", "1.10002")
    paper.symbols["EURUSD"] = VENUE_SPEC
    now = datetime.now(UTC) + timedelta(seconds=5)

    shadow_result = await _pipeline(shadow).process(_signal(), now=now)
    paper_result = await _pipeline(paper).process(_signal(), now=now)

    # --- identical: the decision ---
    assert shadow_result.risk is not None and paper_result.risk is not None
    assert shadow_result.risk.decision == paper_result.risk.decision
    assert shadow_result.risk.approved == paper_result.risk.approved is True
    assert shadow_result.sizing is not None and paper_result.sizing is not None
    assert shadow_result.sizing.volume == paper_result.sizing.volume
    assert created_an_order(shadow_result.outcome) is True
    assert created_an_order(paper_result.outcome) is True
    assert len(shadow.decisions) == paper._orders_placed == 1

    # --- differing: the execution, and ONLY the execution ---
    assert await shadow.get_positions() == []
    assert len(await paper.get_positions()) == 1
    assert shadow_result.order is not None and paper_result.order is not None
    assert shadow_result.order.filled_quantity == 0
    assert paper_result.order.filled_quantity == Decimal("0.20")

    await paper.disconnect()


async def test_a_risk_veto_stops_a_shadow_run_too(shadow: ShadowBroker) -> None:
    """Shadow does not relax any gate. The RiskEngine is still mandatory.

    A shadow mode that skipped risk would be validating something other than
    the platform.
    """
    registry = OrderManagerRegistry()
    registry.register("acct-a", shadow, mode="paper", broker="shadow")

    async def spec_for(_symbol: str) -> ContractSpec:
        return SPEC

    pipeline = ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(RiskLimits(max_open_positions=0, require_stop_loss=False)),
        spec_for=spec_for,
        strategy_state=lambda _s: StrategyState(exists=True, enabled=True),
        sizing_method=SizingMethod.fixed_risk,
        risk_amount=Decimal("100"),
    )
    result = await pipeline.process(_signal(), now=datetime.now(UTC) + timedelta(seconds=5))
    assert not created_an_order(result.outcome)
    assert shadow.decisions == [], "a vetoed signal reached the shadow adapter"


# =========================================================== 3. the decisions


def test_shadow_presents_as_paper_because_risk_refuses_an_unknown_mode() -> None:
    """The design decision, and why it went that way.

    `mode = "shadow"` was the obvious choice and it did not work: `RiskEngine`
    checks `proposal.mode in ("paper", "demo")` and vetoes anything else, so
    every shadow signal died at the risk gate before reaching the adapter and a
    shadow run recorded nothing.

    The fix could have been to widen that allow-list. It was not: the list is a
    control that fails closed on an unknown mode, and widening a safety control
    so a new feature fits is backwards. **The feature adapts to the control.**

    What makes this adapter shadow is that it never fills -- not a label.
    """
    assert ShadowBroker().mode == "paper"
    risk_source = (BACKEND / "app" / "risk" / "engine.py").read_text(encoding="utf-8")
    assert 'proposal.mode in ("paper", "demo")' in risk_source, (
        "the RiskEngine mode allow-list moved; re-check why shadow presents as paper"
    )
    assert '"shadow"' not in risk_source, "the allow-list was widened for shadow"


def test_no_pipeline_stage_knows_about_shadow() -> None:
    """Structural. Shadow is an adapter, not a branch.

    The brief asks for REAL PIPELINE -> EXECUTION ADAPTER -> SHADOW EXECUTOR
    rather than a separate shadow implementation. The way to keep that true is
    for no stage above the adapter to be able to tell.
    """
    for name in ("execution/pipeline.py", "risk/engine.py", "sizing/calculator.py"):
        tree = ast.parse((BACKEND / "app" / name).read_text(encoding="utf-8"))
        # The AST, not the text. `pipeline.py` mentions "the shadow decision
        # record" in a comment explaining why the risk verdict is serialised,
        # and a substring search over the source called that a branch. What
        # matters is whether any stage can ACT on shadow-ness: a string literal
        # it could compare against, or an import of the adapter.
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert not [v for v in literals if v.strip().lower() == "shadow"], (
            f"{name} contains a 'shadow' literal it could branch on"
        )
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        assert not [m for m in imported if "shadow" in m], f"{name} imports the shadow adapter"


def test_the_shadow_adapter_cannot_reach_a_venue() -> None:
    """No MT5, no credential, no socket."""
    source = (BACKEND / "app" / "brokers" / "shadow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    assert not [m for m in imported if "MetaTrader5" in m or "requests" in m or "http" in m]
    for forbidden in ("password", "login=", "api_key", "secret"):
        assert forbidden not in source.lower().replace('login="shadow"', "")


def test_a_shadow_decision_carries_no_price_it_did_not_observe() -> None:
    """It records what was INTENDED. It never records a fill."""
    fields = set(ShadowDecision.__dataclass_fields__)
    for forbidden in ("fill_price", "filled_volume", "profit", "pnl", "entry_price"):
        assert forbidden not in fields
