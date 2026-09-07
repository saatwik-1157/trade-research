"""How much the platform may do without asking, and how it loses that. **L63.**

L62 protected the safety ENVELOPE from being widened by the thing it bounds:
every envelope field is a hard limit, so an action aimed at one is an action
that loosens a hard constraint, which L61 already forbids outright.

**L63 found that the certification machinery itself was not protected the same
way.** Before this module, an action targeting `autonomy_level`,
`certification_state`, `certification_expiry` or `assurance_threshold`
classified as `REQUIRES_APPROVAL` rather than `FORBIDDEN` -- an approval path
by which the system could raise its own autonomy or extend its own
certification. Section 15 says never allow automatic escalation above the
currently certified level, and section 23 says the system may not disable its
own certification; `REQUIRES_APPROVAL` satisfies neither, because it is a door
rather than a wall.

The envelope needed no new mechanism at L62 and this needs none either. It
needed the governance machinery to be **inside** the set L61 already protects.

**Autonomy is a ceiling, never a floor.** Every rule here can only lower it.
There is no function in this module that raises an autonomy level, which is why
the escalation rule needs no enforcement: it is not expressible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum, StrEnum

from app.safety.invariants import Severity


class AutonomyLevel(IntEnum):
    """L63 section 15. What the platform may do unattended.

    `IntEnum` so a comparison IS the ordering, the same reasoning
    `decision.Layer` uses: a lookup table beside the documentation eventually
    disagrees with it.
    """

    OBSERVE = 0
    RECOMMEND = 1
    APPROVAL = 2
    BOUNDED = 3
    CERTIFIED = 4

    @property
    def may_auto_apply(self) -> bool:
        """Only BOUNDED and above act without a person."""
        return self >= AutonomyLevel.BOUNDED


class ViolationSeverity(StrEnum):
    """L63 section 14."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


#: Targets that ARE the governance machinery. An action aimed at one is asking
#: to change the rules it is judged by, or the record of whether it may act at
#: all.
#:
#: `ENVELOPE_TARGETS` (L62) covers the numeric bounds; this covers the
#: certification and autonomy state around them, plus the three controls
#: section 23 names explicitly -- audit logging, monitoring and the approval
#: requirement itself. A system that can switch off its own audit log has no
#: audit log.
GOVERNANCE_TARGETS: frozenset[str] = frozenset(
    {
        "autonomy_level",
        "autonomy",
        "certification",
        "certification_state",
        "certification_expiry",
        "certification_gate",
        "assurance_threshold",
        "assurance_dimension",
        "invariant",
        "invariant_status",
        "safety_invariant",
        "audit_logging",
        "audit_log",
        "monitoring",
        "approval_requirement",
        "approval_required",
        "policy_version",
        "verifier",
    }
)


#: The highest autonomy each certification state permits.
#:
#: A ceiling, not a setting. `CERTIFIED` permits level 4; it does not grant it.
#: Anything the platform has not positively certified sits at RECOMMEND or
#: below, which is the fail-closed reading of section 16's "unknown safety
#: state: fail closed".
CEILING: dict[str, AutonomyLevel] = {
    "CERTIFIED": AutonomyLevel.CERTIFIED,
    "MONITORED": AutonomyLevel.CERTIFIED,
    "CONDITIONALLY_CERTIFIED": AutonomyLevel.BOUNDED,
    "DEGRADED": AutonomyLevel.APPROVAL,
    "REVALIDATING": AutonomyLevel.RECOMMEND,
    "REVALIDATION_REQUIRED": AutonomyLevel.RECOMMEND,
    "SUSPENDED": AutonomyLevel.RECOMMEND,
    "CERTIFICATION_EXPIRED": AutonomyLevel.RECOMMEND,
    "NOT_CERTIFIED": AutonomyLevel.OBSERVE,
    "CERTIFICATION_REVOKED": AutonomyLevel.OBSERVE,
}


def ceiling_for(certification_state: str) -> AutonomyLevel:
    """The most autonomy this certification state allows.

    **An unrecognised state returns OBSERVE**, not a default and not the
    previous value. Section 16: unknown safety state fails closed. A state
    added later that nobody mapped is exactly the case where a permissive
    default would be silently wrong.
    """
    return CEILING.get(certification_state, AutonomyLevel.OBSERVE)


@dataclass(frozen=True)
class Downgrade:
    """A reduction in autonomy, and why. There is no `Upgrade`."""

    to: AutonomyLevel
    reason: str
    severity: ViolationSeverity


def downgrade_for(
    *,
    current: AutonomyLevel,
    critical_invariant_failed: bool = False,
    high_violations: int = 0,
    medium_violations: int = 0,
    safety_state_unknown: bool = False,
    evidence_stale: bool = False,
) -> Downgrade | None:
    """How far autonomy drops, given what went wrong. L63 section 16.

    Written in order of severity, first match wins, and **every branch returns
    a level at or below `current`** -- there is no path through this function
    that increases autonomy. Restoration is a separate, staged process that a
    person starts.
    """
    if critical_invariant_failed:
        return Downgrade(
            AutonomyLevel.OBSERVE,
            "a critical safety invariant failed. Autonomy is suspended entirely rather "
            "than reduced: an invariant is the thing every other judgement rests on, and "
            "a platform that cannot trust one of them cannot trust a smaller version of "
            "the same decision",
            ViolationSeverity.CRITICAL,
        )

    if safety_state_unknown:
        return Downgrade(
            AutonomyLevel.OBSERVE,
            "the safety state could not be established. Unknown is not healthy and is "
            "never read as permission -- this is the fail-closed branch, and it is "
            "deliberately as severe as an outright failure because the two are "
            "indistinguishable from here",
            ViolationSeverity.CRITICAL,
        )

    if high_violations > 0:
        return Downgrade(
            min(current, AutonomyLevel.APPROVAL),
            f"{high_violations} high-severity policy violation(s). A person sees every "
            "action until it is revalidated",
            ViolationSeverity.HIGH,
        )

    if evidence_stale:
        return Downgrade(
            min(current, AutonomyLevel.APPROVAL),
            "the evidence certification rests on is stale. A certification is a claim "
            "about the moment it was made, and an old claim is not a current one",
            ViolationSeverity.MEDIUM,
        )

    if medium_violations >= 3:
        return Downgrade(
            min(current, AutonomyLevel.BOUNDED),
            f"{medium_violations} medium-severity violations. One is noise; a run of them "
            "is a pattern, and the pattern is what a single-event rule cannot see",
            ViolationSeverity.MEDIUM,
        )

    return None


#: The staged path back. L63 section 17.
#:
#: A list rather than a transition function because the ORDER is the safety
#: property and a reader has to be able to see it. Failure to full autonomy in
#: one step is the thing section 17 exists to forbid, and it is the shape every
#: restoration takes when it is written as "clear the error, restore the
#: setting".
RESTORATION_PATH: tuple[tuple[AutonomyLevel, str], ...] = (
    (AutonomyLevel.OBSERVE, "safe state reached and the cause diagnosed"),
    (AutonomyLevel.RECOMMEND, "targeted regression for the affected gates passes"),
    (AutonomyLevel.APPROVAL, "shadow operation shows the expected decisions"),
    (AutonomyLevel.BOUNDED, "a limited canary has run without violation"),
    (AutonomyLevel.CERTIFIED, "full revalidation passes and certification is renewed"),
)


def next_restoration_step(
    current: AutonomyLevel, *, ceiling: AutonomyLevel
) -> tuple[AutonomyLevel, str] | None:
    """The single next step up, or None. **Never more than one step.**

    Capped by the certification ceiling, so restoration cannot walk past what
    the platform is currently certified for -- section 15's "never allow
    automatic escalation above the currently certified level". Returning one
    step at a time is what makes each step have to be earned separately.
    """
    for level, requirement in RESTORATION_PATH:
        if level > current:
            if level > ceiling:
                return None
            return level, requirement
    return None


#: How long a certification stands before it has to be renewed.
#:
#: A certification with no expiry is a claim about a moment presented as a
#: claim about now. Thirty days is a configuration decision, not a measurement:
#: nothing has ever been recertified here, so there is no observed rate of
#: drift to fit to. Recorded in `CERTIFICATION_LIFECYCLE.md` as an assumption
#: requiring approval.
DEFAULT_CERTIFICATION_TTL = timedelta(days=30)

#: How old the evidence behind a certification may be before the certification
#: is treated as resting on nothing. Deliberately far shorter than the TTL:
#: the certificate can be a month old, but the facts under it cannot.
DEFAULT_EVIDENCE_TTL = timedelta(days=1)


def severity_of(invariant_severity: Severity) -> ViolationSeverity:
    """Map an invariant's severity onto a violation's. One direction only."""
    return {
        Severity.CRITICAL: ViolationSeverity.CRITICAL,
        Severity.HIGH: ViolationSeverity.HIGH,
        Severity.MEDIUM: ViolationSeverity.MEDIUM,
    }[invariant_severity]


__all__ = [
    "CEILING",
    "DEFAULT_CERTIFICATION_TTL",
    "DEFAULT_EVIDENCE_TTL",
    "GOVERNANCE_TARGETS",
    "RESTORATION_PATH",
    "AutonomyLevel",
    "Downgrade",
    "ViolationSeverity",
    "ceiling_for",
    "downgrade_for",
    "next_restoration_step",
    "severity_of",
]
