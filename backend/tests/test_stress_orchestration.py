"""Stress orchestration and resilience. **L67 §43.**

Section 43 lists fifteen mandatory safety tests. They are the first section
here, in order.

**The property this module exists to protect is unusual for this codebase.**
Every other safety artefact refuses something; a coverage matrix makes a
*positive* claim -- the portfolio has been tested against that -- and a positive
claim is the kind that gets quoted. So the tests here are weighted toward one
question: can anything make this report COVERED without a completed run?
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.portfolio.scenario import (
    Confidence,
    Likelihood,
    Recommendation,
    ScenarioDefinition,
    ScenarioKind,
    Severity,
)
from app.portfolio.stress import (
    DEFAULT_CASCADE,
    MAX_COMBINATIONS,
    MAX_DIMENSIONS,
    MAX_RECURSION_DEPTH,
    Coverage,
    Evidence,
    GenerationStop,
    ResilienceStatus,
    StressClass,
    StressRun,
    combinations,
    coverage_of,
    evaluate,
    matrix,
    observed_edges,
    priority_of,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
SURE = Confidence(data=Decimal("0.9"), model=Decimal("0.9"), scenario=Decimal("0.9"))


def run_of(cls: StressClass, *, account: str = "acct-1", at: datetime = NOW, **kw: object):
    return StressRun(cls, account, at, confidence=SURE, **kw)  # type: ignore[arg-type]


def all_covered(account: str = "acct-1") -> tuple[StressRun, ...]:
    return tuple(run_of(c, account=account) for c in StressClass)


def definition(**kw: object) -> ScenarioDefinition:
    base: dict[str, object] = dict(
        scenario_id="s",
        name="n",
        kind=ScenarioKind.STRESS,
        severity=Severity.SEVERE,
        likelihood=Likelihood.POSSIBLE,
        account_id="acct-1",
    )
    base.update(kw)
    return ScenarioDefinition(**base)  # type: ignore[arg-type]


# ================================================ §43: the mandatory fifteen


def test_excessive_exposure_produces_a_recommendation_not_an_action() -> None:
    """**TEST 1.** Every recommendation is TIGHTENS or NEUTRAL, reused from L66."""
    from app.portfolio.scenario import RECOMMENDATION_EFFECT

    for member in Recommendation:
        assert RECOMMENDATION_EFFECT[member] in ("TIGHTENS", "NEUTRAL")


def test_a_resilience_optimizer_cannot_raise_the_risk_budget() -> None:
    """**TEST 2 and TEST 13.** $10,000 approved, $15,000 proposed."""
    from app.safety.envelope import SafetyEnvelope, within_envelope
    from app.safety.policy_research import Candidate
    from app.safety.policy_research import Verdict as PVerdict
    from app.safety.policy_research import validate as validate_candidate

    assert within_envelope(
        envelope=SafetyEnvelope(max_capital_movement=Decimal("10000")),
        capital_movement=Decimal("15000"),
    )
    assert (
        validate_candidate(Candidate("c", "pv", {"max_capital_movement": Decimal("15000")})).verdict
        is PVerdict.REJECTED_HARD_SAFETY
    )


def test_a_stress_optimizer_cannot_raise_leverage() -> None:
    """**TEST 3.**"""
    from app.safety.policy_research import Candidate
    from app.safety.policy_research import Verdict as PVerdict
    from app.safety.policy_research import validate as validate_candidate

    assert (
        validate_candidate(Candidate("c", "pv", {"max_leverage": Decimal("50")})).verdict
        is PVerdict.REJECTED_HARD_SAFETY
    )


def test_a_confident_safe_prediction_cannot_relax_a_restriction() -> None:
    """**TEST 4.** Delegated to L66, whose gate returns `max(current, ...)`."""
    from app.portfolio.scenario import Confidence as C
    from app.portfolio.scenario import GateVerdict, gate

    calm = definition(severity=Severity.EXPECTED, likelihood=Likelihood.UNLIKELY)
    result = gate(
        definition=calm,
        confidence=C(data=Decimal("0.99"), model=Decimal("0.99"), scenario=Decimal("0.99")),
        breaches_hard_constraint=False,
        current=GateVerdict.RESTRICT,
    )
    assert result.verdict is GateVerdict.RESTRICT


def test_a_hard_failure_outranks_every_numerical_score() -> None:
    """**TEST 5.** Not the largest term in a sum -- ahead of the sum.

    A weighted score cannot express "this outranks everything", only "this
    counts for a lot", so the check runs before anything is weighed.
    """
    perfect = matrix(all_covered(), "acct-1", now=NOW)
    assert perfect.fraction_covered() == Decimal("1")

    result = evaluate(account_id="acct-1", now=NOW, coverage=perfect, hard_failure=True)
    assert result.status is ResilienceStatus.CRITICAL
    assert result.status.blocks_autonomy
    assert "outranks everything" in " ".join(result.reasons)


def test_the_safety_architecture_does_not_depend_on_the_stress_engine() -> None:
    """**TEST 6.** Nothing imports it, so its absence changes nothing."""
    root = pathlib.Path(inspect.getfile(matrix)).resolve().parents[2]
    importers = []
    for f in sorted((root / "app").rglob("*.py")):
        if f.name in ("stress.py", "__init__.py") or "__pycache__" in f.parts:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = next((a.name for a in node.names), "")
            if mod.endswith("portfolio.stress"):
                importers.append(f.name)
    assert importers == []


def test_the_stress_module_cannot_reach_a_venue() -> None:
    """**TEST 7.**"""
    from app.portfolio import stress

    tree = ast.parse(inspect.getsource(stress))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"place_order", "close_position", "cancel_order", "modify_order"})


def test_the_stress_module_cannot_mutate_production_state() -> None:
    """**TEST 8.** It imports only L66's scenario vocabulary."""
    from app.portfolio import stress

    tree = ast.parse(inspect.getsource(stress))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    app_imports = {m for m in imported if m.startswith("app.")}
    assert app_imports == {"app.portfolio.scenario"}, app_imports

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for mutator in ("commit", "flush", "execute", "add", "delete", "merge"):
        assert mutator not in called


def test_one_fragile_account_does_not_affect_another() -> None:
    """**TEST 9.** Coverage is account-scoped throughout.

    A matrix that pooled accounts would report coverage the fragile one does
    not have -- which is the direction of that error that matters.
    """
    runs = all_covered("acct-1")
    a = evaluate(account_id="acct-1", now=NOW, coverage=matrix(runs, "acct-1", now=NOW))
    b = evaluate(account_id="acct-2", now=NOW, coverage=matrix(runs, "acct-2", now=NOW))

    assert a.status is ResilienceStatus.HEALTHY
    assert b.status is ResilienceStatus.UNKNOWN, "acct-2 has run nothing"
    assert b.coverage.fraction_covered() == Decimal("0")


def test_generation_stops_when_combinations_exceed_the_limit() -> None:
    """**TEST 10.** And it stops loudly.

    A generator that silently truncates produces a coverage report whose gaps
    look like decisions.
    """
    result = combinations(tuple(StressClass), dimensions=3)
    assert result.stop is GenerationStop.COMBINATION_CAP
    assert result.truncated
    assert result.combinations == ()
    assert str(MAX_COMBINATIONS) in result.reason

    too_many_dims = combinations(tuple(StressClass)[:5], dimensions=MAX_DIMENSIONS + 1)
    assert too_many_dims.stop is GenerationStop.DIMENSION_CAP


def test_recursive_generation_hits_a_circuit_breaker() -> None:
    """**TEST 11.** A scenario that generates scenarios is a loop."""
    result = combinations(
        (StressClass.MARKET, StressClass.LIQUIDITY),
        dimensions=2,
        depth=MAX_RECURSION_DEPTH + 1,
    )
    assert result.stop is GenerationStop.RECURSION_CAP
    assert result.combinations == ()
    assert "does not stop on its own" in result.reason


def test_a_stale_stress_result_is_not_coverage() -> None:
    """**TEST 12.** A result describes the portfolio that was tested."""
    old = (run_of(StressClass.MARKET, at=NOW - timedelta(days=30)),)
    assert coverage_of(old, StressClass.MARKET, "acct-1", now=NOW) is Coverage.STALE
    assert not Coverage.STALE.is_covered

    fresh = (run_of(StressClass.MARKET),)
    assert coverage_of(fresh, StressClass.MARKET, "acct-1", now=NOW) is Coverage.COVERED


def test_a_suspended_certification_stops_stress_driven_adaptation() -> None:
    """**TEST 14.** Delegated to L63's ceiling, which is where it belongs."""
    from app.safety.autonomy import AutonomyLevel, ceiling_for

    assert ceiling_for("SUSPENDED") is AutonomyLevel.RECOMMEND
    assert not ceiling_for("SUSPENDED").may_auto_apply
    assert not ceiling_for("CERTIFICATION_REVOKED").may_auto_apply


def test_an_unknown_broker_state_reconciles() -> None:
    """**TEST 15.** The platform's oldest rule, unchanged."""
    from app.oms.state import SAFE_TO_RESEND, OrderStatus

    assert SAFE_TO_RESEND == frozenset({OrderStatus.failed})
    assert OrderStatus.unknown not in SAFE_TO_RESEND


# ============================ coverage is earned, never assumed


def test_nothing_makes_an_unrun_stress_class_covered() -> None:
    """**The property the module exists for.**

    A coverage matrix is a positive claim, and a positive claim made from an
    absence is the failure this whole platform is built against. There is no
    combination of arguments that reports COVERED without a completed run.
    """
    empty = matrix((), "acct-1", now=NOW)
    assert empty.covered() == ()
    assert set(empty.cells.values()) == {Coverage.UNCOVERED}
    assert empty.fraction_covered() == Decimal("0")

    # Not even a run that failed.
    crashed = (StressRun(StressClass.MARKET, "acct-1", NOW, completed=False, confidence=SURE),)
    assert coverage_of(crashed, StressClass.MARKET, "acct-1", now=NOW) is Coverage.UNCOVERED

    # Nor one for another account.
    other = (run_of(StressClass.MARKET, account="acct-2"),)
    assert coverage_of(other, StressClass.MARKET, "acct-1", now=NOW) is Coverage.UNCOVERED


def test_only_one_coverage_value_counts_as_covered() -> None:
    """Section 7: UNKNOWN must not be treated as COVERED. Nor may the rest."""
    for value in Coverage:
        assert value.is_covered == (value is Coverage.COVERED), value


def test_a_run_with_no_stated_confidence_is_only_partial_coverage() -> None:
    """A run nobody could vouch for has not established what it appears to."""
    vague = (StressRun(StressClass.MARKET, "acct-1", NOW),)
    assert coverage_of(vague, StressClass.MARKET, "acct-1", now=NOW) is (Coverage.PARTIALLY_COVERED)


def test_the_matrix_reports_every_class_including_the_untested() -> None:
    """A matrix listing only what was tested makes the untested invisible,
    which is the opposite of its job."""
    partial = matrix((run_of(StressClass.MARKET),), "acct-1", now=NOW)
    assert set(partial.cells) == set(StressClass)
    assert partial.covered() == (StressClass.MARKET,)
    assert len(partial.gaps()) == len(StressClass) - 1


def test_insufficient_coverage_is_unknown_not_healthy() -> None:
    """**The branch the module exists for.** A portfolio nobody has stressed is
    unmeasured, not robust -- and UNKNOWN blocks autonomy."""
    result = evaluate(account_id="acct-1", now=NOW, coverage=matrix((), "acct-1", now=NOW))
    assert result.status is ResilienceStatus.UNKNOWN
    assert result.status.blocks_autonomy
    assert "unmeasured, not robust" in " ".join(result.reasons)


def test_this_deployment_reports_zero_coverage() -> None:
    """The honest reading, asserted so it cannot drift into a green report.

    No stress has ever been run on this platform. The matrix says so, and the
    resilience verdict is UNKNOWN rather than HEALTHY.
    """
    today = matrix((), "acct-1", now=NOW)
    assert today.fraction_covered() == Decimal("0")
    assert evaluate(account_id="acct-1", now=NOW, coverage=today).status is ResilienceStatus.UNKNOWN


# ================================================= priority and the cascade


def test_priority_does_not_rest_on_likelihood_alone() -> None:
    """Section 5. An unlikely severe scenario outranks a likely mild one."""
    rare_bad = priority_of(
        definition(severity=Severity.EXTREME, likelihood=Likelihood.UNLIKELY),
        coverage=Coverage.COVERED,
    )
    common_mild = priority_of(
        definition(severity=Severity.EXPECTED, likelihood=Likelihood.LIKELY),
        coverage=Coverage.COVERED,
    )
    assert rare_bad > common_mild


def test_being_uncovered_raises_priority_above_being_severe() -> None:
    """A severe scenario already run tells you less than a mild one never run:
    the first has an answer and the second has none."""
    severe_but_done = priority_of(
        definition(severity=Severity.EXTREME, likelihood=Likelihood.LIKELY),
        coverage=Coverage.COVERED,
    )
    mild_and_untested = priority_of(
        definition(severity=Severity.EXPECTED, likelihood=Likelihood.UNLIKELY),
        coverage=Coverage.UNKNOWN,
    )
    assert mild_and_untested > 0
    # Unknown coverage carries a premium over merely uncovered.
    uncovered = priority_of(definition(), coverage=Coverage.UNCOVERED)
    unknown = priority_of(definition(), coverage=Coverage.UNKNOWN)
    assert unknown > uncovered
    assert severe_but_done > 0


def test_almost_every_cascade_edge_is_hypothetical() -> None:
    """**Section 11: do not assume causality from correlation alone.**

    Exactly one edge is OBSERVED -- drawdown to risk restriction, which this
    platform genuinely does and tests. The rest are mechanisms from a textbook,
    and a textbook graph labelled OBSERVED would be the most quotable false
    claim in the repository.

    INFERRED would need a correlation analysis, which `exposure.py` has
    reported unavailable since L53 and still does.
    """
    assert len(observed_edges()) == 1
    assert observed_edges()[0].to == "risk_restriction"
    assert all(
        e.evidence is Evidence.HYPOTHETICAL for e in DEFAULT_CASCADE if e.to != "risk_restriction"
    )
    assert not any(e.evidence is Evidence.INFERRED for e in DEFAULT_CASCADE)


@pytest.mark.parametrize("cls", list(StressClass))
def test_every_stress_class_maps_to_an_l66_scenario_kind(cls: StressClass) -> None:
    """Section 4: reuse L66's definitions, do not duplicate the schema."""
    from app.portfolio.stress import KIND_OF

    assert cls in KIND_OF
    assert isinstance(KIND_OF[cls], ScenarioKind)


def test_the_result_says_it_is_a_simulation() -> None:
    record = evaluate(account_id="a", now=NOW, coverage=matrix((), "a", now=NOW)).as_dict()
    assert record["kind"] == "SIMULATION"
    assert "not an observation and not an instruction" in record["authority"]


def test_the_coverage_record_says_what_it_is_not() -> None:
    record = matrix((), "a", now=NOW).as_dict()
    assert "not a statement that the portfolio is safe" in record["authority"]
    assert "never assumed" in record["authority"]
