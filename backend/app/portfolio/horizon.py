"""Decisions that reason over different time scales. **L65.**

**Horizon is not a second copy of `Layer`, and the difference is the design.**

L60's `Layer` answers *who says so* -- HARD_SAFETY, RISK, PORTFOLIO, STRATEGY,
SIGNAL, OPTIMIZATION, PREFERENCE. `Horizon` answers *over what period*. They are
orthogonal: a long-horizon recommendation from RISK and a long-horizon
recommendation from OPTIMIZATION carry different authority despite sharing a
time scale.

Because they are orthogonal they must **compose, not compete**. Two independent
orderings resolving the same conflict is the "two engines that agree until they
do not" failure, and the one that disagreed silently would be the one nobody
read. So `synthesise()` maps each horizon recommendation onto a `Finding` and
**delegates the resolution entirely to `decision.decide()`**. There is no second
resolver here, and a test asserts `decide` is the function actually called.

What horizon adds is the four things a single-shot resolver cannot express:

* **Deferral.** L60's `decide()` picks a winner. L65 section 8 requires that a
  long-term recommendation which loses to a short-term constraint is *kept*,
  with the reason, rather than discarded -- because the reason it lost is a
  condition that will pass.
* **Expiration.** A recommendation is about the moment it was made. A long-term
  one may outlive a short-term one, but none of them lives forever.
* **Revalidation.** A deferred recommendation is re-checked against current
  conditions before it applies. Never executed as-written against a world that
  has moved.
* **Hysteresis.** A minimum change threshold, so a target is not pushed back
  and forth by noise -- **and never applied to an emergency**, because a delay
  on a safety action is the one thing hysteresis must not buy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import IntEnum
from typing import Any

from app.portfolio.decision import (
    Decision,
    DecisionContext,
    Finding,
    Layer,
    Verdict,
    decide,
)


class Horizon(IntEnum):
    """L65 section 7. **Lower number wins.**

    Deliberately inverted relative to `Layer`, where higher wins. The two are
    different questions and sharing a direction would invite reading one as the
    other; a reader who has to check which way round it is will check.
    """

    EMERGENCY = 0
    IMMEDIATE_RISK = 1
    SHORT_TERM = 2
    MEDIUM_TERM = 3
    LONG_TERM = 4

    @property
    def is_safety(self) -> bool:
        """EMERGENCY and IMMEDIATE_RISK are never deferred and never smoothed."""
        return self <= Horizon.IMMEDIATE_RISK


#: Which `Layer` a horizon's recommendation carries into `decide()`.
#:
#: This mapping is where the two orderings compose. It is deliberately
#: conservative: a long-horizon recommendation enters as OPTIMIZATION, which is
#: the second-weakest layer, so a strategic view cannot outrank a portfolio
#: constraint merely by being strategic.
LAYER_OF: dict[Horizon, Layer] = {
    Horizon.EMERGENCY: Layer.HARD_SAFETY,
    Horizon.IMMEDIATE_RISK: Layer.RISK,
    Horizon.SHORT_TERM: Layer.STRATEGY,
    Horizon.MEDIUM_TERM: Layer.PORTFOLIO,
    Horizon.LONG_TERM: Layer.OPTIMIZATION,
}

#: How long a recommendation from each horizon stays valid. Section 29.
#:
#: Configuration decisions, not measurements: no multi-horizon decision has
#: ever been made here, so there is no observed rate at which these go stale.
#: Recorded in `DECISION_EXPIRATION_POLICY.md` as requiring approval.
DEFAULT_VALIDITY: dict[Horizon, timedelta] = {
    Horizon.EMERGENCY: timedelta(minutes=5),
    Horizon.IMMEDIATE_RISK: timedelta(minutes=15),
    Horizon.SHORT_TERM: timedelta(hours=4),
    Horizon.MEDIUM_TERM: timedelta(days=2),
    Horizon.LONG_TERM: timedelta(days=14),
}


@dataclass(frozen=True)
class HorizonRecommendation:
    """What one horizon thinks, and when it stops being about now."""

    horizon: Horizon
    verdict: Verdict
    reason: str
    #: What it is about, so a deferral can name a dependency and a duplicate
    #: can be recognised.
    target: str = ""
    #: Size of the change proposed, where there is one. Used by hysteresis.
    magnitude: Decimal | None = None
    at: datetime | None = None
    validity: timedelta | None = None
    #: Component confidences. Section 12 forbids one opaque score, so this is a
    #: mapping and an absent entry is absent -- never read as high.
    confidence: dict[str, Decimal] = field(default_factory=dict)

    def expires_at(self) -> datetime | None:
        if self.at is None:
            return None
        return self.at + (self.validity or DEFAULT_VALIDITY[self.horizon])

    def expired(self, *, now: datetime) -> bool:
        """**A recommendation with no timestamp is expired**, not eternal.

        Section 29 says expired recommendations must not execute. An undated
        one cannot be shown to be current, and unknown is not fresh -- the same
        rule `decision.Input.freshness` applies to evidence.
        """
        end = self.expires_at()
        return end is None or now >= end

    def as_finding(self) -> Finding:
        return Finding(
            layer=LAYER_OF[self.horizon],
            verdict=self.verdict,
            reason=f"[{self.horizon.name}] {self.reason}",
            inputs=(self.target,) if self.target else (),
        )


@dataclass(frozen=True)
class Deferred:
    """A recommendation that lost, kept with the reason it lost. Section 8."""

    recommendation: HorizonRecommendation
    #: The horizon whose verdict displaced it.
    displaced_by: Horizon
    reason: str
    #: What would have to become true. Named rather than implied, because a
    #: deferral with no condition is a queue entry nobody can ever clear.
    required_condition: str
    expires_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.recommendation.horizon.name,
            "verdict": str(self.recommendation.verdict),
            "target": self.recommendation.target,
            "displaced_by": self.displaced_by.name,
            "deferred_reason": self.reason,
            "required_condition": self.required_condition,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True)
class MultiHorizonDecision:
    """The synthesised result, and everything that fed it."""

    decision_id: str
    at: datetime
    account_id: str
    environment: str
    decision: Decision
    recommendations: tuple[HorizonRecommendation, ...]
    deferred: tuple[Deferred, ...]
    expired: tuple[HorizonRecommendation, ...]
    conflicts: tuple[str, ...]
    confidence: dict[str, Decimal]

    @property
    def verdict(self) -> Verdict:
        return self.decision.verdict

    @property
    def deciding_horizon(self) -> Horizon | None:
        """Which horizon's recommendation actually won."""
        for rec in self.recommendations:
            if (
                LAYER_OF[rec.horizon] is self.decision.layer
                and rec.verdict is self.decision.verdict
            ):
                return rec.horizon
        return None

    def explain(self) -> str:
        """Section 34. Why this decision won, in a sentence a person reads."""
        lines = [f"{r.horizon.name}: {r.verdict}" for r in self.recommendations]
        won = self.deciding_horizon
        head = ", ".join(lines) if lines else "no horizon reported"
        tail = (
            f"{won.name} wins ({self.decision.layer.name} layer)"
            if won
            else f"{self.decision.layer.name} decides"
        )
        conflict = f" Conflicts: {'; '.join(self.conflicts)}." if self.conflicts else ""
        deferred = (
            f" Deferred: {', '.join(d.recommendation.horizon.name for d in self.deferred)}."
            if self.deferred
            else ""
        )
        return f"{head}. {tail} -> {self.decision.verdict}.{conflict}{deferred}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "at": self.at.isoformat(),
            "account_id": self.account_id,
            "environment": self.environment,
            "final": str(self.decision.verdict),
            "decided_by_layer": self.decision.layer.name,
            "decided_by_horizon": (self.deciding_horizon.name if self.deciding_horizon else None),
            "reason": self.decision.reason,
            "explanation": self.explain(),
            "conflicts": list(self.conflicts),
            "deferred": [d.as_dict() for d in self.deferred],
            "expired": [r.horizon.name for r in self.expired],
            "confidence": {k: str(v) for k, v in self.confidence.items()},
            "authority": (
                "A recommendation. The RiskEngine remains the final veto, the OMS owns "
                "order state, and nothing here reaches either."
            ),
        }


def conflicts_between(
    recommendations: tuple[HorizonRecommendation, ...],
) -> tuple[str, ...]:
    """Pairs of horizons that disagree about the same target.

    Only a genuine disagreement counts: two horizons that both say ALLOW are
    not in conflict, and two that speak about different targets are not either.
    """
    out: list[str] = []
    for i, a in enumerate(recommendations):
        for b in recommendations[i + 1 :]:
            if a.target != b.target:
                continue
            if a.verdict is b.verdict:
                continue
            out.append(
                f"{a.horizon.name} says {a.verdict} and {b.horizon.name} says "
                f"{b.verdict} about {a.target or 'the portfolio'}"
            )
    return tuple(out)


def synthesise(
    *,
    decision_id: str,
    account_id: str,
    now: datetime,
    recommendations: tuple[HorizonRecommendation, ...],
    context: DecisionContext | None = None,
    environment: str = "paper",
) -> MultiHorizonDecision:
    """Resolve the horizons. **Delegates the resolution to L60's `decide`.**

    Deterministic for identical inputs, which section 11 requires and which
    falls out of `decide()` being a pure function over the findings.
    """
    live: list[HorizonRecommendation] = []
    expired: list[HorizonRecommendation] = []
    for rec in recommendations:
        # An emergency is never set aside for being old. If an emergency
        # recommendation has gone stale, that is a reason to look at it, not a
        # reason to drop it -- so it stays live and the staleness is a finding
        # for a person rather than a silent removal.
        if rec.horizon is Horizon.EMERGENCY or not rec.expired(now=now):
            live.append(rec)
        else:
            expired.append(rec)

    ctx = context if context is not None else DecisionContext(now=now)
    for rec in live:
        ctx.add_finding(rec.as_finding())

    result = decide(ctx)

    # Anything that lost to a MORE restrictive verdict is deferred rather than
    # discarded: the condition that beat it is a condition that can pass.
    deferred: list[Deferred] = []
    winner_horizon = min(
        (r.horizon for r in live if r.verdict is result.verdict),
        default=Horizon.EMERGENCY,
    )
    for rec in live:
        if rec.verdict is result.verdict:
            continue
        if rec.horizon.is_safety:
            # A safety horizon is never "deferred". It either decided or it
            # agreed; it does not wait in a queue.
            continue
        deferred.append(
            Deferred(
                recommendation=rec,
                displaced_by=winner_horizon,
                reason=(
                    f"{winner_horizon.name} returned {result.verdict}, which is more "
                    f"restrictive than this {rec.horizon.name} recommendation. It is "
                    "kept rather than discarded: what displaced it is a condition, and "
                    "conditions pass"
                ),
                required_condition=(f"{winner_horizon.name} no longer returns {result.verdict}"),
                expires_at=rec.expires_at(),
            )
        )

    confidence: dict[str, Decimal] = {}
    for rec in live:
        for key, value in rec.confidence.items():
            confidence[f"{rec.horizon.name.lower()}.{key}"] = value

    return MultiHorizonDecision(
        decision_id=decision_id,
        at=now,
        account_id=account_id,
        environment=environment,
        decision=result,
        recommendations=tuple(live),
        deferred=tuple(deferred),
        expired=tuple(expired),
        conflicts=conflicts_between(tuple(live)),
        confidence=confidence,
    )


#: Smallest change worth making. Section 16.
#:
#: A configuration decision, not a measurement. Nothing has ever adjusted an
#: allocation here, so there is no observed noise floor to set it against.
DEFAULT_MIN_CHANGE = Decimal("0.02")


def worth_acting_on(
    recommendation: HorizonRecommendation,
    *,
    minimum: Decimal = DEFAULT_MIN_CHANGE,
) -> tuple[bool, str]:
    """Hysteresis: is this change big enough to be worth making?

    **Never applied to a safety horizon.** Section 16's last line: never use
    hysteresis to delay emergency safety actions. A smoothing rule that also
    smoothed the emergency stop would be the L61 lesson repeated -- a guard
    that suppresses the response to the thing it guards against.
    """
    if recommendation.horizon.is_safety:
        return True, (
            f"{recommendation.horizon.name} is a safety horizon and is never smoothed. "
            "Hysteresis exists to stop noise moving an allocation, not to put a delay "
            "in front of a stop"
        )
    if recommendation.magnitude is None:
        return True, "no magnitude stated, so there is nothing to compare against"
    if abs(recommendation.magnitude) < minimum:
        return False, (
            f"a change of {recommendation.magnitude} is below the {minimum} threshold. "
            "Acting on it would be reacting to noise, and reduce-restore-reduce is how "
            "a control loop starts arguing with itself"
        )
    return True, f"a change of {recommendation.magnitude} clears the {minimum} threshold"


@dataclass(frozen=True)
class Revalidation:
    """Whether a deferred recommendation may now apply. Section 30."""

    allowed: bool
    reason: str


def revalidate(
    deferred: Deferred,
    *,
    now: datetime,
    condition_cleared: bool,
    certification_permits: bool,
    risk_permits: bool,
    data_fresh: bool,
) -> Revalidation:
    """Re-check a deferred recommendation against conditions as they are NOW.

    Every gate is re-asked. **A recommendation is never applied as written
    against a world that has moved**, which is section 30's whole point: the
    conditions that made it sensible are exactly the conditions that changed
    while it waited.

    Written worst-first and it never short-circuits into a yes: the only way to
    `allowed=True` is past all five checks.
    """
    if deferred.expires_at is not None and now >= deferred.expires_at:
        return Revalidation(
            False,
            f"the recommendation expired at {deferred.expires_at.isoformat()}. An "
            "expired recommendation does not execute, however eligible its condition "
            "has become",
        )
    if not condition_cleared:
        return Revalidation(
            False, f"the deferral condition still holds: {deferred.required_condition}"
        )
    if not certification_permits:
        return Revalidation(
            False,
            "certification does not currently permit autonomous adaptation. A "
            "recommendation made under one certification does not carry into another",
        )
    if not risk_permits:
        return Revalidation(
            False,
            "the RiskEngine does not permit it now. Risk is re-asked rather than "
            "remembered: the answer it gave when this was proposed was about a "
            "portfolio that no longer exists",
        )
    if not data_fresh:
        return Revalidation(
            False,
            "the data this would act on is not fresh. Missing is never read as permission",
        )
    return Revalidation(True, "revalidated against current conditions")


__all__ = [
    "DEFAULT_MIN_CHANGE",
    "DEFAULT_VALIDITY",
    "LAYER_OF",
    "Deferred",
    "Horizon",
    "HorizonRecommendation",
    "MultiHorizonDecision",
    "Revalidation",
    "conflicts_between",
    "revalidate",
    "synthesise",
    "worth_acting_on",
]
