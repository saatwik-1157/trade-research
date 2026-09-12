#!/usr/bin/env python
"""One-command demonstration of the execution pipeline. **PAPER ONLY.**

    python demo.py            # all scenarios
    python demo.py --list     # names only
    python demo.py -k risk    # only scenarios whose name matches

This drives the REAL production modules -- `app.execution.ExecutionPipeline`,
`app.risk.RiskEngine`, `app.oms.OrderManagerRegistry`, `app.sizing` -- against
`app.brokers.fake.FakeBroker` in `paper` mode. Nothing is mocked except the
venue, and the venue is a simulator that this repository already ships and
tests. There is no MetaTrader5 import, no network, no database and no
credential anywhere in this file.

Why a script rather than a running server: the full stack needs Postgres and
Redis through Docker, and the Docker daemon is not always up. The pipeline
itself needs neither, so the flow can be demonstrated exactly as it runs in
production without standing up infrastructure that would add nothing to what
is being shown.

Every scenario prints the same trace, because the point is that the same gates
run every time and the only thing that varies is which one refuses:

    TradingView -> Signal -> Strategy -> AI -> RISK -> SIZING -> OMS -> Venue

Exit code is 0 only if every scenario reached its EXPECTED outcome, including
the ones expected to refuse. A demo that cannot fail demonstrates nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

from app.brokers.fake import FakeBroker  # noqa: E402
from app.execution import (  # noqa: E402
    ExecutionPipeline,
    IncomingSignal,
    Outcome,
    StrategyState,
)
from app.oms.registry import OrderManagerRegistry  # noqa: E402
from app.risk.engine import KillSwitches, RiskEngine, RiskLimits  # noqa: E402
from app.sizing.calculator import SizingMethod  # noqa: E402
from app.symbols.service import ContractSpec  # noqa: E402

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

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
    spec_source="demo",
    spec_updated_at=None,
)

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
    BOLD = DIM = OFF = ""


def signal(**over: object) -> IncomingSignal:
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
    base.update(over)
    return IncomingSignal(**base)  # type: ignore[arg-type]


def build(
    venue: FakeBroker | None,
    *,
    limits: RiskLimits | None = None,
    strategy: StrategyState | None = None,
    spec: ContractSpec | None = SPEC,
    switches: KillSwitches | None = None,
) -> ExecutionPipeline:
    registry = OrderManagerRegistry()
    if venue is not None:
        registry.register("acct-a", venue, mode="paper", broker="fake")

    async def spec_for(_symbol: str) -> ContractSpec | None:
        return spec

    def strategy_state(_sid: str | None) -> StrategyState:
        return strategy or StrategyState(exists=True, enabled=True)

    return ExecutionPipeline(
        managers=registry,
        risk=RiskEngine(limits or RiskLimits(require_stop_loss=False), switches=switches),
        spec_for=spec_for,
        strategy_state=strategy_state,
        sizing_method=SizingMethod.fixed_risk,
        risk_amount=Decimal("100"),
    )


async def paper_venue() -> FakeBroker:
    v = FakeBroker(mode="paper")
    await v.connect()
    v.set_quote("EURUSD", "1.10000", "1.10002")
    return v


# ------------------------------------------------------------------ trace


def trace(name: str, result: object, *, expected: Outcome, note: str = "") -> bool:
    """Print the full decision chain. §32 and §33 are the same renderer.

    Deliberately one function for both "why did it trade" and "why did it
    not": a separate renderer for refusals is how refusals end up with less
    detail than fills, and the refusals are the ones being debugged.
    """
    r = result  # type: ignore[assignment]
    traded = r.created_order  # type: ignore[attr-defined]
    # Two conditions, not one. The outcome name alone would pass if a refusal
    # were renamed while still sending an order, which is the failure that
    # matters. `expected is filled` is the only case allowed to have traded.
    ok = r.outcome is expected and traded == (expected is Outcome.filled)  # type: ignore[attr-defined]
    head = "WHY DID IT TRADE?" if traded else "WHY DID IT NOT TRADE?"

    print(f"\n{BOLD}{'=' * 74}{OFF}")
    print(f"{BOLD}{name}{OFF}")
    if note:
        print(f"{DIM}{note}{OFF}")
    print(f"{BOLD}{'=' * 74}{OFF}")
    print(f"  {head}")
    print(f"    TradingView   : accepted (source=tradingview, auth=strong)")
    print(f"    Strategy      : {'valid' if r.outcome is not Outcome.strategy_error else 'INVALID'}")  # type: ignore[attr-defined]

    ai = r.ai  # type: ignore[attr-defined]
    print(f"    AI advisory   : {ai.decision if ai else 'no opinion (advisory seat empty)'}")

    risk = r.risk  # type: ignore[attr-defined]
    if risk is None:
        print(f"    RISK          : not reached")
    else:
        codes = getattr(risk, "reasons", None) or getattr(risk, "codes", ())
        print(f"    RISK          : {getattr(risk, 'decision', risk)}"
              + (f"  reasons={list(codes)}" if codes else ""))

    sizing = r.sizing  # type: ignore[attr-defined]
    if sizing is None:
        print("    SIZING        : not reached")
    else:
        # `volume`, and the refusal reason beside it. A SizingResult is "a
        # volume, or a refusal. Never both, never a fallback" -- printing only
        # the volume would render a refusal as an empty number.
        print(f"    SIZING        : volume={sizing.volume} "
              f"risk={sizing.risk_actual} ({sizing.reason})")
        if sizing.gap:
            print(f"    sizing gap    : {sizing.gap}")

    order = r.order  # type: ignore[attr-defined]
    print(f"    OMS / venue   : {getattr(order, 'status', None) if order else 'no order created'}")
    print(f"    {BOLD}OUTCOME{OFF}       : {BOLD}{r.outcome}{OFF}")  # type: ignore[attr-defined]
    if r.detail:  # type: ignore[attr-defined]
        print(f"    detail        : {r.detail}")  # type: ignore[attr-defined]
    print(f"    execution_id  : {r.execution_id}")  # type: ignore[attr-defined]
    print(f"    order created : {traded}")
    print(f"  {'PASS' if ok else 'FAIL'}  expected {expected} with "
          f"created_order={expected is Outcome.filled}, "
          f"got {r.outcome} with created_order={traded}")  # type: ignore[attr-defined]
    return ok


# -------------------------------------------------------------- scenarios


async def demo_1_valid_trade() -> bool:
    v = await paper_venue()
    p = build(v)
    r = await p.process(signal(), now=T0)
    ok = trace("DEMO 1 - VALID TRADE", r, expected=Outcome.filled,
               note="Every gate passes. A paper fill at the simulator's quote.")
    await v.disconnect()
    return ok


async def demo_2_risk_rejected() -> bool:
    v = await paper_venue()
    # A position cap of zero. The RiskEngine is the only thing that can refuse
    # here, and the pipeline cannot proceed without its approval token.
    p = build(v, limits=RiskLimits(require_stop_loss=False, max_open_positions=0))
    r = await p.process(signal(), now=T0)
    ok = trace("DEMO 2 - RISK REJECTED", r, expected=Outcome.risk_vetoed,
               note="max_open_positions=0. The veto is a token the pipeline cannot forge.")
    await v.disconnect()
    return ok


async def demo_3_duplicate_signal() -> bool:
    v = await paper_venue()
    p = build(v)
    first = await p.process(signal(), now=T0)
    second = await p.process(signal(), now=T0 + timedelta(seconds=5))
    print(f"\n{DIM}  first pass outcome: {first.outcome}{OFF}")
    ok = trace("DEMO 3 - DUPLICATE SIGNAL", second,
               expected=Outcome.duplicate_signal,
               note="Same signal_key twice. One order, not two.")
    await v.disconnect()
    return ok


async def demo_4_stale_signal() -> bool:
    v = await paper_venue()
    p = build(v, limits=RiskLimits(require_stop_loss=False, max_signal_age_seconds=60))
    # Signal stamped an hour before it is processed.
    r = await p.process(signal(), now=T0 + timedelta(hours=1))
    ok = trace("DEMO 4 - STALE SIGNAL", r, expected=Outcome.signal_stale,
               note="max_signal_age_seconds=60, signal is 3600s old.")
    await v.disconnect()
    return ok


async def demo_5_venue_disconnected() -> bool:
    # No venue registered for the account at all: the OMS has nowhere to send.
    p = build(None)
    r = await p.process(signal(), now=T0)
    ok = trace("DEMO 5 - MT5 / VENUE UNAVAILABLE", r,
               expected=Outcome.no_venue,
               note="No order manager for the account. No venue = no order.")
    return ok


async def demo_6_kill_switch() -> bool:
    v = await paper_venue()
    p = build(v, switches=KillSwitches(global_stop=True,
                                       global_reason="operator kill switch"))
    r = await p.process(signal(), now=T0)
    ok = trace("DEMO 6 - KILL SWITCH", r, expected=Outcome.kill_switch,
               note="global_stop=True. Prevents NEW orders; closes nothing.")
    await v.disconnect()
    return ok


async def demo_7_strategy_disabled() -> bool:
    v = await paper_venue()
    p = build(v, strategy=StrategyState(exists=True, enabled=False))
    r = await p.process(signal(), now=T0)
    ok = trace("DEMO 7 - STRATEGY DISABLED / QUARANTINED", r,
               expected=Outcome.strategy_disabled,
               note="A disabled strategy cannot trade even on a valid signal.")
    await v.disconnect()
    return ok


async def demo_8_no_stop_loss() -> bool:
    v = await paper_venue()
    p = build(v, limits=RiskLimits(require_stop_loss=True))
    r = await p.process(signal(stop_loss=None), now=T0)
    ok = trace("DEMO 8 - NO STOP LOSS", r, expected=Outcome.sizing_refused,
               note="require_stop_loss=True. An unprotected position is refused.")
    await v.disconnect()
    return ok


async def demo_9_no_contract_spec() -> bool:
    v = await paper_venue()
    p = build(v, spec=None)
    r = await p.process(signal(), now=T0)
    ok = trace("DEMO 9 - SYMBOL NOT MAPPED", r, expected=Outcome.spec_incomplete,
               note="No ContractSpec. A symbol that cannot be sized is not guessed.")
    await v.disconnect()
    return ok


async def demo_10_research_no_action() -> bool:
    """§30. The research half, and its default answer."""
    from app.portfolio.allocation import (
        Allocation,
        PortfolioDecisionContext,
        Preservation,
        propose,
    )
    from app.portfolio.state import AccountState, Freshness, Source
    from app.research.opportunity import Evidence, Opportunity, OpportunityKind, assess

    print(f"\n{BOLD}{'=' * 74}{OFF}")
    print(f"{BOLD}DEMO 10 - RESEARCH: NO_ACTION{OFF}")
    print(f"{DIM}Research proposes; it never executes. This is the default path.{OFF}")
    print(f"{BOLD}{'=' * 74}{OFF}")

    o = Opportunity(
        symbol="NVDA",
        kind=OpportunityKind.fundamental_acceleration,
        discovered_at=T0,
        evidence=(Evidence("revenue", 1.0, "EDGAR 0000320193-26-000010", T0),),
        dedupe_key="NVDA:10-Q:2026Q2",
    )
    a = assess(o, seen={}, now=T0)
    print(f"  RESEARCH")
    print(f"    coverage      : {a.coverage}")
    print(f"    priority_score: {a.priority_score}   (None = below 50% coverage)")
    print(f"    edge          : {a.edge}")
    print(f"    blocking      : {list(a.blocking_factors)}")
    for g in a.data_gaps:
        print(f"    data gap      : {g}")

    ctx = PortfolioDecisionContext(
        as_of=T0,
        account=AccountState(
            account_id="paper-1", environment="paper",
            balance=Decimal("100000"), equity=Decimal("100000"),
            margin_free=Decimal("90000"), as_of=T0,
            source=Source.paper_engine, freshness=Freshness.fresh,
        ),
        available_capital=Decimal("50000"),
        opportunity_symbol="NVDA",
        opportunity_score=a.priority_score,
        opportunity_edge=str(a.edge),
        opportunity_blocking=a.blocking_factors,
        preservation=Preservation.normal,
    )
    p = propose(ctx, now=T0)
    print(f"  PORTFOLIO")
    print(f"    fit           : {p.fit}")
    print(f"    marginal      : computed={p.marginal.computed} ({p.marginal.reason_absent})")
    print(f"    waiting       : {p.waiting}")
    print(f"    allocation    : {BOLD}{p.allocation}{OFF}")
    print(f"    action        : {BOLD}{p.action}{OFF}")
    for r_ in p.reason:
        print(f"    reason        : {r_}")
    print(f"    valid_until   : {p.valid_until.isoformat()}")

    ok = p.allocation is Allocation.no_allocation and not p.permits_capital
    print(f"  {'PASS' if ok else 'FAIL'}  research reached NO_ACTION and authorised no capital")
    return ok


async def demo_11_safety_invariants() -> bool:
    """§55. The invariants, checked rather than asserted in prose."""
    print(f"\n{BOLD}{'=' * 74}{OFF}")
    print(f"{BOLD}DEMO 11 - SAFETY INVARIANTS{OFF}")
    print(f"{BOLD}{'=' * 74}{OFF}")
    ok = True

    from app.core.settings import Settings

    s = Settings(_env_file=None)
    checks: list[tuple[str, bool, object]] = [
        ("PAPER is the default", s.trading_mode.value == "paper", s.trading_mode.value),
        ("LIVE_TRADING is false by default", s.live_trading is False, s.live_trading),
        ("live execution is not allowed", not s.live_execution_allowed,
         len(s.live_execution_blockers())),
    ]
    for label, passed, got in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label:44s} got={got}")
        ok = ok and passed

    # No module in the research or allocation layer may import a venue.
    import ast as _ast
    import pathlib

    for rel in ("backend/app/research/opportunity.py",
                "backend/app/portfolio/allocation.py"):
        src = pathlib.Path(rel).read_text(encoding="utf-8")
        names: set[str] = set()
        for node in _ast.walk(_ast.parse(src)):
            if isinstance(node, _ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, _ast.ImportFrom) and node.module:
                names.add(node.module)
        bad = [n for n in names
               if n.startswith(("app.oms", "app.brokers", "app.execution", "MetaTrader5"))]
        print(f"  {'PASS' if not bad else 'FAIL'}  {rel.split('/')[-1]:44s} "
              f"imports no venue{'' if not bad else f' -- {bad}'}")
        ok = ok and not bad

    return ok


SCENARIOS = [
    ("demo_1_valid_trade", demo_1_valid_trade),
    ("demo_2_risk_rejected", demo_2_risk_rejected),
    ("demo_3_duplicate_signal", demo_3_duplicate_signal),
    ("demo_4_stale_signal", demo_4_stale_signal),
    ("demo_5_venue_disconnected", demo_5_venue_disconnected),
    ("demo_6_kill_switch", demo_6_kill_switch),
    ("demo_7_strategy_disabled", demo_7_strategy_disabled),
    ("demo_8_no_stop_loss", demo_8_no_stop_loss),
    ("demo_9_no_contract_spec", demo_9_no_contract_spec),
    ("demo_10_research_no_action", demo_10_research_no_action),
    ("demo_11_safety_invariants", demo_11_safety_invariants),
]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="print scenario names")
    ap.add_argument("-k", metavar="MATCH", help="run only matching scenarios")
    args = ap.parse_args()

    if args.list:
        for name, _ in SCENARIOS:
            print(name)
        return 0

    chosen = [(n, f) for n, f in SCENARIOS if not args.k or args.k in n]

    print(f"{BOLD}trade-research demonstration -- PAPER ONLY{OFF}")
    print(f"{DIM}real ExecutionPipeline / RiskEngine / OMS against a simulated venue.")
    print(f"no MetaTrader5 import, no network, no database, no credentials.{OFF}")

    results: list[tuple[str, bool]] = []
    for name, fn in chosen:
        try:
            results.append((name, await fn()))
        except Exception as exc:  # noqa: BLE001 - a demo reports its own failures
            print(f"\n  ERROR in {name}: {type(exc).__name__}: {exc}")
            results.append((name, False))

    passed = sum(1 for _, ok in results if ok)
    print(f"\n{BOLD}{'=' * 74}{OFF}")
    print(f"{BOLD}SUMMARY{OFF}   {passed}/{len(results)} scenarios reached their expected outcome")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"{BOLD}{'=' * 74}{OFF}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
