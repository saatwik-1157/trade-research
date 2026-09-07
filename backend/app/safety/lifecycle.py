"""Certification as something that can go stale. **L63 sections 3, 4 and 9.**

L62's `certify()` is a pure function of the invariant registry, so it returns
the same answer forever. That is correct as far as it goes and it is not
enough: **a certification with no expiry is a claim about a moment presented as
a claim about now.**

This adds the two things that make it a lifecycle rather than a stamp:

* **Time.** A certificate has a TTL, and the evidence under it has a much
  shorter one. The certificate may be a month old; the facts under it may not.
* **Transitions.** A deterministic table, with the recovery direction
  deliberately harder than the failure direction.

**The asymmetry is the design.** Falling is one step and automatic; climbing
back is staged and needs evidence at each stage. Section 16 says not to restore
autonomy merely because the original error disappeared, and the way that rule
gets broken is a transition table where `SUSPENDED -> CERTIFIED` exists at all.
It does not exist here.

This module extends `certification.py` rather than replacing it: `certify()`
still decides whether the gates pass, and this decides whether that decision is
still current.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.safety.autonomy import (
    DEFAULT_CERTIFICATION_TTL,
    DEFAULT_EVIDENCE_TTL,
    AutonomyLevel,
    ceiling_for,
)
from app.safety.certification import CertificationState


class AssuranceStatus(StrEnum):
    """L63 section 7. One dimension's health.

    `UNKNOWN` is first-class and is **never** treated as HEALTHY -- section 7
    says so explicitly, and this platform has been bitten by the opposite
    before: `open_symbols` defaulting to an empty frozenset made "nobody told
    me" and "nothing is open" the same fact.
    """

    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"

    @property
    def is_good(self) -> bool:
        return self is AssuranceStatus.HEALTHY

    @property
    def blocks_autonomy(self) -> bool:
        """UNKNOWN blocks exactly as hard as CRITICAL, and that is the point."""
        return self in (AssuranceStatus.CRITICAL, AssuranceStatus.UNKNOWN)


#: The lifecycle states, extending L62's `CertificationState` rather than
#: replacing it. L62's five are reused verbatim; L63 adds the four that only
#: mean something once certification can decay.
class LifecycleState(StrEnum):
    NOT_CERTIFIED = "NOT_CERTIFIED"
    CONDITIONALLY_CERTIFIED = "CONDITIONALLY_CERTIFIED"
    CERTIFIED = "CERTIFIED"
    MONITORED = "MONITORED"
    DEGRADED = "DEGRADED"
    SUSPENDED = "SUSPENDED"
    CERTIFICATION_EXPIRED = "CERTIFICATION_EXPIRED"
    CERTIFICATION_REVOKED = "CERTIFICATION_REVOKED"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"
    REVALIDATING = "REVALIDATING"


#: What may follow what. L63 section 4.
#:
#: Read the recovery rows rather than the failure rows. **No state transitions
#: directly to CERTIFIED except REVALIDATING**, and REVALIDATING is only
#: reachable from a state a person has moved it to. There is no edge by which a
#: platform recovers its full certification because a condition cleared.
TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.NOT_CERTIFIED: frozenset({LifecycleState.REVALIDATING}),
    LifecycleState.CONDITIONALLY_CERTIFIED: frozenset(
        {
            LifecycleState.MONITORED,
            LifecycleState.DEGRADED,
            LifecycleState.SUSPENDED,
            LifecycleState.CERTIFICATION_EXPIRED,
            LifecycleState.CERTIFICATION_REVOKED,
            LifecycleState.REVALIDATION_REQUIRED,
        }
    ),
    LifecycleState.CERTIFIED: frozenset(
        {
            LifecycleState.MONITORED,
            LifecycleState.DEGRADED,
            LifecycleState.SUSPENDED,
            LifecycleState.CERTIFICATION_EXPIRED,
            LifecycleState.CERTIFICATION_REVOKED,
            LifecycleState.REVALIDATION_REQUIRED,
        }
    ),
    LifecycleState.MONITORED: frozenset(
        {
            LifecycleState.DEGRADED,
            LifecycleState.SUSPENDED,
            LifecycleState.CERTIFICATION_EXPIRED,
            LifecycleState.CERTIFICATION_REVOKED,
            LifecycleState.REVALIDATION_REQUIRED,
        }
    ),
    # Degraded may recover to MONITORED -- but only through REVALIDATING.
    LifecycleState.DEGRADED: frozenset(
        {
            LifecycleState.SUSPENDED,
            LifecycleState.CERTIFICATION_REVOKED,
            LifecycleState.REVALIDATION_REQUIRED,
            LifecycleState.REVALIDATING,
        }
    ),
    LifecycleState.SUSPENDED: frozenset(
        {
            LifecycleState.CERTIFICATION_REVOKED,
            LifecycleState.REVALIDATION_REQUIRED,
            LifecycleState.REVALIDATING,
        }
    ),
    LifecycleState.CERTIFICATION_EXPIRED: frozenset(
        {LifecycleState.REVALIDATION_REQUIRED, LifecycleState.REVALIDATING}
    ),
    # Revoked is the closest thing to terminal: it goes to REVALIDATION_REQUIRED
    # and nowhere else, so a revoked certification cannot be quietly resumed.
    LifecycleState.CERTIFICATION_REVOKED: frozenset({LifecycleState.REVALIDATION_REQUIRED}),
    LifecycleState.REVALIDATION_REQUIRED: frozenset({LifecycleState.REVALIDATING}),
    # The one door back, and it is the only one.
    LifecycleState.REVALIDATING: frozenset(
        {
            LifecycleState.CERTIFIED,
            LifecycleState.CONDITIONALLY_CERTIFIED,
            LifecycleState.NOT_CERTIFIED,
            LifecycleState.SUSPENDED,
        }
    ),
}


@dataclass(frozen=True)
class TransitionRefusal:
    frm: LifecycleState
    to: LifecycleState
    reason: str


def check_transition(frm: LifecycleState, to: LifecycleState) -> TransitionRefusal | None:
    """Whether this transition is legal. `None` means it is.

    A refusal rather than a boolean, for the same reason every other gate in
    this platform returns one: the sentence an operator reads has to name the
    condition.
    """
    if frm == to:
        return None
    allowed = TRANSITIONS.get(frm)
    if allowed is None:
        return TransitionRefusal(
            frm, to, f"{frm} is not a state with defined transitions; refusing to guess"
        )
    if to in allowed:
        return None
    if to in (LifecycleState.CERTIFIED, LifecycleState.CONDITIONALLY_CERTIFIED):
        return TransitionRefusal(
            frm,
            to,
            f"{frm} cannot become {to} directly. Certification is regained only through "
            "REVALIDATING, and only after the gates pass again -- a condition clearing "
            "is not evidence that the thing it broke now works",
        )
    return TransitionRefusal(frm, to, f"{frm} does not transition to {to}")


@dataclass(frozen=True)
class Assurance:
    """Whether a certification is still standing, and what it permits now.

    Everything here is derived. Nothing is stored, because there is nothing
    running to store it about -- see `CONTINUOUS_ASSURANCE_ARCHITECTURE.md`.
    """

    state: LifecycleState
    autonomy: AutonomyLevel
    at: datetime
    certified_at: datetime | None
    evidence_at: datetime | None
    expires_at: datetime | None
    reasons: tuple[str, ...]
    dimensions: dict[str, AssuranceStatus]

    @property
    def expired(self) -> bool:
        return self.state is LifecycleState.CERTIFICATION_EXPIRED

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "autonomy_level": self.autonomy.name,
            "may_auto_apply": self.autonomy.may_auto_apply,
            "at": self.at.isoformat(),
            "certified_at": self.certified_at.isoformat() if self.certified_at else None,
            "evidence_at": self.evidence_at.isoformat() if self.evidence_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "reasons": list(self.reasons),
            "dimensions": {k: str(v) for k, v in self.dimensions.items()},
            "authority": (
                "An assessment, not an application. The RiskEngine remains the final "
                "veto and the OMS owns order state."
            ),
        }


def assess(
    *,
    base: CertificationState,
    now: datetime,
    certified_at: datetime | None = None,
    evidence_at: datetime | None = None,
    dimensions: dict[str, AssuranceStatus] | None = None,
    critical_invariant_failed: bool = False,
    ttl: timedelta = DEFAULT_CERTIFICATION_TTL,
    evidence_ttl: timedelta = DEFAULT_EVIDENCE_TTL,
) -> Assurance:
    """What the certification is worth right now.

    The order is the safety property, and it is written worst-first: a hard
    failure is not weighed against a good score, it replaces it. Section 8 is
    explicit that hard safety failures override numerical scores, and the way
    that rule gets broken is a weighted average where CRITICAL is worth -3.
    """
    dims = dict(dimensions or {})
    reasons: list[str] = []

    # 1. A critical invariant failure. Nothing else is consulted.
    if critical_invariant_failed:
        state = LifecycleState.CERTIFICATION_REVOKED
        reasons.append(
            "a critical safety invariant failed. Certification is revoked outright "
            "rather than scored down: an invariant is what every other judgement rests "
            "on, and there is no numerical result that survives one being false"
        )
        return Assurance(
            state,
            ceiling_for(state),
            now,
            certified_at,
            evidence_at,
            None,
            tuple(reasons),
            dims,
        )

    # 2. Never certified in the first place.
    if certified_at is None:
        state = LifecycleState(base.value)
        reasons.append("no certification has been issued")
        return Assurance(
            state, ceiling_for(state), now, None, evidence_at, None, tuple(reasons), dims
        )

    expires_at = certified_at + ttl

    # 3. A dimension that is CRITICAL or UNKNOWN. Both block, equally.
    blocking = sorted(k for k, v in dims.items() if v.blocks_autonomy)
    unknown = sorted(k for k, v in dims.items() if v is AssuranceStatus.UNKNOWN)

    # 4. Time.
    if now >= expires_at:
        state = LifecycleState.CERTIFICATION_EXPIRED
        reasons.append(
            f"the certification issued at {certified_at.isoformat()} expired at "
            f"{expires_at.isoformat()}. It is not evidence about now"
        )
    elif evidence_at is not None and now - evidence_at > evidence_ttl:
        state = LifecycleState.DEGRADED
        reasons.append(
            f"the evidence behind the certification was gathered at "
            f"{evidence_at.isoformat()} and is older than {evidence_ttl}. The certificate "
            "has not expired; what it rests on has gone stale, which is a different fact "
            "and a milder one"
        )
    elif evidence_at is None:
        state = LifecycleState.DEGRADED
        reasons.append(
            "no evidence timestamp is recorded, so the certification cannot be shown to "
            "rest on anything current. Unknown is not healthy"
        )
    elif blocking:
        state = LifecycleState.SUSPENDED
        reasons.append(
            f"assurance dimension(s) {', '.join(blocking)} are CRITICAL or UNKNOWN. "
            "UNKNOWN counts the same as CRITICAL here: a dimension nobody could measure "
            "is not a dimension that is fine"
        )
    elif any(v is AssuranceStatus.DEGRADED for v in dims.values()):
        state = LifecycleState.DEGRADED
        reasons.append(
            ", ".join(sorted(k for k, v in dims.items() if v is AssuranceStatus.DEGRADED))
            + " degraded"
        )
    else:
        state = (
            LifecycleState.MONITORED
            if base is CertificationState.CERTIFIED
            else LifecycleState(base.value)
        )
        reasons.append(
            "certification current, evidence fresh, no dimension blocking. "
            "MONITORED rather than CERTIFIED because a certification that is being "
            "watched is a different claim from one that was issued and filed"
        )

    if unknown and state not in (
        LifecycleState.SUSPENDED,
        LifecycleState.CERTIFICATION_EXPIRED,
    ):
        reasons.append(f"unmeasured dimension(s): {', '.join(unknown)}")

    return Assurance(
        state,
        ceiling_for(state),
        now,
        certified_at,
        evidence_at,
        expires_at,
        tuple(reasons),
        dims,
    )


__all__ = [
    "TRANSITIONS",
    "Assurance",
    "AssuranceStatus",
    "LifecycleState",
    "TransitionRefusal",
    "assess",
    "check_transition",
]
