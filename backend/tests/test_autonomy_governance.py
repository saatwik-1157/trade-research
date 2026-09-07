"""Certification lifecycle, autonomy levels and change impact. **L63 §35.**

Section 35 lists fifteen mandatory safety tests. They are the first section
here, in order, with the number in each docstring so the brief can be walked
against the file.

The two that carry the most weight are TEST 12 and TEST 9. TEST 12 is the hole
L63 found and closed -- before `GOVERNANCE_TARGETS`, an action aimed at
`autonomy_level` came back REQUIRES_APPROVAL, which is a door where section 15
requires a wall. TEST 9 is the rule that is easiest to write wrong: a condition
clearing is not evidence that the thing it broke now works.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.portfolio.control import Effect
from app.safety.autonomy import (
    GOVERNANCE_TARGETS,
    AutonomyLevel,
    ceiling_for,
    downgrade_for,
    next_restoration_step,
)
from app.safety.certification import CertificationState
from app.safety.impact import Impact, analyse, trigger_impact
from app.safety.lifecycle import (
    AssuranceStatus,
    LifecycleState,
    assess,
    check_transition,
)
from app.safety.verification import ActionProposal, PolicyVerificationEngine

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
CERTIFIED_AT = NOW - timedelta(days=1)


def action(**kw: object) -> ActionProposal:
    base: dict[str, object] = dict(
        kind="reduce_strategy_allocation",
        target="strategy:sma_cross:allocation",
        effect=Effect.TIGHTENS,
        account_id="acct-1",
        allocation_change=Decimal("-0.05"),
        confidence=Decimal("1"),
    )
    base.update(kw)
    return ActionProposal(**base)  # type: ignore[arg-type]


# =============================================== section 35: the mandatory fifteen


def test_a_suspended_certification_applies_nothing() -> None:
    """**TEST 1.** SUSPENDED, action proposed → no auto-apply."""
    level = ceiling_for("SUSPENDED")
    assert level is AutonomyLevel.RECOMMEND
    r = PolicyVerificationEngine(autonomy=level).verify(action(), now=NOW)
    assert not r.may_auto_apply
    assert "nothing below BOUNDED" in " ".join(v.detail for v in r.violations)


def test_a_revoked_certification_leaves_only_observation() -> None:
    """**TEST 2.** REVOKED → observe/recommend only."""
    assert ceiling_for("CERTIFICATION_REVOKED") is AutonomyLevel.OBSERVE
    r = PolicyVerificationEngine(autonomy=AutonomyLevel.OBSERVE).verify(action(), now=NOW)
    assert not r.may_auto_apply


def test_a_critical_invariant_failure_revokes_and_suspends_at_once() -> None:
    """**TEST 3.** Not scored down. Replaced.

    A numerical result cannot survive an invariant being false, so the check
    runs before anything is weighed rather than as the largest term in a sum.
    """
    a = assess(
        base=CertificationState.CERTIFIED,
        now=NOW,
        certified_at=CERTIFIED_AT,
        evidence_at=NOW,
        dimensions={"risk": AssuranceStatus.HEALTHY},
        critical_invariant_failed=True,
    )
    assert a.state is LifecycleState.CERTIFICATION_REVOKED
    assert a.autonomy is AutonomyLevel.OBSERVE
    assert "revoked outright rather than scored down" in " ".join(a.reasons)

    d = downgrade_for(current=AutonomyLevel.CERTIFIED, critical_invariant_failed=True)
    assert d is not None and d.to is AutonomyLevel.OBSERVE


def test_an_expired_certification_downgrades_autonomy_by_itself() -> None:
    """**TEST 4.** Nobody has to notice. Time does it."""
    a = assess(
        base=CertificationState.CERTIFIED,
        now=CERTIFIED_AT + timedelta(days=31),
        certified_at=CERTIFIED_AT,
        evidence_at=CERTIFIED_AT + timedelta(days=31),
    )
    assert a.state is LifecycleState.CERTIFICATION_EXPIRED
    assert a.autonomy is AutonomyLevel.RECOMMEND
    assert not a.autonomy.may_auto_apply
    assert "not evidence about now" in " ".join(a.reasons)


def test_a_policy_change_runs_impact_analysis() -> None:
    """**TEST 5.**"""
    assert trigger_impact("POLICY_CHANGED") is Impact.FULL_REVALIDATION
    a = analyse(["backend/app/portfolio/control.py"])
    assert a.requires_revalidation
    assert "INV-05" in a.affected_invariants


def test_a_risk_engine_change_is_high_impact() -> None:
    """**TEST 6.** A diff to a path everything rests on suspends autonomy
    rather than being reasoned about."""
    a = analyse(["backend/app/risk/engine.py"])
    assert a.impact is Impact.SUSPEND_AUTONOMY
    assert trigger_impact("RISK_RULE_CHANGED") is Impact.SUSPEND_AUTONOMY

    for path in ("backend/app/oms/service.py", "backend/app/execution/pipeline.py"):
        assert analyse([path]).impact is Impact.SUSPEND_AUTONOMY, path


def test_an_ai_policy_recommendation_is_a_proposal_only() -> None:
    """**TEST 7.** It may propose. It may not deploy."""
    r = PolicyVerificationEngine().verify(
        action(kind="ai_proposes_policy_change", target="policy_version", effect=Effect.LOOSENS),
        now=NOW,
    )
    assert not r.may_auto_apply
    assert r.decision.value == "REJECT"


def test_an_ai_recommendation_to_remove_a_risk_restriction_is_refused() -> None:
    """**TEST 8.** It does not matter who proposed it."""
    r = PolicyVerificationEngine().verify(
        action(
            kind="ai_remove_drawdown_protection",
            target="max_drawdown_pct",
            effect=Effect.LOOSENS,
            touches_hard_limit=True,
        ),
        now=NOW,
    )
    assert r.decision.value == "REJECT"
    assert "never grant them" in " ".join(v.detail for v in r.violations)


def test_recovery_restores_one_step_at_a_time_and_never_jumps() -> None:
    """**TEST 9.** The rule that is easiest to write wrong.

    Section 16: do not restore maximum autonomy merely because the original
    error disappeared. The way that rule gets broken is a transition table
    where `SUSPENDED → CERTIFIED` exists at all, so it does not exist here.
    """
    assert check_transition(LifecycleState.SUSPENDED, LifecycleState.CERTIFIED) is not None
    assert check_transition(LifecycleState.DEGRADED, LifecycleState.CERTIFIED) is not None
    assert (
        check_transition(LifecycleState.CERTIFICATION_REVOKED, LifecycleState.CERTIFIED) is not None
    )
    refusal = check_transition(LifecycleState.SUSPENDED, LifecycleState.CERTIFIED)
    assert refusal is not None and "only through REVALIDATING" in refusal.reason

    # The one door back exists, and it is one step wide.
    assert check_transition(LifecycleState.SUSPENDED, LifecycleState.REVALIDATING) is None
    assert check_transition(LifecycleState.REVALIDATING, LifecycleState.CERTIFIED) is None

    # And restoration climbs a rung at a time, never two.
    step = next_restoration_step(AutonomyLevel.OBSERVE, ceiling=AutonomyLevel.CERTIFIED)
    assert step is not None and step[0] is AutonomyLevel.RECOMMEND


def test_restoration_cannot_climb_past_the_certified_ceiling() -> None:
    """Section 15: never allow automatic escalation above the certified level."""
    assert next_restoration_step(AutonomyLevel.RECOMMEND, ceiling=AutonomyLevel.RECOMMEND) is None
    step = next_restoration_step(AutonomyLevel.APPROVAL, ceiling=AutonomyLevel.BOUNDED)
    assert step is not None and step[0] is AutonomyLevel.BOUNDED
    assert next_restoration_step(AutonomyLevel.BOUNDED, ceiling=AutonomyLevel.BOUNDED) is None


def test_a_repeated_action_anomaly_restricts_the_loop() -> None:
    """**TEST 10.** Delegated to L61's ledger; asserted through the wrapper."""
    from app.portfolio.control import Action, ActionLedger

    ledger = ActionLedger()
    for i in range(6):
        ledger.record(
            Action(
                kind="x",
                target="strategy:sma_cross:allocation",
                effect=Effect.TIGHTENS if i % 2 == 0 else Effect.LOOSENS,
            ),
            at=NOW + timedelta(minutes=i),
        )
    r = PolicyVerificationEngine(ledger=ledger).verify(action(), now=NOW + timedelta(minutes=7))
    assert not r.may_auto_apply


def test_an_unknown_safety_state_fails_closed() -> None:
    """**TEST 11.** UNKNOWN is not HEALTHY, and blocks exactly as hard as
    CRITICAL. Section 7 says so; L53 is why."""
    assert AssuranceStatus.UNKNOWN.blocks_autonomy
    assert AssuranceStatus.CRITICAL.blocks_autonomy
    assert not AssuranceStatus.UNKNOWN.is_good

    a = assess(
        base=CertificationState.CERTIFIED,
        now=NOW,
        certified_at=CERTIFIED_AT,
        evidence_at=NOW,
        dimensions={"risk": AssuranceStatus.HEALTHY, "execution": AssuranceStatus.UNKNOWN},
    )
    assert a.state is LifecycleState.SUSPENDED
    assert not a.autonomy.may_auto_apply
    assert "not a dimension that is fine" in " ".join(a.reasons)

    d = downgrade_for(current=AutonomyLevel.CERTIFIED, safety_state_unknown=True)
    assert d is not None and d.to is AutonomyLevel.OBSERVE


@pytest.mark.parametrize("target", sorted(GOVERNANCE_TARGETS))
def test_the_system_cannot_raise_its_own_autonomy(target: str) -> None:
    """**TEST 12, and the hole L63 found.** Evidence for INV-27.

    Before `GOVERNANCE_TARGETS`, an action aimed at `autonomy_level` or
    `certification_state` classified as **REQUIRES_APPROVAL** -- an approval
    path by which the system could raise its own autonomy, extend its own
    certification, or switch off its own audit log. Section 15 requires a wall
    and REQUIRES_APPROVAL is a door.

    Fixed the way L62 fixed the same shape for the safety envelope: by putting
    the governance machinery INSIDE the set L61 already refuses outright,
    rather than by adding a new rule that could be forgotten.

    Parameterised over every governance target, so a target added to the set
    later is covered without anybody remembering to add a test.
    """
    r = PolicyVerificationEngine().verify(
        action(kind="tune", target=target, effect=Effect.LOOSENS, touches_hard_limit=False),
        now=NOW,
    )
    assert r.decision.value == "REJECT", f"{target} was not forbidden"
    assert not r.may_auto_apply
    assert "never grant them" in " ".join(v.detail for v in r.violations)


def test_nothing_below_bounded_is_ever_auto_applied() -> None:
    """Evidence for INV-26. Asserted across every level, not one."""
    for level in AutonomyLevel:
        r = PolicyVerificationEngine(autonomy=level).verify(action(), now=NOW)
        assert r.may_auto_apply == (level >= AutonomyLevel.BOUNDED), level.name


def test_one_degraded_account_does_not_degrade_another() -> None:
    """**TEST 13.** Account isolation, through the fingerprint.

    Two accounts proposing the same change are two actions, so one being
    already-verified never marks the other as done.
    """
    e = PolicyVerificationEngine()
    first = e.verify(action(account_id="acct-1"), now=NOW)
    second = e.verify(action(account_id="acct-2"), now=NOW)
    assert first.may_auto_apply and second.may_auto_apply

    repeat = e.verify(action(account_id="acct-1"), now=NOW + timedelta(seconds=5))
    assert not repeat.may_auto_apply, "the same account's repeat is a duplicate"


def test_a_failing_regression_stops_a_policy_becoming_certified() -> None:
    """**TEST 14.** The gates are derived from invariant results, so a failing
    invariant cannot be certified around."""
    from app.safety.certification import certify

    cert = certify(now=NOW, failed=frozenset({"INV-05"}))
    assert cert.state.value == "CERTIFICATION_REVOKED"


def test_evidence_that_cannot_be_reconstructed_degrades_certification() -> None:
    """**TEST 15.** A certificate whose evidence is missing or stale rests on
    nothing, and that is a different fact from the certificate expiring."""
    missing = assess(
        base=CertificationState.CERTIFIED,
        now=NOW,
        certified_at=CERTIFIED_AT,
        evidence_at=None,
    )
    assert missing.state is LifecycleState.DEGRADED
    assert "Unknown is not healthy" in " ".join(missing.reasons)

    stale = assess(
        base=CertificationState.CERTIFIED,
        now=NOW,
        certified_at=CERTIFIED_AT,
        evidence_at=NOW - timedelta(days=3),
    )
    assert stale.state is LifecycleState.DEGRADED
    assert "gone stale" in " ".join(stale.reasons)


# ==================================================== properties of the model


def test_autonomy_is_a_ceiling_and_nothing_here_raises_it() -> None:
    """**The property that makes the escalation rule need no enforcement.**

    No function in `autonomy.py` returns a level above what it was given, so
    "never escalate automatically" is not a rule somebody has to remember -- it
    is not expressible. Checked across the whole cross-product rather than at a
    sample.
    """
    for current in AutonomyLevel:
        for kwargs in (
            {"critical_invariant_failed": True},
            {"safety_state_unknown": True},
            {"high_violations": 1},
            {"evidence_stale": True},
            {"medium_violations": 3},
            {},
        ):
            d = downgrade_for(current=current, **kwargs)
            if d is not None:
                assert d.to <= current, f"{kwargs} raised autonomy from {current.name}"


def test_an_unrecognised_certification_state_fails_closed() -> None:
    """A state nobody mapped is exactly where a permissive default is wrong."""
    assert ceiling_for("SOMETHING_ADDED_LATER") is AutonomyLevel.OBSERVE
    assert ceiling_for("") is AutonomyLevel.OBSERVE


def test_an_unrecognised_trigger_is_not_no_action() -> None:
    assert trigger_impact("A_TRIGGER_NOBODY_MAPPED") is Impact.FULL_REVALIDATION


def test_an_unrecognised_path_is_high_impact_not_none() -> None:
    """The branch it would be most tempting to soften the first time it flags
    something dull. A path nobody classified is a path nobody thought about."""
    a = analyse(["backend/app/marketdata/feed.py"])
    assert a.impact is Impact.FULL_REVALIDATION
    assert a.unrecognised == ("backend/app/marketdata/feed.py",)
    assert "never as none" in " ".join(a.reasons)


def test_documentation_and_frontend_changes_do_not_decertify() -> None:
    """The other direction: an analyser that flagged everything would be
    ignored within a week, which is its own failure mode."""
    a = analyse(["INTERFACE.md", "frontend/src/app/page.tsx", "backend/tests/test_risk.py"])
    assert a.impact is Impact.NONE
    assert not a.requires_revalidation


def test_the_safety_package_judges_its_own_changes_honestly() -> None:
    """`app/safety/` is itself a critical path, so changing the verifier
    suspends autonomy. A governance tool exempt from its own rule is the first
    place a hole appears."""
    assert analyse(["backend/app/safety/verification.py"]).impact is Impact.SUSPEND_AUTONOMY


def test_every_lifecycle_state_has_defined_transitions() -> None:
    """An unmapped state is refused rather than guessed at, but it should not
    arise: the table is asserted total."""
    for state in LifecycleState:
        assert state in __import__("app.safety.lifecycle", fromlist=["TRANSITIONS"]).TRANSITIONS, (
            state
        )


def test_no_state_reaches_certified_except_through_revalidating() -> None:
    """**The single most important edge in the table**, asserted over all of
    it rather than at the three states that happen to be tested above."""
    from app.safety.lifecycle import TRANSITIONS

    for frm, tos in TRANSITIONS.items():
        if LifecycleState.CERTIFIED in tos:
            assert frm is LifecycleState.REVALIDATING, (
                f"{frm} can reach CERTIFIED without revalidating"
            )


#: The governance targets that MUST be protected, written out rather than read
#: from `GOVERNANCE_TARGETS`.
#:
#: This list exists because the parameterised test above cannot catch its own
#: parameter source shrinking: deleting `autonomy_level` from
#: `GOVERNANCE_TARGETS` also deletes the case that would have caught it, and the
#: suite stayed green. That is the self-referential guard this repository has
#: now hit four times -- a test that reads the thing it is checking proves only
#: that the thing agrees with itself.
#:
#: Section 23 names audit logging, monitoring and approvals explicitly; the
#: other three are the ones whose loss would let the system re-grant itself
#: authority.
MUST_BE_PROTECTED = (
    "autonomy_level",
    "certification_state",
    "certification_expiry",
    "audit_logging",
    "monitoring",
    "approval_required",
)


@pytest.mark.parametrize("target", MUST_BE_PROTECTED)
def test_the_targets_that_must_never_be_droppable_are_protected(target: str) -> None:
    """Independent of `GOVERNANCE_TARGETS`, deliberately."""
    assert target in GOVERNANCE_TARGETS, f"{target} was dropped from the protected set"
    r = PolicyVerificationEngine().verify(
        action(kind="tune", target=target, effect=Effect.LOOSENS, touches_hard_limit=False),
        now=NOW,
    )
    assert r.decision.value == "REJECT", f"{target} is no longer forbidden"
