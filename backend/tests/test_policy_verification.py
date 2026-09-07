"""The fifteen mandatory safety tests, and the engine under them. **L62 §28.**

Section 28 lists tests A through O and calls them mandatory. They are the first
section here, in order, named for what they check rather than for their letter,
with the letter in the docstring so the brief can be walked against the file.

Three of them (L, M, N) could not be written as section 28 describes, and each
says so in its own docstring rather than being quietly dropped or quietly
weakened into something that passes. A mandatory test that was made to pass by
lowering what it asserts is worse than one that is openly absent.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.portfolio.control import Effect
from app.portfolio.decision import DecisionContext, Input, Layer, Verdict
from app.safety.envelope import DEFAULT_ENVELOPE, SafetyEnvelope
from app.safety.verification import (
    ActionProposal,
    PolicyVerificationEngine,
    VerificationDecision,
    fingerprint,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
ACCOUNT = "acct-1"


def engine(**kw: object) -> PolicyVerificationEngine:
    return PolicyVerificationEngine(**kw)  # type: ignore[arg-type]


def reduce_allocation(**kw: object) -> ActionProposal:
    """The ordinary, permitted action. Every test below is a departure from it.

    It states `confidence=1`, and has to. A mechanical reduction is not a
    prediction, but under the rule the property test below forced, an action
    that states NO confidence has not cleared the confidence bar -- leaving the
    field out is not a way to skip a check.
    """
    base: dict[str, object] = dict(
        kind="reduce_strategy_allocation",
        target="strategy:sma_cross:allocation",
        effect=Effect.TIGHTENS,
        account_id=ACCOUNT,
        allocation_change=Decimal("-0.05"),
        confidence=Decimal("1"),
    )
    base.update(kw)
    return ActionProposal(**base)  # type: ignore[arg-type]


# ===================================== section 28: the mandatory fifteen


def test_a_proposal_over_the_approved_budget_is_refused() -> None:
    """**TEST A.** Approved budget $10,000; the optimizer proposes $15,000."""
    e = engine(envelope=SafetyEnvelope(max_capital_movement=Decimal("10000")))
    r = e.verify(
        reduce_allocation(kind="reallocate", capital_movement=Decimal("15000")),
        now=NOW,
    )
    assert not r.may_auto_apply
    assert any("max_capital_movement" in v.detail for v in r.violations)
    assert "15000" in " ".join(v.detail for v in r.violations)


def test_a_risk_veto_is_not_retried_around() -> None:
    """**TEST B.** The RiskEngine refused; control proposes a retry.

    The veto is CARRIED into the verification rather than re-derived from it.
    A verifier that recomputed risk would be a second risk engine, and the day
    the two disagreed the trade would go to whichever one the code consulted
    last.
    """
    r = engine().verify(
        reduce_allocation(effect=Effect.LOOSENS, retries=1),
        now=NOW,
        risk_veto="daily loss limit breached",
    )
    assert r.decision is VerificationDecision.REJECT
    assert any(v.invariant_id == "INV-01" for v in r.violations)
    assert "cannot overturn it" in " ".join(v.detail for v in r.violations)


def test_an_ai_recommendation_to_raise_leverage_is_refused() -> None:
    """**TEST C.** It does not matter who proposed it."""
    r = engine().verify(
        ActionProposal(
            kind="ai_recommended_leverage_increase",
            target="max_leverage",
            effect=Effect.LOOSENS,
            account_id=ACCOUNT,
            touches_hard_limit=True,
        ),
        now=NOW,
    )
    assert r.decision is VerificationDecision.REJECT
    assert "never grant them" in " ".join(v.detail for v in r.violations)


def test_nothing_is_auto_applied_in_safe_mode() -> None:
    """**TEST D.** Safe mode is a latched refusal above everything else."""
    r = engine().verify(reduce_allocation(), now=NOW, safe_mode=True)
    assert r.decision is VerificationDecision.SAFE_MODE
    assert not r.may_auto_apply
    assert "released by a person" in " ".join(v.detail for v in r.violations)


def test_an_unknown_venue_state_is_reconciled_before_anything_moves() -> None:
    """**TEST E.** An IPC timeout after order_send looks exactly like a
    rejection from this side, so unknown is settled by asking the venue."""
    r = engine().verify(reduce_allocation(), now=NOW, unreconciled_order=True)
    assert r.decision is VerificationDecision.REJECT
    assert any(v.invariant_id == "INV-10" for v in r.violations)


def test_stale_evidence_defers_rather_than_permits() -> None:
    """**TEST F.** Missing is never read as permission.

    Checked twice on purpose: the envelope catches the age, and the L60
    decision context independently turns the missing input into a HARD_SAFETY
    finding. Either alone would refuse; both is what makes it not depend on
    which one a caller remembered to populate.
    """
    ctx = DecisionContext(now=NOW)
    ctx.add_input(Input(name="mark_price", source="marketdata", value=None))
    ctx.require_fresh("mark_price")

    r = engine().verify(
        reduce_allocation(effect=Effect.LOOSENS, data_age=timedelta(hours=3)),
        now=NOW,
        context=ctx,
    )
    assert not r.may_auto_apply
    assert any("max_data_age" in v.detail for v in r.violations)
    assert any(v.invariant_id == "INV-16" for v in r.violations)


def test_a_quarantined_strategy_gets_no_new_exposure() -> None:
    """**TEST G.** New allocation to a quarantined strategy is refused."""
    r = engine().verify(
        reduce_allocation(kind="increase_allocation", effect=Effect.LOOSENS),
        now=NOW,
        quarantined=True,
    )
    assert r.decision is VerificationDecision.REJECT
    assert any(v.invariant_id == "INV-13" for v in r.violations)


def test_quarantine_still_allows_reducing_what_is_already_open() -> None:
    """The other half of G, and the half that makes it a safety control
    rather than a trap. A restriction that also blocked exits would be a way
    of being unable to get out."""
    r = engine().verify(reduce_allocation(), now=NOW, quarantined=True)
    assert r.may_auto_apply


def test_the_same_proposal_twice_is_one_action() -> None:
    """**TEST H.** Idempotency. Evidence for INV-21."""
    e = engine()
    first = e.verify(reduce_allocation(), now=NOW)
    assert first.may_auto_apply

    # A different proposal id, the same action, under the same rules.
    second = e.verify(reduce_allocation(), now=NOW + timedelta(seconds=30))
    assert second.decision is VerificationDecision.REJECT
    assert any(v.invariant_id == "INV-21" for v in second.violations)
    assert first.proposal_id in " ".join(v.detail for v in second.violations)


def test_a_rejected_proposal_is_not_remembered_as_done() -> None:
    """The failure mode the idempotency cache would otherwise introduce: a
    refusal caused by a passing condition must not become permanent."""
    e = engine()
    blocked = e.verify(reduce_allocation(), now=NOW, safe_mode=True)
    assert not blocked.passed

    allowed = e.verify(reduce_allocation(), now=NOW + timedelta(minutes=1))
    assert allowed.may_auto_apply, "a proposal refused under safe mode must be re-proposable"


def test_a_thrashing_target_stops_being_auto_applied() -> None:
    """**TEST I.** Cooldown / circuit breaker, delegated to L61's ledger."""
    from app.portfolio.control import Action, ActionLedger

    ledger = ActionLedger()
    for i in range(6):
        effect = Effect.TIGHTENS if i % 2 == 0 else Effect.LOOSENS
        ledger.record(
            Action(kind="x", target="strategy:sma_cross:allocation", effect=effect),
            at=NOW + timedelta(minutes=i),
        )
    r = engine(ledger=ledger).verify(reduce_allocation(), now=NOW + timedelta(minutes=7))
    assert r.decision is VerificationDecision.REQUIRES_APPROVAL
    assert "arguing with itself" in " ".join(v.detail for v in r.violations)


def test_an_absent_advisory_input_cannot_make_a_decision_more_permissive() -> None:
    """**TEST J.** AI unavailable, deterministic fallback.

    Asserted as a PROPERTY over the confidence range rather than as one
    scenario: for every confidence an advisory input could carry, the outcome
    with it absent is never more permissive than the outcome with it present.
    An absent model removes a veto; it can never add an approval.
    """
    for value in (None, Decimal("0.0"), Decimal("0.5"), Decimal("0.9"), Decimal("1.0")):
        with_ai = engine().verify(reduce_allocation(confidence=value), now=NOW)
        without = engine().verify(reduce_allocation(confidence=None), now=NOW)
        assert not (without.may_auto_apply and not with_ai.may_auto_apply), (
            f"an absent advisory input was more permissive than confidence={value}"
        )


def test_stating_no_confidence_is_not_better_than_stating_none() -> None:
    """**The defect the property in TEST J found.** L62 regression.

    `within_envelope` checks only what it is given, so `confidence=0.0` was
    refused for being under the bar while `confidence=None` sailed past it
    unchecked. An action with no confidence behind it was treated as more
    trustworthy than one that honestly reported having none.

    That is the L53 defect wearing different clothes: `PortfolioState.
    open_symbols` defaulted to an empty frozenset, so "nobody told me what is
    open" was indistinguishable from "nothing is open", and the one aggregate
    risk control enabled by default could not fire. Absent is not zero, and
    absent is never permission.

    Worth recording how it was found: a single-scenario test would have used
    one confidence value and passed. It took asserting the property across the
    whole range -- absent is never more permissive than present -- to see it.
    """
    absent = engine().verify(reduce_allocation(confidence=None), now=NOW)
    zero = engine().verify(reduce_allocation(confidence=Decimal("0")), now=NOW)

    assert not absent.may_auto_apply
    assert not zero.may_auto_apply
    assert any("Absent evidence is not permission" in v.detail for v in absent.violations)


def test_an_action_aimed_at_the_envelope_itself_is_forbidden() -> None:
    """**TEST K.** A policy trying to change a hard risk limit.

    The envelope's own fields are hard limits, so proposing to change one is
    proposing to loosen a hard constraint -- which L61 already forbids with no
    approval path. No new mechanism; the envelope simply had to be inside the
    thing that was already protected.
    """
    for target in ("max_capital_movement", "max_actions_per_window", "safety_envelope"):
        r = engine().verify(
            ActionProposal(
                kind="tune_limits",
                target=target,
                effect=Effect.LOOSENS,
                account_id=ACCOUNT,
                touches_hard_limit=False,  # the proposal claims it is ordinary
            ),
            now=NOW,
        )
        assert r.decision is VerificationDecision.REJECT, target
        assert "never grant them" in " ".join(v.detail for v in r.violations)


def test_an_approval_required_action_is_never_auto_applied() -> None:
    """**TEST L, in the part that can be tested here.** Evidence for INV-14.

    Section 28's TEST L is "frontend attempts unauthorized policy mutation ->
    backend rejects". That is an HTTP/RBAC assertion and it already exists, in
    `tests/test_security.py` -- `test_a_plain_user_cannot_change_risk_
    configuration` and the route-level guards around it. It is NOT re-tested
    here, because a second weaker version of an authorization test is how the
    weaker one ends up being the one that gets maintained.

    What belongs here is the half that is this module's own: a proposal that
    needs approval must never come back auto-appliable, no matter how it got
    that way.
    """
    causes = (
        reduce_allocation(allocation_change=Decimal("-0.99")),  # out of envelope
        reduce_allocation(reversible=False, allocation_change=Decimal("-0.02")),
        reduce_allocation(kind="raise", effect=Effect.LOOSENS),
    )
    for proposal in causes:
        r = engine().verify(proposal, now=NOW)
        if r.decision is VerificationDecision.REQUIRES_APPROVAL:
            assert r.approval_required
            assert not r.may_auto_apply, f"{proposal.kind} was auto-appliable"


def test_a_decision_is_verified_against_exactly_one_policy_version() -> None:
    """**TEST M, reduced to what is real.**

    Section 28 asks what happens when the policy version changes mid-decision.
    **There is no stored policy here to change** -- `app.portfolio.control` and
    `app.portfolio.decision` are pure functions over module constants, which is
    why INV-25 is NOT_APPLICABLE and GATE-10 is only partially applicable.

    What CAN be asserted, and is, is the property a version exists to give: one
    verification is stamped with exactly one version, that version is derived
    from the rules actually applied, and a change to the envelope produces a
    different one. A decision can therefore be replayed against the rules that
    made it, which is the point of the field.
    """
    tight = engine(envelope=SafetyEnvelope(max_capital_movement=Decimal("100")))
    loose = engine(envelope=SafetyEnvelope(max_capital_movement=Decimal("100000")))

    a = tight.verify(reduce_allocation(), now=NOW)
    b = loose.verify(reduce_allocation(), now=NOW)
    assert a.policy_version != b.policy_version, "different rules must version differently"

    again = engine(envelope=SafetyEnvelope(max_capital_movement=Decimal("100"))).verify(
        reduce_allocation(), now=NOW
    )
    assert again.policy_version == a.policy_version, "the same rules must version identically"


def test_the_fingerprint_separates_accounts_and_rule_sets() -> None:
    """**TEST N, in the part that is real here.** Evidence for INV-21.

    Section 28's TEST N is "historical replay attempts to access future data ->
    fail closed". That control exists and predates this level: it is
    `app.datasets.leakage`, bound as INV-22's evidence, and it is not
    duplicated here.

    The reproducibility property this module owns is narrower: an action's
    identity must include the account and the rule set, so that "we already did
    this" can never be true across a boundary where it is not.
    """
    base = reduce_allocation()
    other_account = reduce_allocation(account_id="acct-2")
    assert fingerprint(base, policy_version="v1") != fingerprint(other_account, policy_version="v1")
    assert fingerprint(base, policy_version="v1") != fingerprint(base, policy_version="v2")
    # And the id must NOT matter, or nothing would ever be a duplicate.
    assert fingerprint(base, policy_version="v1") == fingerprint(
        reduce_allocation(), policy_version="v1"
    )


def test_no_autonomous_action_touches_a_live_environment() -> None:
    """**TEST O.** Live execution is a governance decision, not an action."""
    r = engine().verify(reduce_allocation(environment="live"), now=NOW)
    assert r.decision is VerificationDecision.REJECT
    assert any(v.invariant_id == "INV-07" for v in r.violations)


# =========================================== the engine's own properties


def test_every_refusal_reason_is_collected_not_just_the_first() -> None:
    """An operator reading a rejection wants every reason, not the one the
    code happened to reach first."""
    r = engine().verify(
        reduce_allocation(
            effect=Effect.LOOSENS, environment="live", capital_movement=Decimal("999999")
        ),
        now=NOW,
        safe_mode=True,
        risk_veto="limit breached",
        unreconciled_order=True,
    )
    offended = {v.invariant_id for v in r.violations}
    assert {"INV-19", "INV-10", "INV-01", "INV-07"} <= offended
    assert r.decision is VerificationDecision.SAFE_MODE, "the most restrictive outcome wins"


def test_the_most_restrictive_outcome_wins_regardless_of_check_order() -> None:
    r = engine().verify(
        reduce_allocation(effect=Effect.LOOSENS),  # would be REQUIRES_APPROVAL alone
        now=NOW,
        risk_veto="no",  # REJECT
    )
    assert r.decision is VerificationDecision.REJECT


def test_a_clean_reduction_inside_the_envelope_is_auto_appliable() -> None:
    """The gate has to permit what it is for, or it is not a gate."""
    r = engine().verify(reduce_allocation(), now=NOW)
    assert r.may_auto_apply
    assert r.passed and not r.approval_required and not r.violations


def test_the_result_says_it_applied_nothing() -> None:
    r = engine().verify(reduce_allocation(), now=NOW).as_dict()
    assert "not an application" in r["authority"]
    assert "RiskEngine remains the final veto" in r["authority"]


def test_the_verifier_delegates_rather_than_re_deciding() -> None:
    """**The no-second-policy-engine rule, asserted rather than promised.**

    L62 says not to build a second policy engine. The way that rule gets broken
    is never a file called `policy_engine_2.py` -- it is a verifier that starts
    re-deriving a classification the existing one already makes, because
    calling out felt awkward. So this checks the calls are actually there.
    """
    from app.safety import verification

    tree = ast.parse(inspect.getsource(verification))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "permitted" in called, "action classification must be delegated to L61"
    assert "decide" in called, "the layer hierarchy must be delegated to L60"
    assert "within_envelope" in called

    # And it must not have grown its own copy of what it delegates.
    source = inspect.getsource(verification)
    for reimplementation in ("def classify(", "def decide(", "class RiskEngine"):
        assert reimplementation not in source, f"{reimplementation} is a second implementation"


def test_the_envelope_agrees_with_the_decision_layers_freshness_limit() -> None:
    """Two separate numbers with the same value. If one moves, this catches
    the divergence rather than leaving two staleness rules in the codebase."""
    from app.portfolio.decision import DEFAULT_MAX_AGE

    assert DEFAULT_ENVELOPE.max_data_age == DEFAULT_MAX_AGE


@pytest.mark.parametrize("field_name", [f for f in SafetyEnvelope.__dataclass_fields__])
def test_the_envelope_is_frozen(field_name: str) -> None:
    """A control loop that could widen its own bounds would have none."""
    env = SafetyEnvelope()
    # `FrozenInstanceError` specifically: a blind `Exception` would also pass
    # if the attribute simply did not exist, which is not what is being tested.
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(env, field_name, None)


def test_the_verification_module_never_reaches_a_venue() -> None:
    path = pathlib.Path(inspect.getfile(__import__("app.safety.verification", fromlist=["x"])))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"place_order", "close_position", "cancel_order", "modify_order"})


def test_a_layer_below_risk_cannot_relax_it_through_the_verifier() -> None:
    """L60's hierarchy has to survive being wrapped.

    An OPTIMIZATION layer saying ALLOW alongside a RISK layer saying RESTRICT
    must not come out of the verifier as permission, or the wrapper would have
    reintroduced exactly what `decide()` was built to make unrepresentable.
    """
    from app.portfolio.decision import Finding

    ctx = DecisionContext(now=NOW)
    ctx.add_finding(Finding(Layer.OPTIMIZATION, Verdict.ALLOW, "the optimizer is confident"))
    ctx.add_finding(Finding(Layer.RISK, Verdict.RESTRICT, "exposure limit"))

    r = engine().verify(reduce_allocation(), now=NOW, context=ctx)
    assert not r.may_auto_apply
    assert r.evidence["decision_layer"] == "RISK"
