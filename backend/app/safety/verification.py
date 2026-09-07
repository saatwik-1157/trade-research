"""Verifying one proposed autonomous action. **L62 section 4.**

**This is not a second policy engine, and the distinction is the whole design.**
L62 says explicitly not to build one, and there are already two components that
decide things about an action:

* `app.portfolio.control` (L61) decides how much autonomy an action's DIRECTION
  earns, and detects a target being pushed back and forth.
* `app.portfolio.decision` (L60) decides how a conflict between layers
  resolves, and refuses to let a lower layer relax a higher one.

So this module delegates both and adds only what neither has: magnitude
(`envelope.py`), identity (`fingerprint`), and the record. If it re-derived a
classification either of those already makes, the two would eventually
disagree, and the one that disagreed silently would be the one nobody reads.
There is a syntax-tree test asserting that `classify` and `decide` are the
functions actually called.

**It applies nothing.** It returns a `PolicyVerificationResult`. The RiskEngine
remains the final veto and the OMS owns order state; nothing here reaches
either, and a test parses this module to keep that true.

**What it cannot verify, it says so.** The L62 brief describes a decision
engine, a control policy and a policy version that this repository does not
contain. `policy_version` here is a hash of the rules that were actually
applied -- the envelope and the two classifier modules -- rather than the id of
a stored policy, because there is no stored policy. A version number invented
to fill a field would be the fabricated evidence this level exists to prevent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.portfolio.control import Action, ActionClass, ActionLedger, Effect, permitted
from app.portfolio.decision import Decision, DecisionContext, Verdict
from app.safety.autonomy import GOVERNANCE_TARGETS, AutonomyLevel
from app.safety.envelope import (
    DEFAULT_ENVELOPE,
    ENVELOPE_TARGETS,
    EnvelopeBreach,
    SafetyEnvelope,
    within_envelope,
)

VERIFIER_VERSION = "L62.1"


class VerificationDecision(StrEnum):
    """L62 section 4. What may happen to this proposal."""

    PASS = "PASS"
    PASS_WITH_WARNING = "PASS_WITH_WARNING"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    REJECT = "REJECT"
    SAFE_MODE = "SAFE_MODE"


#: How restrictive each decision is. The engine takes the maximum, so a check
#: that would REJECT cannot be displaced by one that would PASS. Same
#: construction as `decision.SEVERITY`, and for the same reason.
SEVERITY: dict[VerificationDecision, int] = {
    VerificationDecision.PASS: 0,
    VerificationDecision.PASS_WITH_WARNING: 1,
    VerificationDecision.REQUIRES_APPROVAL: 2,
    VerificationDecision.REJECT: 3,
    VerificationDecision.SAFE_MODE: 4,
}


@dataclass(frozen=True)
class Violation:
    """One reason a proposal did not pass, and which invariant it offends."""

    invariant_id: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"invariant": self.invariant_id, "detail": self.detail}


@dataclass(frozen=True)
class ActionProposal:
    """A proposed autonomous action, with everything needed to judge it.

    The magnitudes are separate optional fields rather than one number because
    they are not interchangeable: moving 10% of an allocation and moving $1,000
    are different claims about different quantities, and an envelope that
    accepted either as "the size" would be comparing them against the wrong
    limit.
    """

    kind: str
    target: str
    effect: Effect
    account_id: str
    environment: str = "paper"
    touches_hard_limit: bool = False
    reversible: bool = True

    allocation_change: Decimal | None = None
    capital_movement: Decimal | None = None
    exposure_change: Decimal | None = None
    confidence: Decimal | None = None
    data_age: timedelta | None = None

    chain_length: int = 1
    retries: int = 0

    proposal_id: str = field(default_factory=lambda: str(uuid4()))

    def hard_limit(self) -> bool:
        """Whether this touches a hard constraint.

        An action aimed at the envelope OR at the governance machinery always
        does, whatever the caller claimed. A proposal gets to describe its own
        effect; it does not get to decide that the bounds it is judged against,
        or the record of whether it may act at all, are ordinary parameters.

        `GOVERNANCE_TARGETS` was added at L63 to close a real hole: before it,
        an action targeting `autonomy_level` or `certification_state`
        classified as REQUIRES_APPROVAL rather than FORBIDDEN -- an approval
        path by which the system could raise its own autonomy. A door, where
        section 15 requires a wall.
        """
        return (
            self.touches_hard_limit
            or self.target in ENVELOPE_TARGETS
            or self.target in GOVERNANCE_TARGETS
        )


def fingerprint(proposal: ActionProposal, *, policy_version: str) -> str:
    """Identity for idempotency: the same action, proposed again.

    Deliberately **excludes** the proposal id and the timestamp, which is the
    entire point -- two proposals with different ids are the same action if
    they would do the same thing to the same target on the same account under
    the same rules. Including the id would make every resubmission unique and
    the check useless.

    It **includes** the environment, the account and the policy version: the
    same change to a different account is a different action, and the same
    change under different rules has not actually been verified before.
    """
    parts = (
        proposal.kind,
        proposal.target,
        str(proposal.effect),
        proposal.account_id,
        proposal.environment,
        str(proposal.allocation_change),
        str(proposal.capital_movement),
        str(proposal.exposure_change),
        policy_version,
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class PolicyVerificationResult:
    """L62 section 4. What was checked, what it concluded, and on what."""

    verification_id: str
    proposal_id: str
    policy_version: str
    verified_at: datetime
    environment: str
    account_id: str
    passed: bool
    decision: VerificationDecision
    violations: tuple[Violation, ...]
    warnings: tuple[str, ...]
    checked_invariants: tuple[str, ...]
    risk_impact: str
    capital_impact: str
    approval_required: bool
    rollback_required: bool
    evidence: dict[str, Any]
    verifier_version: str = VERIFIER_VERSION

    @property
    def may_auto_apply(self) -> bool:
        """The only question a caller should ask before acting.

        A single property rather than something a caller assembles from
        `passed` and `decision`, because the two can be read apart and the
        reading that skips `approval_required` is the one that auto-applies an
        action somebody was meant to see.
        """
        return (
            self.decision in (VerificationDecision.PASS, VerificationDecision.PASS_WITH_WARNING)
            and not self.approval_required
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "proposal_id": self.proposal_id,
            "policy_version": self.policy_version,
            "verified_at": self.verified_at.isoformat(),
            "environment": self.environment,
            "account_id": self.account_id,
            "passed": self.passed,
            "decision": str(self.decision),
            "may_auto_apply": self.may_auto_apply,
            "violations": [v.as_dict() for v in self.violations],
            "warnings": list(self.warnings),
            "checked_invariants": list(self.checked_invariants),
            "risk_impact": self.risk_impact,
            "capital_impact": self.capital_impact,
            "approval_required": self.approval_required,
            "rollback_required": self.rollback_required,
            "evidence": self.evidence,
            "verifier_version": self.verifier_version,
            "authority": (
                "A verification, not an application. The RiskEngine remains the final "
                "veto and the OMS owns order state."
            ),
        }


class PolicyVerificationEngine:
    """Verifies proposals. Holds the envelope, the ledger and what it has seen.

    One instance per process. It is not a store: `seen` answers "have I already
    verified this action", and a restart legitimately resets that question --
    the durable backstop for orders is `orders.intent_id` UNIQUE, which is a
    database constraint and outlives everything here.
    """

    def __init__(
        self,
        *,
        envelope: SafetyEnvelope = DEFAULT_ENVELOPE,
        ledger: ActionLedger | None = None,
        autonomy: AutonomyLevel = AutonomyLevel.BOUNDED,
    ) -> None:
        self.envelope = envelope
        self.ledger = ledger if ledger is not None else ActionLedger()
        self.seen: dict[str, str] = {}
        # The default is BOUNDED rather than CERTIFIED: the platform is
        # CONDITIONALLY_CERTIFIED, and `ceiling_for` puts that at BOUNDED. A
        # default of CERTIFIED would be an engine that starts out claiming more
        # than the certification behind it.
        self.autonomy = autonomy

    def policy_version(self) -> str:
        """A hash of the rules actually applied, not the id of a stored policy.

        There is no stored policy in this repository to carry an id. What can
        honestly be versioned is the content of the rules: the envelope's
        values and the two classifier modules' source. A change to any of them
        produces a different version, which is what a policy version is FOR --
        so a decision can be replayed against the rules that made it.
        """
        import inspect

        # Imported by full dotted path rather than `from app.portfolio import
        # control, decision`, so the package-level import guard in
        # `test_safety_invariants.py` stays exact. Widening the allow-list to
        # `app.portfolio` would have licensed `app.portfolio.service` too.
        import app.portfolio.control as control
        import app.portfolio.decision as decision

        material = "\x1f".join(
            (
                repr(self.envelope),
                hashlib.sha256(inspect.getsource(control).encode()).hexdigest()[:16],
                hashlib.sha256(inspect.getsource(decision).encode()).hexdigest()[:16],
                VERIFIER_VERSION,
            )
        )
        return "pv-" + hashlib.sha256(material.encode()).hexdigest()[:16]

    def verify(
        self,
        proposal: ActionProposal,
        *,
        now: datetime,
        safe_mode: bool = False,
        risk_veto: str | None = None,
        quarantined: bool = False,
        unreconciled_order: bool = False,
        context: DecisionContext | None = None,
        actions_in_window: int = 0,
        concurrent_actions: int = 0,
    ) -> PolicyVerificationResult:
        """Check one proposal against everything that can refuse it.

        The checks are written in order of authority, every one of them runs,
        and the most restrictive outcome wins. Running them all rather than
        returning at the first refusal is deliberate: an operator reading a
        rejection wants every reason it was rejected, not the first one the
        code happened to reach.
        """
        version = self.policy_version()
        print_id = str(uuid4())
        violations: list[Violation] = []
        warnings: list[str] = []
        checked: list[str] = []
        outcomes: list[VerificationDecision] = [VerificationDecision.PASS]

        def refuse(inv: str, decision: VerificationDecision, why: str) -> None:
            violations.append(Violation(inv, why))
            outcomes.append(decision)

        # --- 1. Safe mode. A latched second refusal, above everything.
        checked.append("INV-19")
        if safe_mode:
            refuse(
                "INV-19",
                VerificationDecision.SAFE_MODE,
                "safe mode is engaged; no autonomous action is applied while it holds. "
                "It is released by a person re-running the startup sequence, never by "
                "an action arguing that it is safe",
            )

        # --- 2. Unknown venue state. Reconcile before anything else moves.
        checked.append("INV-10")
        if unreconciled_order:
            refuse(
                "INV-10",
                VerificationDecision.REJECT,
                "an order's venue state was never established. It is settled by asking "
                "the venue, never by acting around it: an IPC timeout after order_send "
                "looks exactly like a rejection from this side",
            )

        # --- 3. The RiskEngine's veto. Not re-derived here, carried.
        checked.append("INV-01")
        if risk_veto is not None:
            refuse(
                "INV-01",
                VerificationDecision.REJECT,
                f"the RiskEngine refused: {risk_veto}. Risk is not re-evaluated here and "
                "a verification cannot overturn it",
            )

        # --- 4. Direction and loop protection. DELEGATED to L61 entirely.
        checked.extend(("INV-05", "INV-06", "INV-07", "INV-15", "INV-18"))
        action = Action(
            kind=proposal.kind,
            target=proposal.target,
            effect=proposal.effect,
            touches_hard_limit=proposal.hard_limit(),
            within_bounds=not within_envelope(
                envelope=self.envelope,
                allocation_change=proposal.allocation_change,
                capital_movement=proposal.capital_movement,
                exposure_change=proposal.exposure_change,
            ),
            reversible=proposal.reversible,
        )
        klass, reason = permitted(action, self.ledger, now=now)
        if klass is ActionClass.FORBIDDEN:
            refuse("INV-05", VerificationDecision.REJECT, reason)
        elif klass is ActionClass.REQUIRES_APPROVAL:
            refuse("INV-14", VerificationDecision.REQUIRES_APPROVAL, reason)
        elif klass in (ActionClass.OBSERVE_ONLY, ActionClass.RECOMMEND):
            warnings.append(reason)

        # --- 5. Magnitude, frequency, freshness, confidence.
        checked.append("INV-17")
        breaches: tuple[EnvelopeBreach, ...] = within_envelope(
            envelope=self.envelope,
            allocation_change=proposal.allocation_change,
            capital_movement=proposal.capital_movement,
            exposure_change=proposal.exposure_change,
            confidence=proposal.confidence,
            data_age=proposal.data_age,
            actions_in_window=actions_in_window,
            concurrent_actions=concurrent_actions,
            chain_length=proposal.chain_length,
            retries=proposal.retries,
        )
        for breach in breaches:
            # Out of the envelope is not forbidden -- it is out of the range
            # somebody sized, which is a person's decision rather than a ban.
            refuse("INV-17", VerificationDecision.REQUIRES_APPROVAL, str(breach))

        # A stated confidence of 0.0 was refused while an ABSENT one passed,
        # because `within_envelope` only checks what it is given. That is the
        # L53 defect exactly -- `open_symbols` defaulted to an empty frozenset,
        # so "nobody told me what is open" was indistinguishable from "nothing
        # is open" -- and it was found by asserting the property across the
        # whole confidence range rather than by testing one value.
        #
        # An action that states no confidence has not cleared the bar. A
        # deterministic action states 1.0 and says so; it does not get to skip
        # the check by leaving the field out.
        if self.envelope.min_confidence > 0 and proposal.confidence is None:
            refuse(
                "INV-11",
                VerificationDecision.REQUIRES_APPROVAL,
                f"no confidence was stated, and the envelope requires at least "
                f"{self.envelope.min_confidence}. Absent evidence is not permission: an "
                "action with no confidence behind it is not more trustworthy than one "
                "that reported none",
            )

        # --- 6. Stale evidence. Missing is never permission.
        checked.append("INV-11")
        decision_result: Decision | None = None
        if context is not None:
            from app.portfolio.decision import decide

            decision_result = decide(context)
            if decision_result.verdict is not Verdict.ALLOW:
                mapped = (
                    VerificationDecision.REJECT
                    if decision_result.verdict in (Verdict.REJECT, Verdict.PAUSE)
                    else VerificationDecision.REQUIRES_APPROVAL
                )
                refuse(
                    "INV-16",
                    mapped,
                    f"{decision_result.layer.name} returned {decision_result.verdict}: "
                    f"{decision_result.reason}",
                )
                checked.append("INV-16")

        # --- 7. Quarantine restricts new exposure, never management.
        checked.append("INV-13")
        if quarantined and proposal.effect is Effect.LOOSENS:
            refuse(
                "INV-13",
                VerificationDecision.REJECT,
                "the strategy is quarantined, so no action may add exposure to it. "
                "Reducing or closing what is already open stays available: a "
                "restriction that also blocked exits would be a way of being unable "
                "to get out",
            )

        # --- 8. Environment. Nothing autonomous touches live.
        checked.append("INV-07")
        if proposal.environment == "live":
            refuse(
                "INV-07",
                VerificationDecision.REJECT,
                "no autonomous action is applied to a live environment. Live execution "
                "is a governance decision a person takes through the production "
                "process, with its own gates",
            )

        # --- 9. Identity. The same action, proposed twice.
        checked.append("INV-21")
        print_hash = fingerprint(proposal, policy_version=version)
        duplicate = self.seen.get(print_hash)
        if duplicate is not None and duplicate != proposal.proposal_id:
            refuse(
                "INV-21",
                VerificationDecision.REJECT,
                f"this is the same action as proposal {duplicate}, already verified "
                "under the same rules for the same account. Resubmission is not a "
                "second decision",
            )

        # --- 10. The autonomy ceiling. Applied LAST and one-directional.
        #
        # Below BOUNDED nothing is auto-applied, whatever every other check
        # concluded. This is the gate that makes a suspended or revoked
        # certification mean something at the moment of action rather than only
        # in a report -- L63 section 15's "never allow automatic escalation
        # above the currently certified level", enforced where the action is
        # rather than where the certificate is.
        checked.append("INV-26")
        if not self.autonomy.may_auto_apply:
            refuse(
                "INV-26",
                VerificationDecision.REQUIRES_APPROVAL,
                f"the platform is at autonomy {self.autonomy.name} and nothing below "
                "BOUNDED is applied without a person. Autonomy is restored by staged "
                "revalidation, never by the condition that lowered it going away",
            )

        decision_out = max(outcomes, key=lambda d: SEVERITY[d])
        passed = decision_out in (
            VerificationDecision.PASS,
            VerificationDecision.PASS_WITH_WARNING,
        )
        if passed and warnings and decision_out is VerificationDecision.PASS:
            decision_out = VerificationDecision.PASS_WITH_WARNING

        # Only a proposal that actually cleared is remembered. Recording a
        # rejected one would make its rejection permanent even after the
        # condition that caused it cleared.
        if passed:
            self.seen.setdefault(print_hash, proposal.proposal_id)

        return PolicyVerificationResult(
            verification_id=print_id,
            proposal_id=proposal.proposal_id,
            policy_version=version,
            verified_at=now,
            environment=proposal.environment,
            account_id=proposal.account_id,
            passed=passed,
            decision=decision_out,
            violations=tuple(violations),
            warnings=tuple(warnings),
            checked_invariants=tuple(dict.fromkeys(checked)),
            risk_impact=(
                str(proposal.exposure_change) if proposal.exposure_change else "none stated"
            ),
            capital_impact=str(proposal.capital_movement)
            if proposal.capital_movement
            else "none stated",
            approval_required=decision_out is VerificationDecision.REQUIRES_APPROVAL,
            rollback_required=False,
            evidence={
                "autonomy_level": self.autonomy.name,
                "action_fingerprint": print_hash,
                "action_class": str(klass),
                "action_class_reason": reason,
                "envelope_breaches": [str(b) for b in breaches],
                "decision_layer": decision_result.layer.name if decision_result else None,
                "delegated_to": [
                    "app.portfolio.control.permitted",
                    "app.portfolio.decision.decide",
                    "app.safety.envelope.within_envelope",
                ],
            },
        )


__all__ = [
    "SEVERITY",
    "VERIFIER_VERSION",
    "ActionProposal",
    "PolicyVerificationEngine",
    "PolicyVerificationResult",
    "VerificationDecision",
    "Violation",
    "fingerprint",
]
