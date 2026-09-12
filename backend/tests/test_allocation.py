"""L86 allocation validation: the refusals, not the recommendations.

Every test here asserts that the engine declines, blocks, or reports absence.
That is the point of it. An allocation engine whose interesting behaviour is
saying yes has the wrong shape.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.portfolio.allocation import (
    HARD_CONSTRAINTS,
    Action,
    Allocation,
    Fit,
    PortfolioDecisionContext,
    PositionSnapshot,
    Preservation,
    SellToFund,
    WaitingValue,
    marginal_contribution,
    propose,
)
from app.portfolio.state import AccountState, Freshness, Source

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def account(**kw: object) -> AccountState:
    base: dict[str, object] = {
        "account_id": "paper-1",
        "environment": "paper",
        "balance": Decimal("100000"),
        "equity": Decimal("100000"),
        "margin_free": Decimal("90000"),
        "as_of": NOW,
        "source": Source.paper_engine,
        "freshness": Freshness.fresh,
    }
    base.update(kw)
    return AccountState(**base)  # type: ignore[arg-type]


def ctx(**kw: object) -> PortfolioDecisionContext:
    base: dict[str, object] = {
        "as_of": NOW,
        "account": account(),
        "positions": (),
        "available_capital": Decimal("50000"),
        "opportunity_symbol": "NVDA",
        "opportunity_score": 0.8,
        "opportunity_edge": "potential_edge",
    }
    base.update(kw)
    return PortfolioDecisionContext(**base)  # type: ignore[arg-type]


def holdings(n: int, sector: str = "tech") -> tuple[PositionSnapshot, ...]:
    return tuple(
        PositionSnapshot(
            symbol=f"S{i}",
            market_value=Decimal("10000"),
            sector=sector,
            thesis_strength=0.8,
        )
        for i in range(n)
    )


# ---------------------------------------------------------------- safety


def test_the_module_cannot_reach_a_venue():
    """§34.1-3. Asserted from the import graph, not from a grep."""
    source = pathlib.Path("app/portfolio/allocation.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in imported:
        for bad in ("app.oms", "app.brokers", "app.execution", "MetaTrader5"):
            assert not name.startswith(bad), f"allocation imported {name}"


def test_a_proposal_never_claims_authority():
    p = propose(ctx(positions=holdings(4, "energy")), now=NOW)
    # `permits_capital` is the strongest thing it can say, and even that is
    # only "capital is proposed", never "capital is authorised".
    assert "RiskEngine remains the final veto" in p.uncertainty


# ------------------------------------------------ zero is always available


def test_zero_allocation_needs_no_justification():
    """§6, §34.10. The default outcome with nothing clearing the bar."""
    p = propose(ctx(opportunity_score=0.1, positions=holdings(4, "energy")), now=NOW)
    assert p.allocation is Allocation.no_allocation
    assert not p.permits_capital


# ---------------------------------------------------- hard constraints


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({"available_capital": Decimal("0")}, "NO_AVAILABLE_CAPITAL"),
        ({"platform_allows_new_risk": False}, "PLATFORM_FORBIDS_NEW_RISK"),
        (
            {"preservation": Preservation.capital_preservation},
            "CAPITAL_PRESERVATION_ACTIVE",
        ),
        ({"preservation": Preservation.emergency}, "CAPITAL_PRESERVATION_ACTIVE"),
        ({"risk_budget_remaining": 0.0}, "DRAWDOWN_LIMIT"),
        ({"account": account(freshness=Freshness.stale)}, "ACCOUNT_STATE_UNKNOWN"),
    ],
)
def test_each_hard_constraint_blocks_absolutely(kw, expected):
    """§7, §34.15. No score outvotes one of these."""
    p = propose(ctx(opportunity_score=1.0, positions=holdings(5, "energy"), **kw), now=NOW)
    assert expected in p.hard_constraint_failures
    assert p.allocation is Allocation.blocked
    assert p.action is Action.blocked
    assert not p.permits_capital


def test_every_declared_hard_constraint_is_a_known_code():
    p = propose(ctx(available_capital=Decimal("0")), now=NOW)
    for code in p.hard_constraint_failures:
        assert code in HARD_CONSTRAINTS


def test_a_perfect_opportunity_cannot_override_capital_preservation():
    p = propose(
        ctx(
            opportunity_score=1.0,
            opportunity_edge="strong_edge",
            preservation=Preservation.emergency,
            positions=holdings(6, "energy"),
        ),
        now=NOW,
    )
    assert p.allocation is Allocation.blocked


def test_defensive_preservation_tightens_rather_than_forbids():
    """§16. Tightening, not a veto -- the strongest outcome is a review."""
    p = propose(
        ctx(
            opportunity_score=1.0,
            preservation=Preservation.defensive,
            positions=holdings(5, "energy"),
        ),
        now=NOW,
    )
    assert p.allocation is Allocation.review_required
    assert not p.permits_capital


# ---------------------------------------------------- refusal to compute


def test_marginal_contribution_refuses_on_a_degenerate_portfolio():
    """One holding has no margin to contribute to."""
    m = marginal_contribution(ctx(positions=holdings(1)))
    assert not m.computed
    assert "at least 2 independent holdings" in m.reason_absent


def test_marginal_contribution_refuses_when_positions_carry_no_value():
    c = ctx(
        positions=(
            PositionSnapshot(symbol="A", sector="tech"),
            PositionSnapshot(symbol="B", sector="energy"),
        )
    )
    m = marginal_contribution(c)
    assert not m.computed
    assert "no market value" in m.reason_absent


def test_fit_is_indeterminate_below_two_holdings():
    p = propose(ctx(positions=holdings(1)), now=NOW)
    assert p.fit is Fit.indeterminate
    assert any("too few" in g for g in p.data_gaps)


def test_risk_and_correlation_deltas_stay_absent_even_when_computed():
    """Concentration is computable from values; the others are not."""
    m = marginal_contribution(ctx(positions=holdings(4, "energy")))
    assert m.computed
    assert m.d_concentration is not None
    assert m.d_risk is None
    assert m.d_correlation is None
    assert m.d_drawdown is None
    assert "return series" in m.reason_absent


def test_absent_inputs_leave_confidence_none():
    p = propose(ctx(positions=holdings(1)), now=NOW)
    assert p.confidence is None


# ---------------------------------------------------------------- fit


def test_a_crowded_sector_conflicts():
    p = propose(ctx(positions=holdings(4, "tech"), opportunity_sector="tech"), now=NOW)
    assert p.fit is Fit.conflicting
    assert p.allocation is Allocation.no_allocation


# ---------------------------------------------------------------- selling


def test_nothing_is_ever_sold_automatically():
    """§9, §34. The strongest output is a review."""
    weak = (
        PositionSnapshot(symbol="A", market_value=Decimal("1"), thesis_strength=0.05),
        PositionSnapshot(symbol="B", market_value=Decimal("1"), thesis_strength=0.05),
    )
    c = ctx(positions=weak, available_capital=Decimal("0"))
    p = propose(c, now=NOW)
    assert p.sell_to_fund in (SellToFund.reduce_review, SellToFund.no_sell_required)
    assert p.action is not Action.exit_review or "review" in str(p.action).lower()
    # And capital being short is itself a hard block, so nothing proceeds.
    assert p.allocation is Allocation.blocked


# ---------------------------------------------------------------- waiting


def test_no_edge_means_waiting_not_acting():
    """The repository's standing finding, encoded as a portfolio decision."""
    p = propose(
        ctx(opportunity_edge="no_edge", opportunity_score=0.9, positions=holdings(4, "energy")),
        now=NOW,
    )
    assert p.waiting is WaitingValue.wait_for_information
    assert p.allocation is Allocation.watch
    assert not p.permits_capital


def test_a_blocked_opportunity_routes_to_research_not_capital():
    p = propose(
        ctx(opportunity_blocking=("INSUFFICIENT_DATA",), positions=holdings(4, "energy")),
        now=NOW,
    )
    assert p.action is Action.research
    assert p.allocation is Allocation.no_allocation


# ---------------------------------------------------------------- freshness


def test_a_proposal_carries_an_expiry():
    """§28."""
    p = propose(ctx(positions=holdings(4, "energy")), now=NOW, valid_for=timedelta(hours=6))
    assert p.valid_until == NOW + timedelta(hours=6)
    assert p.created_at == NOW


def test_an_old_proposal_reports_itself_expired():
    p = propose(
        ctx(positions=holdings(4, "energy")),
        now=datetime.now(UTC) - timedelta(days=2),
        valid_for=timedelta(hours=1),
    )
    assert p.expired


# ---------------------------------------------------------------- the one yes


def test_a_clean_opportunity_reaches_only_a_small_allocation_review():
    """The best available outcome. Note it is `add_review`, not `add`."""
    p = propose(
        ctx(
            opportunity_score=0.8,
            opportunity_edge="potential_edge",
            opportunity_sector="energy",
            positions=holdings(4, "tech"),
        ),
        now=NOW,
    )
    assert p.allocation is Allocation.small_allocation
    assert p.action is Action.add_review
    assert not p.hard_constraint_failures
