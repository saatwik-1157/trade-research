"""Predictive scenario intelligence. **L66 §46.**

Section 46 lists fifteen mandatory safety tests. They are the first section
here, in order.

**Almost all of them reduce to one property:** a prediction may tighten and may
never loosen. `gate()` takes the verdict the platform already holds and returns
`max(current, everything the scenario adds)`, so there is no input -- however
confident, however favourable -- that makes the answer more permissive than it
already was. Section 47's rule that prediction must never become direct
execution is therefore a property of the return value rather than a policy
somebody enforces.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.portfolio.scenario import (
    BOUNDS,
    LOOSENING_RECOMMENDATIONS,
    MATRIX,
    RECOMMENDATION_EFFECT,
    Confidence,
    Freshness,
    GateVerdict,
    Likelihood,
    Outlook,
    Recommendation,
    RiskClass,
    ScenarioDefinition,
    ScenarioKind,
    Severity,
    classify,
    conservative_of,
    gate,
    validate,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
SURE = Confidence(data=Decimal("0.95"), model=Decimal("0.95"), scenario=Decimal("0.95"))


def scenario(**kw: object) -> ScenarioDefinition:
    base: dict[str, object] = dict(
        scenario_id="s1",
        name="volatility spike",
        kind=ScenarioKind.VOLATILITY,
        severity=Severity.SEVERE,
        likelihood=Likelihood.POSSIBLE,
        account_id="acct-1",
        inputs={"volatility_pct": Decimal("20")},
    )
    base.update(kw)
    return ScenarioDefinition(**base)  # type: ignore[arg-type]


def run(**kw: object):
    base: dict[str, object] = dict(
        definition=scenario(),
        confidence=SURE,
        breaches_hard_constraint=False,
    )
    base.update(kw)
    return gate(**base)  # type: ignore[arg-type]


# ================================================ §46: the mandatory fifteen


def test_a_scenario_predicting_more_risk_warns_and_changes_no_limit() -> None:
    """**TEST 1.** It produces a verdict, not a new limit.

    The module has no writer for any limit -- there is no field on any type
    here that a risk ceiling could be assigned to.
    """
    result = run(definition=scenario(severity=Severity.EXTREME, likelihood=Likelihood.LIKELY))
    assert result.risk_class is RiskClass.CRITICAL
    assert result.verdict is GateVerdict.RESTRICT
    assert "not a risk check" in result.as_dict()["authority"]


def test_a_recommendation_still_travels_the_normal_control_chain() -> None:
    """**TEST 2.** `may_proceed` means "to verification", not "to a venue"."""
    result = run()
    assert "RiskEngine remains" in result.as_dict()["authority"]
    assert "worth putting in front of the safety chain" in result.as_dict()["authority"]


def test_a_confident_calm_forecast_cannot_relax_an_existing_restriction() -> None:
    """**TEST 3, and the property the whole module rests on.**

    The RiskEngine has restricted. A scenario engine projects calm with 95%
    confidence on every component. The answer must still be RESTRICT.

    Not because a branch checks for it -- because `gate()` returns
    `max(current, ...)` and there is no path that lowers a verdict.
    """
    calm = scenario(
        name="calm",
        severity=Severity.EXPECTED,
        likelihood=Likelihood.UNLIKELY,
        inputs={"volatility_pct": Decimal("-10")},
    )
    for held in GateVerdict:
        result = gate(
            definition=calm,
            confidence=SURE,
            breaches_hard_constraint=False,
            current=held,
        )
        assert result.verdict >= held, f"a calm forecast lowered {held.name}"


def test_low_confidence_makes_the_handling_more_conservative_not_less() -> None:
    """**TEST 4.** A forecast nobody can vouch for is a reason to do less."""
    unsure = run(confidence=Confidence(data=Decimal("0.2")))
    sure = run(confidence=SURE)
    assert unsure.verdict >= sure.verdict
    assert "low or unstated confidence" in unsure.warnings

    unstated = run(confidence=Confidence())
    assert unstated.verdict >= sure.verdict, "unstated confidence must not read as high"


def test_a_scenario_cannot_reach_a_venue() -> None:
    """**TEST 5.** Blocked by there being no path, not by a check."""
    from app.portfolio import scenario as module

    tree = ast.parse(inspect.getsource(module))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"place_order", "close_position", "cancel_order", "modify_order"})


def test_a_scenario_cannot_mutate_production_state() -> None:
    """**TEST 6.** It imports no session, no engine and no repository."""
    from app.portfolio import scenario as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert [m for m in imported if m.startswith("app.")] == [], (
        "the scenario module reaches into the application"
    )
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for mutator in ("commit", "flush", "execute", "add", "delete", "merge"):
        assert mutator not in called, f"scenario code calls {mutator}()"


def test_a_long_horizon_scenario_loses_to_immediate_safety() -> None:
    """**TEST 7.** Delegated to L65, which delegates to L60."""
    from app.portfolio.decision import Verdict
    from app.portfolio.horizon import Horizon, HorizonRecommendation, synthesise

    d = synthesise(
        decision_id="d",
        account_id="acct-1",
        now=NOW,
        recommendations=(
            HorizonRecommendation(
                Horizon.LONG_TERM, Verdict.ALLOW, "scenario says fine", target="t", at=NOW
            ),
            HorizonRecommendation(
                Horizon.EMERGENCY, Verdict.REJECT, "kill switch", target="t", at=NOW
            ),
        ),
    )
    assert d.verdict is Verdict.REJECT


def test_a_historical_scenario_without_a_data_version_is_refused() -> None:
    """**TEST 8.** Leakage is checked by `app.datasets.leakage`, which predates
    this level. What this refuses is a replay that cannot be *checked* -- one
    that does not say which data it replayed."""
    bad = scenario(kind=ScenarioKind.HISTORICAL, data_version="")
    result = validate(bad)
    assert not result.ok
    assert any("cannot be reproduced" in v for v in result.violations)

    good = validate(scenario(kind=ScenarioKind.HISTORICAL, data_version="bars@2026-01"))
    assert good.ok

    from app.datasets import leakage

    assert leakage is not None, "the leakage control is the one that checks the replay"


#: The one module permitted to import the scenario engine.
#:
#: `app/portfolio/stress.py` (L67) reuses this module's `ScenarioDefinition`,
#: `Recommendation` and risk matrix rather than defining second copies, which
#: is what L67 section 4 asks for. It is allow-listed rather than excluded from
#: the rule, and the chain terminates safely because **stress is itself
#: unreachable** -- `test_stress_orchestration.py::
#: test_the_safety_architecture_does_not_depend_on_the_stress_engine` asserts
#: nothing imports it either. The two tests together give the transitive
#: property: no module the platform runs depends on either.
SCENARIO_IMPORTERS_ALLOWED = {"stress.py"}


def test_the_safety_architecture_does_not_depend_on_the_scenario_engine() -> None:
    """**TEST 9.** Nothing reachable imports it, so its absence changes nothing.

    This caught L67 adding `stress.py` as an importer, which is why the
    allow-list above is explicit rather than the rule being loosened.
    """
    import pathlib

    root = pathlib.Path(inspect.getfile(gate)).resolve().parents[2]
    importers = []
    for f in sorted((root / "app").rglob("*.py")):
        if (
            f.name in ("scenario.py", "__init__.py")
            or f.name in SCENARIO_IMPORTERS_ALLOWED
            or "__pycache__" in f.parts
        ):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = next((a.name for a in node.names), "")
            if mod.endswith("portfolio.scenario"):
                importers.append(f.name)
    assert importers == []


def test_an_unavailable_model_falls_back_to_the_deterministic_baseline() -> None:
    """**TEST 10.** And it needs no fallback branch, because the baseline was
    always the floor: a model may raise a stress estimate and never lower one."""
    baseline = Outlook(
        "expected_drawdown", Decimal("8"), "rolling historical", NOW, timedelta(hours=6)
    )
    chosen, why = conservative_of(baseline, None)
    assert chosen is baseline
    assert "never removes a floor" in why

    optimistic = Outlook(
        "expected_drawdown",
        Decimal("2"),
        "model",
        NOW,
        timedelta(hours=6),
        confidence=SURE,
    )
    chosen, why = conservative_of(baseline, optimistic)
    assert chosen is baseline
    assert "may raise a stress estimate and may never lower one" in why

    pessimistic = Outlook(
        "expected_drawdown",
        Decimal("15"),
        "model",
        NOW,
        timedelta(hours=6),
        confidence=SURE,
    )
    chosen, _ = conservative_of(baseline, pessimistic)
    assert chosen is pessimistic, "a model may tighten"


def test_a_low_confidence_model_does_not_displace_the_baseline() -> None:
    """The other half of TEST 10 and of TEST 4 together."""
    baseline = Outlook("x", Decimal("8"), "baseline", NOW, timedelta(hours=6))
    loud_but_unsure = Outlook(
        "x",
        Decimal("50"),
        "model",
        NOW,
        timedelta(hours=6),
        confidence=Confidence(model=Decimal("0.1")),
    )
    chosen, why = conservative_of(baseline, loud_but_unsure)
    assert chosen is baseline
    assert "treated as low, never as high" in why


def test_a_proposal_breaching_a_hard_constraint_is_rejected() -> None:
    """**TEST 11.** A hard constraint is not a scenario input."""
    result = run(breaches_hard_constraint=True)
    assert result.verdict is GateVerdict.REJECT
    assert "not a scenario input" in result.reason


def test_no_scenario_can_turn_10000_of_approved_risk_into_15000() -> None:
    """**TEST 12.** Refused where budgets live, and asserted from here."""
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


def test_safe_mode_stops_a_scenario_recommendation_applying() -> None:
    """**TEST 13.** A scenario cannot argue a portfolio out of a latched
    refusal, whatever it projects."""
    result = run(safe_mode=True)
    assert result.verdict is GateVerdict.REJECT
    assert not result.may_proceed
    assert "latched refusal" in result.reason


def test_a_suspended_certification_stops_scenario_driven_adaptation() -> None:
    """**TEST 14.**"""
    result = run(certification_permits=False)
    assert result.verdict is GateVerdict.REJECT
    assert "without a person" in result.reason


def test_an_unknown_order_state_is_reconciled_before_acting_on_a_forecast() -> None:
    """**TEST 15.**"""
    result = run(unreconciled_order=True)
    assert result.verdict is GateVerdict.REJECT
    assert "never by acting on a forecast" in result.reason


@pytest.mark.parametrize("state", [Freshness.STALE, Freshness.MISSING, Freshness.INVALID])
def test_a_projection_from_data_that_is_not_current_is_deferred(state: Freshness) -> None:
    """Section 13. A projection needs evidence about now to be about next.

    DEFER rather than REJECT: the scenario is not wrong, it is unsupported, and
    those warrant different responses. Refreshing the data makes it answerable;
    nothing makes a rejected one answerable.
    """
    result = run(data=state)
    assert result.verdict is GateVerdict.DEFER
    assert "not evidence about what happens next" in result.reason


def test_fresh_data_adds_no_restriction_of_its_own() -> None:
    """The other direction, so the check above is not passing vacuously."""
    assert run(data=Freshness.FRESH).verdict < GateVerdict.DEFER


# ===================================== the asymmetry, asserted exhaustively


@pytest.mark.parametrize("held", list(GateVerdict))
def test_no_scenario_input_can_ever_lower_the_verdict(held: GateVerdict) -> None:
    """**The property section 47 asks for, checked across every combination.**

    Every severity, every likelihood, every confidence, against every verdict
    the platform might already hold. There is no cell in which the gate returns
    something more permissive than what it was given.
    """
    for severity in Severity:
        for likelihood in Likelihood:
            for conf in (SURE, Confidence(), Confidence(data=Decimal("0.1"))):
                result = gate(
                    definition=scenario(severity=severity, likelihood=likelihood),
                    confidence=conf,
                    breaches_hard_constraint=False,
                    current=held,
                )
                assert result.verdict >= held, (
                    f"{severity.name}/{likelihood.name} lowered {held.name}"
                )


def test_the_engine_has_no_recommendation_that_loosens_anything() -> None:
    """A forecast with an upside is one somebody will act on for its upside.

    Checked by EFFECT, not by name. The first version matched substrings and
    flagged `DEFER_INCREASE` -- which withholds an increase and is a
    restriction. Matching characters gets false positives and, worse, false
    negatives: a member called `OPTIMISE_HEADROOM` would have passed.
    """
    assert LOOSENING_RECOMMENDATIONS == frozenset()

    # Every member is classified, so a new one added without an effect fails
    # here rather than defaulting to harmless.
    for member in Recommendation:
        assert member in RECOMMENDATION_EFFECT, f"{member.name} has no declared effect"
        assert RECOMMENDATION_EFFECT[member] in ("TIGHTENS", "NEUTRAL"), (
            f"{member.name} loosens something"
        )


# ================================================ the matrix and validation


def test_an_unlikely_extreme_scenario_stays_visible() -> None:
    """**Section 20**: do not use likelihood alone.

    The matrix is written out rather than computed as likelihood x severity
    precisely because the product buries this row.
    """
    assert classify(Likelihood.UNLIKELY, Severity.EXTREME) is RiskClass.HIGH
    assert classify(Likelihood.LIKELY, Severity.EXPECTED) is RiskClass.MEDIUM
    # A likely-but-mild event does not outrank an unlikely catastrophe.
    assert classify(Likelihood.UNLIKELY, Severity.EXTREME) > classify(
        Likelihood.LIKELY, Severity.EXPECTED
    )


def test_the_matrix_is_total() -> None:
    """An unmapped pair would fall through to whatever a default said."""
    for likelihood in Likelihood:
        for severity in Severity:
            assert (likelihood, severity) in MATRIX


def test_a_scenario_is_never_defined_against_live() -> None:
    result = validate(scenario(environment="live"))
    assert not result.ok
    assert any("live environment" in v for v in result.violations)


def test_an_unrecognised_scenario_input_is_refused() -> None:
    """An input nobody bounded is an input nobody thought about."""
    result = validate(scenario(inputs={"some_knob_nobody_bounded": Decimal("1")}))
    assert not result.ok
    assert any("not a recognised scenario input" in v for v in result.violations)


@pytest.mark.parametrize("name", sorted(BOUNDS))
def test_every_bounded_input_refuses_a_value_outside_its_bound(name: str) -> None:
    low, high = BOUNDS[name]
    assert not validate(scenario(inputs={name: high + Decimal("1")})).ok
    assert not validate(scenario(inputs={name: low - Decimal("1")})).ok
    assert validate(scenario(inputs={name: high})).ok


def test_a_duplicate_scenario_is_detected() -> None:
    """Section 38. The same question under the same versions is one scenario."""
    first = scenario(data_version="d1", model_version="m1", policy_version="p1")
    seen = {first.fingerprint(): "s1"}

    renamed = scenario(
        scenario_id="s2",
        name="different name",
        data_version="d1",
        model_version="m1",
        policy_version="p1",
    )
    assert not validate(renamed, seen=seen).ok

    # A different data version is a different scenario: the same inputs against
    # different data have not actually been run.
    newer = scenario(data_version="d2", model_version="m1", policy_version="p1")
    assert validate(newer, seen=seen).ok


def test_confidence_distinguishes_unmeasured_from_measured_and_bad() -> None:
    """Section 13. `None`, not zero and not one."""
    assert Confidence().weakest() is None
    assert Confidence().is_low(), "unmeasured counts as low"
    assert Confidence(data=Decimal("0.0")).weakest() == Decimal("0")
    assert Confidence(data=Decimal("0.9"), model=Decimal("0.2")).weakest() == Decimal("0.2")


def test_an_outlook_says_it_is_a_prediction() -> None:
    """Section 42: clearly distinguish prediction from observation."""
    record = Outlook("x", Decimal("1"), "method", NOW, timedelta(hours=1)).as_dict()
    assert record["kind"] == "PREDICTION"
    assert "not an observation" in record["authority"]
    assert "may tighten a decision and can never loosen one" in record["authority"]


def test_an_outlook_expires() -> None:
    o = Outlook("x", Decimal("1"), "m", NOW, timedelta(hours=1))
    assert not o.expired(now=NOW)
    assert o.expired(now=NOW + timedelta(hours=2))
