"""L83 opportunity research: the properties that keep it honest.

The tests that matter here are not "does it score" -- they are the ones that
assert it REFUSES to score, refuses to grant edge, and cannot reach a venue.
An opportunity engine that always produces a number is the failure mode; this
file exists to make that failure visible.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from app.research.opportunity import (
    Direction,
    EdgeStrength,
    Evidence,
    FalseOpportunityRisk,
    Freshness,
    Novelty,
    Opportunity,
    OpportunityKind,
    OpportunityState,
    ResearchValue,
    Verdict,
    assess,
    classify_divergence,
    freshness_of,
    novelty_of,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def ev(key: str, value: float | None = 1.0, *, age_days: float = 0.0) -> Evidence:
    return Evidence(
        key=key,
        value=value,
        source="EDGAR accession 0000320193-26-000010",
        observed_at=NOW - timedelta(days=age_days),
    )


def opp(**kw: object) -> Opportunity:
    base: dict[str, object] = {
        "symbol": "NVDA",
        "kind": OpportunityKind.fundamental_acceleration,
        "discovered_at": NOW,
        "evidence": (ev("revenue"),),
        "dedupe_key": "NVDA:10-Q:2026Q2",
    }
    base.update(kw)
    return Opportunity(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- safety


def test_the_module_cannot_reach_a_venue():
    """§32. No execution authority, asserted from the import graph.

    Parsed rather than grepped: a comment mentioning `app.oms` should not fail
    this, and an import hidden inside a function should not pass it.
    """
    source = pathlib.Path("app/research/opportunity.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("app.oms", "app.brokers", "app.execution", "app.positions", "MetaTrader5")
    for name in imported:
        for bad in forbidden:
            assert not name.startswith(bad), f"research imported {name}"


# ---------------------------------------------------------------- provenance


def test_evidence_without_a_source_is_refused():
    with pytest.raises(ValueError, match="no source"):
        Evidence(key="revenue", value=1.0, source="  ", observed_at=NOW)


def test_naive_timestamps_are_refused():
    """A naive stamp is how look-ahead arrives without anyone noticing."""
    with pytest.raises(ValueError, match="naive"):
        Evidence(
            key="revenue",
            value=1.0,
            source="EDGAR",
            observed_at=datetime(2026, 9, 10, 12, 0),
        )


def test_an_opportunity_with_no_evidence_is_blocked():
    a = assess(opp(evidence=()), seen={}, now=NOW)
    assert "NO_PROVENANCE" in a.blocking_factors
    assert not a.promotable
    assert a.state is OpportunityState.blocked


# ---------------------------------------------------------------- refusal


def test_absent_inputs_produce_no_score_rather_than_a_neutral_one():
    """The central property. Below half coverage there is no number at all."""
    a = assess(opp(), seen={}, now=NOW)
    assert a.priority_score is None
    assert a.confidence is None
    assert "INSUFFICIENT_DATA" in a.blocking_factors
    assert a.research_value is ResearchValue.not_worth_researching


def test_the_two_uncomputable_assessments_name_their_missing_input():
    a = assess(opp(), seen={}, now=NOW)
    assert a.expectation_gap is Verdict.insufficient_data
    assert a.base_rate is Verdict.insufficient_data
    joined = " ".join(a.data_gaps)
    assert "consensus estimates" in joined
    assert "point-in-time fundamental panel" in joined


def test_edge_is_no_edge_without_a_selection_result():
    a = assess(opp(), seen={}, now=NOW)
    assert a.edge is EdgeStrength.no_edge


def test_a_search_at_or_below_chance_does_not_grant_edge():
    """`CLAUDE.md`'s repeated finding, encoded: clearing is not beating."""
    a = assess(
        opp(),
        seen={},
        now=NOW,
        values={"earnings_impact_pct": 0.2, "fcf": 10.0, "net_income": 8.0},
        fundamentals=Direction.up,
        price=Direction.down,
        selection_clears=3,
        selection_candidates=5,
    )
    assert a.edge is EdgeStrength.no_edge
    assert any("at or below chance" in r for r in a.reason_codes)


def test_clearing_above_chance_grants_only_potential_edge():
    a = assess(
        opp(),
        seen={},
        now=NOW,
        values={"earnings_impact_pct": 0.2, "fcf": 10.0, "net_income": 8.0},
        fundamentals=Direction.up,
        price=Direction.down,
        selection_clears=9,
        selection_candidates=2,
    )
    # Not `strong_edge`. Beating the null is the weakest of the gates.
    assert a.edge is EdgeStrength.potential_edge


# ---------------------------------------------------------------- novelty


def test_the_same_event_twice_is_a_duplicate_and_blocks():
    first = opp()
    a = assess(opp(), seen={first.dedupe_key: first}, now=NOW)
    assert a.novelty is Novelty.duplicate
    assert "DUPLICATE" in a.blocking_factors


def test_the_same_event_with_a_different_thesis_is_not_a_duplicate():
    first = opp(thesis="margin expansion is structural")
    later = opp(thesis="margin expansion is one cycle")
    assert novelty_of(later, {first.dedupe_key: first}, now=NOW) is Novelty.thesis_changed


def test_an_old_recurrence_is_recurring_not_duplicate():
    first = opp(discovered_at=NOW - timedelta(days=60))
    assert novelty_of(opp(), {first.dedupe_key: first}, now=NOW) is Novelty.recurring


# ---------------------------------------------------------------- freshness


def test_freshness_follows_the_oldest_evidence_not_the_newest():
    """A fresh price beside a stale filing is not a fresh opportunity."""
    mixed = opp(evidence=(ev("price", age_days=0.0), ev("revenue", age_days=45.0)))
    assert freshness_of(mixed, now=NOW) is Freshness.stale


def test_expired_opportunities_block():
    old = opp(evidence=(ev("revenue", age_days=200.0),))
    a = assess(old, seen={}, now=NOW)
    assert a.freshness is Freshness.expired
    assert "EXPIRED" in a.blocking_factors


# ---------------------------------------------------------------- divergence


@pytest.mark.parametrize(
    "fund,price,expected",
    [
        (Direction.up, Direction.down, Verdict.yes),
        (Direction.down, Direction.up, Verdict.yes),
        (Direction.up, Direction.up, Verdict.no),
        (Direction.down, Direction.down, Verdict.no),
        (Direction.up, Direction.flat, Verdict.weak),
        (Direction.unknown, Direction.up, Verdict.insufficient_data),
    ],
)
def test_divergence_classification(fund, price, expected):
    assert classify_divergence(fund, price)[1] is expected


# ------------------------------------------------------- false opportunity


def test_weak_cash_conversion_is_flagged():
    a = assess(
        opp(),
        seen={},
        now=NOW,
        values={"earnings_impact_pct": 0.2, "fcf": 3.0, "net_income": 10.0},
        fundamentals=Direction.up,
        price=Direction.down,
    )
    assert "WEAK_CASH_CONVERSION" in a.reason_codes


def test_two_flags_make_it_high_risk_and_block():
    a = assess(
        opp(),
        seen={},
        now=NOW,
        values={
            "earnings_impact_pct": 0.2,
            "fcf": 3.0,
            "net_income": 10.0,
            "revenue_growth": -0.05,
            "margin_delta": 0.03,
        },
        fundamentals=Direction.up,
        price=Direction.down,
    )
    assert a.false_opportunity is FalseOpportunityRisk.high
    assert "FALSE_OPPORTUNITY_HIGH" in a.blocking_factors
    assert not a.promotable


def test_no_flags_with_no_inputs_is_unknown_not_low():
    """A check that could not run must not read as a clean bill of health."""
    a = assess(opp(), seen={}, now=NOW, values={"earnings_impact_pct": 0.2})
    assert a.false_opportunity is FalseOpportunityRisk.unknown


# ---------------------------------------------------------------- counter-thesis


def test_every_assessment_carries_a_counter_thesis():
    a = assess(opp(), seen={}, now=NOW)
    assert a.counter_thesis
    assert any("already price" in c for c in a.counter_thesis)


def test_flags_appear_in_the_counter_thesis():
    a = assess(
        opp(),
        seen={},
        now=NOW,
        values={"earnings_impact_pct": 0.2, "fcf": 3.0, "net_income": 10.0},
        fundamentals=Direction.up,
        price=Direction.down,
    )
    assert any("WEAK_CASH_CONVERSION" in c for c in a.counter_thesis)


# ---------------------------------------------------------------- scoring


def test_a_well_covered_opportunity_scores_and_stays_bounded():
    a = assess(
        opp(),
        seen={},
        now=NOW,
        values={
            "earnings_impact_pct": 0.25,
            "fcf": 12.0,
            "net_income": 10.0,
            "revenue_growth": 0.2,
        },
        fundamentals=Direction.up,
        price=Direction.down,
    )
    assert a.priority_score is not None
    assert 0.0 <= a.priority_score <= 1.0
    assert a.promotable
    assert a.coverage >= 0.5


def test_an_immaterial_change_does_not_become_a_research_task():
    a = assess(
        opp(),
        seen={},
        now=NOW,
        values={
            "earnings_impact_pct": 0.001,
            "fcf": 12.0,
            "net_income": 10.0,
            "revenue_growth": 0.2,
        },
        fundamentals=Direction.up,
        price=Direction.up,
    )
    assert a.materiality is Verdict.no
    assert a.research_value in (
        ResearchValue.not_worth_researching,
        ResearchValue.low_value,
    )
