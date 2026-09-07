"""Multi-horizon decision coordination. **L65 §44.**

Section 44 lists twelve mandatory safety tests. They are the first section
here, in order.

The property under all of them: **a longer horizon can never displace a shorter
one on safety**, and the reason it holds is that horizon does not resolve
anything. `synthesise()` maps each horizon onto a `Layer` and hands the
resolution to L60's `decide()`, which already cannot express a lower layer
relaxing a higher one. A second ordering here would have been two engines that
agree until they do not.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.portfolio.decision import DecisionContext, Input, Layer, Verdict
from app.portfolio.horizon import (
    DEFAULT_VALIDITY,
    LAYER_OF,
    Deferred,
    Horizon,
    HorizonRecommendation,
    revalidate,
    synthesise,
    worth_acting_on,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def rec(horizon: Horizon, verdict: Verdict, reason: str = "because", **kw: object):
    kw.setdefault("target", "alloc")
    kw.setdefault("at", NOW)
    return HorizonRecommendation(horizon, verdict, reason, **kw)  # type: ignore[arg-type]


def run(*recommendations: HorizonRecommendation, **kw: object):
    return synthesise(
        decision_id="d",
        account_id=str(kw.pop("account_id", "acct-1")),
        now=kw.pop("now", NOW),  # type: ignore[arg-type]
        recommendations=recommendations,
        **kw,  # type: ignore[arg-type]
    )


# ================================================ §44: the mandatory twelve


def test_a_long_term_increase_loses_to_an_exposure_limit() -> None:
    """**TEST 1.** And the long-term view is kept, not thrown away."""
    d = run(
        rec(Horizon.LONG_TERM, Verdict.ALLOW, "strategy deserves more capital"),
        rec(Horizon.IMMEDIATE_RISK, Verdict.RESTRICT, "exposure limit exceeded"),
    )
    assert d.verdict is Verdict.RESTRICT
    assert d.deciding_horizon is Horizon.IMMEDIATE_RISK
    assert [x.recommendation.horizon for x in d.deferred] == [Horizon.LONG_TERM]
    assert "conditions pass" in d.deferred[0].reason


def test_an_excellent_strategy_places_no_order_with_the_broker_down() -> None:
    """**TEST 2.** Long-term quality is not a reason to act into an outage."""
    d = run(
        rec(Horizon.LONG_TERM, Verdict.ALLOW, "best strategy in the book"),
        rec(Horizon.EMERGENCY, Verdict.REJECT, "broker disconnected"),
    )
    assert d.verdict is Verdict.REJECT
    assert d.deciding_horizon is Horizon.EMERGENCY


def test_risk_control_beats_a_healthy_strategy() -> None:
    """**TEST 3.**"""
    d = run(
        rec(Horizon.MEDIUM_TERM, Verdict.ALLOW, "strategy healthy"),
        rec(Horizon.IMMEDIATE_RISK, Verdict.PAUSE, "risk limit breached"),
    )
    assert d.verdict is Verdict.PAUSE


def test_the_most_restrictive_verdict_wins_across_horizons() -> None:
    """**TEST 4.** Short ALLOW, medium RESTRICT, long ALLOW."""
    d = run(
        rec(Horizon.SHORT_TERM, Verdict.ALLOW, "market fine"),
        rec(Horizon.MEDIUM_TERM, Verdict.RESTRICT, "strategy degrading"),
        rec(Horizon.LONG_TERM, Verdict.ALLOW, "increase allocation"),
    )
    assert d.verdict is Verdict.RESTRICT
    assert d.deciding_horizon is Horizon.MEDIUM_TERM
    # Both ALLOWs are kept as deferred, not silently dropped.
    assert {x.recommendation.horizon for x in d.deferred} == {
        Horizon.SHORT_TERM,
        Horizon.LONG_TERM,
    }


def test_an_expired_recommendation_does_not_execute() -> None:
    """**TEST 5.** And an undated one is expired, not eternal."""
    stale = rec(
        Horizon.LONG_TERM,
        Verdict.ALLOW,
        "old view",
        at=NOW - timedelta(days=30),
    )
    d = run(stale, rec(Horizon.SHORT_TERM, Verdict.RESTRICT, "current"))
    assert [r.horizon for r in d.expired] == [Horizon.LONG_TERM]
    assert stale.horizon not in {r.horizon for r in d.recommendations}

    undated = HorizonRecommendation(Horizon.LONG_TERM, Verdict.ALLOW, "no timestamp")
    assert undated.expired(now=NOW), "an undated recommendation cannot be shown current"


def test_a_deferred_recommendation_is_revalidated_before_it_applies() -> None:
    """**TEST 6.** Every gate is re-asked, and only all five permit it."""
    deferred = Deferred(
        recommendation=rec(Horizon.LONG_TERM, Verdict.ALLOW),
        displaced_by=Horizon.IMMEDIATE_RISK,
        reason="risk restricted",
        required_condition="IMMEDIATE_RISK no longer returns RESTRICT",
        expires_at=NOW + timedelta(days=7),
    )
    ok = dict(
        condition_cleared=True,
        certification_permits=True,
        risk_permits=True,
        data_fresh=True,
    )
    assert revalidate(deferred, now=NOW, **ok).allowed

    for gate in ok:
        broken = dict(ok, **{gate: False})
        result = revalidate(deferred, now=NOW, **broken)  # type: ignore[arg-type]
        assert not result.allowed, f"{gate} did not block revalidation"

    late = revalidate(deferred, now=NOW + timedelta(days=8), **ok)
    assert not late.allowed and "expired" in late.reason


def test_a_suspended_certification_stops_autonomous_adaptation() -> None:
    """**TEST 7.** Checked at revalidation, where the action actually is."""
    deferred = Deferred(
        recommendation=rec(Horizon.LONG_TERM, Verdict.ALLOW),
        displaced_by=Horizon.MEDIUM_TERM,
        reason="-",
        required_condition="-",
        expires_at=NOW + timedelta(days=1),
    )
    result = revalidate(
        deferred,
        now=NOW,
        condition_cleared=True,
        certification_permits=False,
        risk_permits=True,
        data_fresh=True,
    )
    assert not result.allowed
    assert "does not carry into another" in result.reason


def test_an_ai_recommendation_to_raise_risk_cannot_win_on_being_long_term() -> None:
    """**TEST 8.** A strategic view enters as OPTIMIZATION -- the second-weakest
    layer -- so it cannot outrank a portfolio or risk constraint by being
    strategic. That is what `LAYER_OF` is for."""
    assert LAYER_OF[Horizon.LONG_TERM].name == "OPTIMIZATION"
    d = run(
        rec(Horizon.LONG_TERM, Verdict.ALLOW, "AI says increase risk"),
        rec(Horizon.MEDIUM_TERM, Verdict.REDUCE, "portfolio concentration"),
    )
    assert d.verdict is Verdict.REDUCE


def test_no_horizon_can_turn_10000_of_approved_risk_into_15000() -> None:
    """**TEST 9.** Refused at the layer that owns budgets, not here.

    This module produces a recommendation; it cannot size anything. The budget
    ceiling is enforced by `app.safety.envelope` (L62) and by
    `app.safety.policy_research` (L64), and asserted here so the multi-horizon
    path is covered explicitly rather than by inheritance.
    """
    from decimal import Decimal as D

    from app.safety.envelope import SafetyEnvelope, within_envelope

    breaches = within_envelope(
        envelope=SafetyEnvelope(max_capital_movement=D("10000")),
        capital_movement=D("15000"),
    )
    assert breaches, "a long-term optimizer proposing 15000 against 10000 must breach"
    assert "max_capital_movement" in str(breaches[0])

    from app.safety.policy_research import Candidate, validate
    from app.safety.policy_research import Verdict as PVerdict

    assert (
        validate(Candidate("c", "pv", {"max_capital_movement": D("15000")})).verdict
        is PVerdict.REJECTED_HARD_SAFETY
    )


def test_stale_market_data_blocks_a_new_short_term_entry() -> None:
    """**TEST 10.** Delegated to L60's `require_fresh`, which turns absent
    evidence into a HARD_SAFETY finding rather than a warning."""
    ctx = DecisionContext(now=NOW)
    ctx.add_input(Input(name="mark_price", source="marketdata", value=None))
    ctx.require_fresh("mark_price")

    d = run(rec(Horizon.SHORT_TERM, Verdict.ALLOW, "signal fired"), context=ctx)
    assert d.verdict is not Verdict.ALLOW
    assert d.decision.layer.name == "HARD_SAFETY"


def test_an_unknown_order_state_is_reconciled_not_retried() -> None:
    """**TEST 11.** The platform's oldest rule, asserted from this path.

    `SAFE_TO_RESEND` is `{failed}` and nothing else -- an IPC timeout after
    order_send looks exactly like a rejection from this side.
    """
    from app.oms.state import SAFE_TO_RESEND, OrderStatus

    assert SAFE_TO_RESEND == frozenset({OrderStatus.failed})
    assert OrderStatus.unknown not in SAFE_TO_RESEND


def test_two_accounts_with_the_same_strategy_name_stay_separate() -> None:
    """**TEST 12.** The decision carries its account and never pools."""
    a = run(rec(Horizon.SHORT_TERM, Verdict.ALLOW), account_id="acct-1")
    b = run(rec(Horizon.SHORT_TERM, Verdict.RESTRICT), account_id="acct-2")
    assert a.account_id == "acct-1" and b.account_id == "acct-2"
    assert a.verdict is Verdict.ALLOW and b.verdict is Verdict.RESTRICT


# ===================================== the properties the twelve rest on


def test_horizon_does_not_resolve_anything_itself() -> None:
    """**The no-second-engine rule, asserted rather than promised.**

    `Horizon` and `Layer` are orthogonal -- one is *when*, the other is *who
    says so* -- so they must compose. Two independent orderings resolving one
    conflict is the failure this repository keeps naming: the one that
    disagreed silently would be the one nobody read.
    """
    from app.portfolio import horizon

    tree = ast.parse(inspect.getsource(horizon))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "decide" in called, "resolution must be delegated to L60"

    source = inspect.getsource(horizon)
    for reimplementation in ("SEVERITY = ", "def decide(", "class RiskEngine"):
        assert reimplementation not in source, f"{reimplementation} is a second resolver"


@pytest.mark.parametrize("horizon", list(Horizon))
def test_every_horizon_maps_to_a_layer(horizon: Horizon) -> None:
    """A horizon with no layer could not enter `decide()` at all."""
    assert horizon in LAYER_OF
    assert horizon in DEFAULT_VALIDITY


def test_no_non_safety_horizon_carries_a_safety_layer() -> None:
    """The real property, and the first version of this test asserted a
    different one that was wrong.

    The obvious assertion is that `LAYER_OF` decreases monotonically with
    horizon -- longer horizon, weaker layer. It fails, and the failure is
    correct: MEDIUM_TERM maps to PORTFOLIO(5) while SHORT_TERM maps to
    STRATEGY(4), because medium-term concerns (allocation, concentration,
    correlation) genuinely ARE portfolio concerns and `Layer` names the KIND of
    concern rather than its urgency.

    It does not matter, because **layer does not decide the outcome**.
    `decide()` takes the most restrictive VERDICT and uses layer only to break
    a tie -- so a medium-term RESTRICT beats a short-term ALLOW on severity,
    which is exactly what section 44's TEST 4 asks for, and PORTFOLIO vs
    STRATEGY only chooses which reason gets named when both say the same thing.

    What must hold is narrower and is the actual safety property: the two
    safety horizons own the two safety layers, and nothing else may borrow one.
    """
    safety_layers = {LAYER_OF[Horizon.EMERGENCY], LAYER_OF[Horizon.IMMEDIATE_RISK]}
    assert safety_layers == {Layer.HARD_SAFETY, Layer.RISK}

    for horizon in Horizon:
        if horizon.is_safety:
            continue
        assert LAYER_OF[horizon] not in safety_layers, (
            f"{horizon.name} is not a safety horizon but carries a safety layer"
        )

    # And the strategic horizon is the weakest of all, so a long-term view
    # cannot outrank anything by being long-term.
    assert LAYER_OF[Horizon.LONG_TERM] == min(LAYER_OF.values())


def test_hysteresis_never_delays_a_safety_horizon() -> None:
    """**Section 16's last line**, and the L61 lesson: a guard that suppressed
    the response to the thing it guards against would disable what it exists
    to protect."""
    for horizon in (Horizon.EMERGENCY, Horizon.IMMEDIATE_RISK):
        act, why = worth_acting_on(rec(horizon, Verdict.PAUSE, magnitude=Decimal("0.0001")))
        assert act, f"{horizon.name} was smoothed"
        assert "never smoothed" in why


def test_hysteresis_stops_noise_moving_an_allocation() -> None:
    small = worth_acting_on(rec(Horizon.MEDIUM_TERM, Verdict.REDUCE, magnitude=Decimal("0.001")))
    assert not small[0]
    assert "arguing with itself" in small[1]

    real = worth_acting_on(rec(Horizon.MEDIUM_TERM, Verdict.REDUCE, magnitude=Decimal("0.10")))
    assert real[0]


def test_a_safety_horizon_is_never_parked_in_the_deferred_queue() -> None:
    """A deferral is a wait. Safety does not wait."""
    d = run(
        rec(Horizon.EMERGENCY, Verdict.REJECT, "kill switch"),
        rec(Horizon.IMMEDIATE_RISK, Verdict.RESTRICT, "exposure"),
        rec(Horizon.LONG_TERM, Verdict.ALLOW, "strategic"),
    )
    parked = {x.recommendation.horizon for x in d.deferred}
    assert Horizon.EMERGENCY not in parked
    assert Horizon.IMMEDIATE_RISK not in parked
    assert Horizon.LONG_TERM in parked


def test_an_emergency_is_never_dropped_for_being_stale() -> None:
    """A stale emergency is a reason to look, not a reason to discard."""
    d = run(
        rec(Horizon.EMERGENCY, Verdict.REJECT, "old kill switch", at=NOW - timedelta(days=99)),
    )
    assert d.expired == ()
    assert d.verdict is Verdict.REJECT


def test_synthesis_is_deterministic() -> None:
    """Section 11. Identical inputs, identical result."""
    args = (
        rec(Horizon.SHORT_TERM, Verdict.ALLOW),
        rec(Horizon.MEDIUM_TERM, Verdict.REDUCE),
        rec(Horizon.LONG_TERM, Verdict.ALLOW),
    )
    a, b = run(*args), run(*args)
    assert a.as_dict()["final"] == b.as_dict()["final"]
    assert a.explain() == b.explain()


def test_confidence_is_per_component_and_absent_is_absent() -> None:
    """Section 12: no single opaque score, and UNKNOWN is not high."""
    d = run(
        rec(
            Horizon.SHORT_TERM,
            Verdict.ALLOW,
            confidence={"data": Decimal("0.9"), "model": Decimal("0.4")},
        ),
        rec(Horizon.LONG_TERM, Verdict.ALLOW),
    )
    assert d.confidence == {
        "short_term.data": Decimal("0.9"),
        "short_term.model": Decimal("0.4"),
    }
    assert not any(k.startswith("long_term.") for k in d.confidence)


def test_conflicts_are_only_reported_between_genuine_disagreements() -> None:
    agree = run(
        rec(Horizon.SHORT_TERM, Verdict.ALLOW),
        rec(Horizon.LONG_TERM, Verdict.ALLOW),
    )
    assert agree.conflicts == ()

    different_targets = run(
        rec(Horizon.SHORT_TERM, Verdict.ALLOW, target="alloc"),
        rec(Horizon.LONG_TERM, Verdict.REDUCE, target="research"),
    )
    assert different_targets.conflicts == ()

    real = run(
        rec(Horizon.SHORT_TERM, Verdict.ALLOW, target="alloc"),
        rec(Horizon.LONG_TERM, Verdict.REDUCE, target="alloc"),
    )
    assert len(real.conflicts) == 1


def test_the_decision_explains_itself() -> None:
    """Section 34. A person has to be able to read why this won."""
    d = run(
        rec(Horizon.SHORT_TERM, Verdict.ALLOW),
        rec(Horizon.MEDIUM_TERM, Verdict.REDUCE),
        rec(Horizon.LONG_TERM, Verdict.ALLOW),
    )
    text = d.explain()
    assert "MEDIUM_TERM wins" in text
    assert "REDUCE" in text
    assert "Deferred:" in text


def test_the_decision_says_it_applied_nothing() -> None:
    record = run(rec(Horizon.SHORT_TERM, Verdict.ALLOW)).as_dict()
    assert "RiskEngine remains the final veto" in record["authority"]
    assert "nothing here reaches either" in record["authority"]


def test_the_module_reaches_nothing_that_trades() -> None:
    from app.portfolio import horizon

    tree = ast.parse(inspect.getsource(horizon))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"place_order", "close_position", "cancel_order", "modify_order"})


def test_the_existing_regime_vocabulary_is_reused_not_replaced() -> None:
    """Section 18: use the existing regime vocabulary if already defined.

    It is (`app/ai/regime.py`, with TRENDING_UP / TRENDING_DOWN / RANGING /
    HIGH_VOLATILITY / LOW_VOLATILITY / UNKNOWN), so this module defines no
    regime enum of its own -- the brief's TREND / MEAN_REVERSION / RISK_ON
    would have been a second vocabulary for one concept.
    """
    from app.ai.regime import Regime
    from app.portfolio import horizon

    assert Regime.unknown.value == "UNKNOWN"
    assert "class Regime" not in inspect.getsource(horizon)
