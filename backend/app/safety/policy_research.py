"""Researching governance policy without being able to change it. **L64.**

Two design decisions carry this module, and both are inversions of what the
obvious implementation would be.

**1. Research operates on an ALLOW-LIST, not a blocklist.**

L62 and L63 built blocklists -- `ENVELOPE_TARGETS`, `GOVERNANCE_TARGETS` --
naming what an autonomous action may not touch. That polarity is right for
actions, because an action's targets are known in advance.

It is exactly wrong for research. A blocklist means *everything is researchable
unless forbidden*, so a safety parameter added next month is researchable by
default, by nobody's decision. `RESEARCHABLE` inverts it: a parameter is
Category A -- immutable, not even researchable -- unless it appears here with a
category and explicit bounds. **Anything unrecognised is HARD_SAFETY**, so the
failure mode of forgetting to classify something is that research refuses to
touch it.

**2. A candidate is DATA, never code.**

Section 9 asks for a sandbox preventing broker access, filesystem abuse, secret
access and arbitrary network access from generated policy. The stronger answer
is not a better sandbox: it is that a candidate is a mapping of parameter name
to number, there is no interpreter for it, and section 8's "never execute
generated policy code" holds because **there is nothing executable to run.**
A test asserts this module contains no `eval`, `exec`, `compile` or import
machinery.

**Nothing here applies anything.** It scores a proposal and returns the score.
The RiskEngine remains the final veto; a test parses the module to keep that
true.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.safety.autonomy import GOVERNANCE_TARGETS
from app.safety.envelope import ENVELOPE_TARGETS


class Category(StrEnum):
    """L64 section 4. What may be researched, and what may not."""

    #: Immutable. Not optimizable, not researchable, not proposable.
    HARD_SAFETY = "HARD_SAFETY"
    #: May be researched. A proposal only; a person still approves it.
    GOVERNANCE = "GOVERNANCE"
    #: May be optimized, inside bounds that are themselves Category A.
    PREFERENCE = "PREFERENCE"


@dataclass(frozen=True)
class Researchable:
    """One parameter research may form a hypothesis about, and its bounds."""

    name: str
    category: Category
    #: Inclusive bounds a candidate value must sit inside. These are Category A
    #: themselves: a candidate that proposes to change a bound is proposing to
    #: change the rules it is judged by.
    low: Decimal
    high: Decimal
    why: str


#: **The allow-list.** Everything not here is HARD_SAFETY.
#:
#: Deliberately short. Each entry is a parameter that genuinely exists in this
#: platform and whose value is a judgement call rather than a safety property.
#: The bounds are configuration decisions, not measurements -- no governance
#: policy has ever run here, so there is no observed distribution to fit -- and
#: they are recorded in `POLICY_CANDIDATE_POLICY.md` as requiring approval.
RESEARCHABLE: tuple[Researchable, ...] = (
    Researchable(
        "oscillation_limit",
        Category.GOVERNANCE,
        Decimal("2"),
        Decimal("6"),
        "how many direction changes on one target count as thrashing. L61 set it "
        "at 3 by reasoning, not measurement, and it is the kind of number a "
        "record of real control-loop behaviour would settle",
    ),
    Researchable(
        "oscillation_window_hours",
        Category.GOVERNANCE,
        Decimal("0.5"),
        Decimal("24"),
        "the window direction changes are counted over",
    ),
    Researchable(
        "evidence_ttl_hours",
        Category.GOVERNANCE,
        Decimal("1"),
        Decimal("72"),
        "how old the evidence behind a certification may be. Lower is stricter",
    ),
    Researchable(
        "certification_ttl_days",
        Category.GOVERNANCE,
        Decimal("1"),
        Decimal("90"),
        "how long a certification stands before renewal",
    ),
    Researchable(
        "medium_violations_before_downgrade",
        Category.GOVERNANCE,
        Decimal("2"),
        Decimal("10"),
        "how many medium violations constitute a pattern rather than noise",
    ),
    Researchable(
        "diversification_preference",
        Category.PREFERENCE,
        Decimal("0"),
        Decimal("1"),
        "how much the allocator prefers spread over concentration",
    ),
    Researchable(
        "stability_preference",
        Category.PREFERENCE,
        Decimal("0"),
        Decimal("1"),
        "how much churn the allocator will accept for a better fit",
    ),
    Researchable(
        "regime_preference",
        Category.PREFERENCE,
        Decimal("0"),
        Decimal("1"),
        "how strongly regime classification weights allocation",
    ),
)

_BY_NAME = {r.name: r for r in RESEARCHABLE}


def categorise(target: str) -> Category:
    """What category a parameter is in. **Unrecognised is HARD_SAFETY.**

    The single most important function in this module. A parameter nobody
    classified is a parameter nobody thought about, and the safe reading of
    that is "do not touch", not "assume it is fine".
    """
    if target in ENVELOPE_TARGETS or target in GOVERNANCE_TARGETS:
        return Category.HARD_SAFETY
    found = _BY_NAME.get(target)
    return found.category if found else Category.HARD_SAFETY


def researchable(target: str) -> Researchable | None:
    return _BY_NAME.get(target)


class Verdict(StrEnum):
    """What static validation concluded about a candidate."""

    ACCEPTED = "ACCEPTED"
    #: Touches Category A. Not reviewable, not approvable.
    REJECTED_HARD_SAFETY = "REJECTED_HARD_SAFETY"
    REJECTED_OUT_OF_BOUNDS = "REJECTED_OUT_OF_BOUNDS"
    REJECTED_MALFORMED = "REJECTED_MALFORMED"
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"


class PromotionState(StrEnum):
    """L64 section 21. Where a candidate is in its life.

    Ordered, and `TRANSITIONS` below permits only forward steps -- there is no
    edge to ACTIVE from anything but CANARY.
    """

    IDEA = "IDEA"
    HYPOTHESIS = "HYPOTHESIS"
    GENERATED = "GENERATED"
    VALIDATING = "VALIDATING"
    REJECTED = "REJECTED"
    BACKTESTED = "BACKTESTED"
    WALK_FORWARD = "WALK_FORWARD"
    STRESS_TESTED = "STRESS_TESTED"
    SHADOW = "SHADOW"
    PROMISING = "PROMISING"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    APPROVED = "APPROVED"
    CERTIFYING = "CERTIFYING"
    CERTIFIED = "CERTIFIED"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"
    RETIRED = "RETIRED"


#: The only permitted moves. Every state may go to REJECTED or RETIRED.
#:
#: **Nothing reaches ACTIVE except from CANARY**, and nothing reaches CANARY
#: except from CERTIFIED. Section 21 says no candidate may jump to ACTIVE, and
#: the way that rule gets broken is a table where the edge quietly exists.
TRANSITIONS: dict[PromotionState, frozenset[PromotionState]] = {
    PromotionState.IDEA: frozenset({PromotionState.HYPOTHESIS}),
    PromotionState.HYPOTHESIS: frozenset({PromotionState.GENERATED}),
    PromotionState.GENERATED: frozenset({PromotionState.VALIDATING}),
    PromotionState.VALIDATING: frozenset({PromotionState.BACKTESTED}),
    PromotionState.BACKTESTED: frozenset({PromotionState.WALK_FORWARD}),
    PromotionState.WALK_FORWARD: frozenset({PromotionState.STRESS_TESTED}),
    PromotionState.STRESS_TESTED: frozenset({PromotionState.SHADOW}),
    PromotionState.SHADOW: frozenset({PromotionState.PROMISING}),
    PromotionState.PROMISING: frozenset({PromotionState.REQUIRES_REVIEW}),
    PromotionState.REQUIRES_REVIEW: frozenset({PromotionState.APPROVED}),
    PromotionState.APPROVED: frozenset({PromotionState.CERTIFYING}),
    PromotionState.CERTIFYING: frozenset({PromotionState.CERTIFIED}),
    PromotionState.CERTIFIED: frozenset({PromotionState.CANARY}),
    PromotionState.CANARY: frozenset({PromotionState.ACTIVE, PromotionState.ROLLED_BACK}),
    PromotionState.ACTIVE: frozenset({PromotionState.ROLLED_BACK, PromotionState.RETIRED}),
    PromotionState.ROLLED_BACK: frozenset({PromotionState.RETIRED}),
    PromotionState.REJECTED: frozenset({PromotionState.RETIRED}),
    PromotionState.RETIRED: frozenset(),
}

#: Any state may be abandoned. Kept separate from `TRANSITIONS` so the forward
#: path reads as a single line rather than as eighteen rows each ending in the
#: same two escapes.
ALWAYS_AVAILABLE = frozenset({PromotionState.REJECTED, PromotionState.RETIRED})


def check_promotion(frm: PromotionState, to: PromotionState) -> str | None:
    """Why this promotion is refused, or None. A sentence, not a boolean."""
    if frm == to:
        return None
    if to in ALWAYS_AVAILABLE:
        return None
    if to in TRANSITIONS.get(frm, frozenset()):
        return None
    if to is PromotionState.ACTIVE:
        return (
            f"{frm} cannot become ACTIVE. A policy becomes active only after a canary "
            "has run, and the canary only after certification -- a candidate that looked "
            "good in research has not been observed doing anything"
        )
    return f"{frm} does not promote to {to}"


@dataclass(frozen=True)
class Hypothesis:
    """L64 section 7. What is being asked, and what would answer it."""

    hypothesis_id: str
    title: str
    motivation: str
    target: str
    baseline_policy_version: str
    expected_effect: str
    validation_plan: str
    rollback_plan: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "title": self.title,
            "motivation": self.motivation,
            "target": self.target,
            "category": str(categorise(self.target)),
            "baseline_policy_version": self.baseline_policy_version,
            "expected_effect": self.expected_effect,
            "validation_plan": self.validation_plan,
            "rollback_plan": self.rollback_plan,
        }


@dataclass(frozen=True)
class Candidate:
    """A proposed set of parameter values. **Data, never code.**

    `changes` maps a parameter name to a number. There is no expression, no
    callable, no source string and no interpreter for one, which is what makes
    section 8's "never execute generated policy code" a structural property
    rather than a rule somebody enforces.
    """

    candidate_id: str
    parent_policy_version: str
    changes: dict[str, Decimal] = field(default_factory=dict)
    rationale: str = ""

    def fingerprint(self) -> str:
        """Identity: the same changes to the same parent are the same policy.

        Excludes the candidate id and the rationale -- two candidates that
        differ only in how they were described are not two policies, and
        including either would make deduplication useless.
        """
        body = "\x1f".join(f"{k}={self.changes[k]}" for k in sorted(self.changes))
        return hashlib.sha256(f"{self.parent_policy_version}\x1e{body}".encode()).hexdigest()[:32]

    def complexity(self) -> int:
        """How many parameters this moves. Section 19's penalty, measured."""
        return len(self.changes)


@dataclass(frozen=True)
class Validation:
    """The result of static validation. Section 11."""

    verdict: Verdict
    candidate_id: str
    fingerprint: str
    violations: tuple[str, ...]
    touched: tuple[str, ...]
    categories: dict[str, str]

    @property
    def accepted(self) -> bool:
        return self.verdict is Verdict.ACCEPTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            "candidate_id": self.candidate_id,
            "fingerprint": self.fingerprint,
            "accepted": self.accepted,
            "violations": list(self.violations),
            "touched": list(self.touched),
            "categories": self.categories,
            "authority": (
                "Static validation only. A candidate that passes has not been approved, "
                "certified or deployed. The RiskEngine remains the final veto."
            ),
        }


def validate(candidate: Candidate, *, seen: dict[str, str] | None = None) -> Validation:
    """Static validation. Section 11. Every check runs; nothing short-circuits.

    A candidate is refused for **every** reason it is refusable, not the first,
    because an author reading a rejection wants the whole list.
    """
    violations: list[str] = []
    categories: dict[str, str] = {}
    touched = tuple(sorted(candidate.changes))

    if not candidate.changes:
        violations.append("a candidate that changes nothing is not a candidate")

    hard: list[str] = []
    for name in touched:
        cat = categorise(name)
        categories[name] = str(cat)
        if cat is Category.HARD_SAFETY:
            hard.append(name)
            continue
        spec = researchable(name)
        assert spec is not None  # categorise() only returns non-HARD for known names
        value = candidate.changes[name]
        if not isinstance(value, Decimal):
            violations.append(f"{name}: value must be a Decimal, got {type(value).__name__}")
            continue
        if not (spec.low <= value <= spec.high):
            violations.append(
                f"{name}: {value} is outside the approved range [{spec.low}, {spec.high}]"
            )

    if hard:
        violations.append(
            f"{', '.join(hard)} is Category A (hard safety). It is immutable: not "
            "optimizable, not researchable and not proposable. There is no approval "
            "path, because a governance loop that could be argued into moving a hard "
            "limit has no hard limits"
        )

    print_hash = candidate.fingerprint()
    if seen is not None and print_hash in seen:
        return Validation(
            Verdict.REJECTED_DUPLICATE,
            candidate.candidate_id,
            print_hash,
            (
                f"identical to a candidate already assessed: {seen[print_hash]}. "
                "Re-testing it would be another draw from the same urn, which is how a "
                "search manufactures a winner",
            ),
            touched,
            categories,
        )

    if hard:
        verdict = Verdict.REJECTED_HARD_SAFETY
    elif not candidate.changes:
        verdict = Verdict.REJECTED_MALFORMED
    elif violations:
        verdict = Verdict.REJECTED_OUT_OF_BOUNDS
    else:
        verdict = Verdict.ACCEPTED

    return Validation(
        verdict, candidate.candidate_id, print_hash, tuple(violations), touched, categories
    )


@dataclass(frozen=True)
class Diff:
    """L64 section 12. Baseline against candidate."""

    changed: dict[str, tuple[Decimal | None, Decimal]]
    hard_safety_touched: tuple[str, ...]

    @property
    def safe_to_review(self) -> bool:
        return not self.hard_safety_touched

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": {
                k: {"from": str(a) if a is not None else None, "to": str(b)}
                for k, (a, b) in self.changed.items()
            },
            "hard_safety_touched": list(self.hard_safety_touched),
            "safe_to_review": self.safe_to_review,
        }


def diff(baseline: dict[str, Decimal], candidate: Candidate) -> Diff:
    changed: dict[str, tuple[Decimal | None, Decimal]] = {}
    for name, value in sorted(candidate.changes.items()):
        before = baseline.get(name)
        if before != value:
            changed[name] = (before, value)
    hard = tuple(n for n in changed if categorise(n) is Category.HARD_SAFETY)
    return Diff(changed, hard)


@dataclass(frozen=True)
class Score:
    """L64 section 18. Safety dominates, and it dominates lexicographically.

    **Not a weighted sum.** A weight, however large, is a price: with a big
    enough performance gain the sum still tips. Comparing safety first and only
    then anything else makes "a policy with better performance but worse safety
    must lose" true by construction rather than by tuning -- the same move L60
    used to make a lower layer unable to relax a higher one.
    """

    safety: int
    robustness: int
    stability: int
    complexity: int

    def ordering(self) -> tuple[int, int, int, int]:
        """Bigger is better. Complexity is negated, so simpler wins ties."""
        return (self.safety, self.robustness, self.stability, -self.complexity)

    def beats(self, other: Score) -> bool:
        return self.ordering() > other.ordering()


def better(challenger: Score, baseline: Score) -> tuple[bool, str]:
    """Whether a challenger should replace a baseline, and why not if not.

    Section 20: never replace the baseline solely because the challenger scores
    higher. A challenger that is worse on safety loses outright, and a
    challenger that merely ties is refused -- an incumbent has evidence a
    candidate does not, so a tie goes to the thing already running.
    """
    if challenger.safety < baseline.safety:
        return False, (
            f"safety {challenger.safety} is below the baseline's {baseline.safety}. "
            "No improvement elsewhere is weighed against it: safety is compared first "
            "and alone"
        )
    if not challenger.beats(baseline):
        return False, (
            "the challenger does not beat the baseline. A tie goes to the incumbent, "
            "which has evidence from running that a candidate does not have"
        )
    return True, "challenger is better on safety-first ordering"


__all__ = [
    "ALWAYS_AVAILABLE",
    "RESEARCHABLE",
    "TRANSITIONS",
    "Candidate",
    "Category",
    "Diff",
    "Hypothesis",
    "PromotionState",
    "Researchable",
    "Score",
    "Validation",
    "Verdict",
    "better",
    "categorise",
    "check_promotion",
    "diff",
    "researchable",
    "validate",
]
