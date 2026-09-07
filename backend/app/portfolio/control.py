"""What the platform may do to itself without asking. **L61.**

Two mechanisms, and they answer the two ways autonomous control goes wrong.

**1. Loosening a hard constraint is FORBIDDEN, by classification rather than
by list.**

L61 forbids increasing risk limits, leverage, drawdown tolerance, enabling live
trading and disabling kill switches. The obvious implementation is a set of
banned action names -- and a set of names is a blocklist one typo, one rename
or one new action kind away from a hole.

So actions are classified by **what they do**, not what they are called: an
action that LOOSENS a hard constraint is forbidden whatever its name, whatever
its magnitude, and with no approval path. There is no `within_bounds` that
rescues it and no reviewer who can wave it through here, because a control loop
that could be argued into raising its own ceiling has no ceiling.

**Only tightening is ever automatic.** Every "never automatically increase" in
the brief reduces to one rule: autonomy may take permissions away, never grant
them. An action that loosens anything -- even inside an approved range, even
reversibly -- needs a person.

**2. A control loop that oscillates stops controlling.**

`ActionLedger` watches for the same target being pushed in opposite directions
inside a window. That is the L51 defect in another costume: the bot supervisor
had five good safety gates and none of them asked *how many times*, so a bot
that crashed on start restarted forever. Here the equivalent is
reduce-restore-reduce, which looks like responsiveness and is thrashing.

**Nothing here applies anything.** It classifies a proposal and returns the
classification. The RiskEngine remains the final veto and the OMS owns order
state -- asserted by a test, not promised.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class Effect(StrEnum):
    """Which way an action moves the platform's permissions."""

    #: Reduces what is permitted. Smaller allocation, tighter cap, a pause.
    TIGHTENS = "TIGHTENS"
    #: Changes nothing about what is permitted.
    NEUTRAL = "NEUTRAL"
    #: Increases what is permitted. Bigger allocation, looser cap, a resume.
    LOOSENS = "LOOSENS"


class ActionClass(StrEnum):
    """How much autonomy an action is allowed. L61 step 9."""

    OBSERVE_ONLY = "OBSERVE_ONLY"
    RECOMMEND = "RECOMMEND"
    AUTO_APPLY_BOUNDED = "AUTO_APPLY_BOUNDED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    FORBIDDEN = "FORBIDDEN"


@dataclass(frozen=True)
class Action:
    """A proposed change, described by what it does rather than its name."""

    kind: str
    target: str
    effect: Effect
    #: True when the action changes a HARD constraint -- a risk limit, leverage,
    #: a drawdown cap, a kill switch, the trading mode. These are the ceilings
    #: everything else operates under, and a loop may never raise its own.
    touches_hard_limit: bool = False
    #: True when the magnitude sits inside a range somebody already approved.
    within_bounds: bool = True
    #: True when the action can be undone without a second decision.
    reversible: bool = True

    def fingerprint(self) -> str:
        """Identity for loop detection: the same target moved the same way."""
        return f"{self.target}:{self.effect}"


def classify(action: Action) -> tuple[ActionClass, str]:
    """How much autonomy this action gets, and why.

    The order of these rules is the safety property, so they are written in
    order and the first match wins.
    """
    # 1. The rule nothing overrides.
    if action.touches_hard_limit and action.effect is Effect.LOOSENS:
        return ActionClass.FORBIDDEN, (
            f"{action.kind!r} would loosen a hard constraint ({action.target}). "
            "Autonomy may take permissions away and never grant them, so this is "
            "forbidden regardless of magnitude, bounds or reversibility. Raising a "
            "hard limit is a governance action a person takes, with its own approval"
        )

    # 2. Tightening a hard constraint IS the emergency path -- safe mode, a kill
    #    switch, blocking new entries. It must not need a meeting.
    if action.touches_hard_limit and action.effect is Effect.TIGHTENS:
        return ActionClass.AUTO_APPLY_BOUNDED, (
            f"{action.kind!r} tightens {action.target}. A control that is expensive to "
            "engage is one nobody engages in an emergency, so this may be applied "
            "autonomously and is audited"
        )

    # 3. Anything that grants more room needs a person, bounds or not.
    if action.effect is Effect.LOOSENS:
        return ActionClass.REQUIRES_APPROVAL, (
            f"{action.kind!r} would increase what is permitted on {action.target}"
            + (
                ", and does so within an approved range -- but 'never automatically "
                "increase risk' has no in-range exception"
                if action.within_bounds
                else ", outside any approved range"
            )
        )

    # 4. Reducing exposure inside an approved range is the bounded autonomy the
    #    whole level exists to permit.
    if action.effect is Effect.TIGHTENS:
        if not action.within_bounds:
            return ActionClass.REQUIRES_APPROVAL, (
                f"{action.kind!r} tightens {action.target} beyond any approved range. "
                "A large reduction is still a large change, and one nobody sized"
            )
        if not action.reversible:
            return ActionClass.REQUIRES_APPROVAL, (
                f"{action.kind!r} tightens {action.target} irreversibly; bounded "
                "autonomy assumes a way back"
            )
        return ActionClass.AUTO_APPLY_BOUNDED, (
            f"{action.kind!r} reduces {action.target} within an approved range and can be undone"
        )

    return ActionClass.RECOMMEND, (
        f"{action.kind!r} changes nothing about what is permitted; it is offered as a "
        "recommendation"
    )


#: How many direction changes on one target within the window count as
#: thrashing rather than responsiveness.
#:
#: Three, because two is a legitimate correction -- reduce, then restore when
#: the condition clears -- and the third is the point at which the loop is
#: arguing with itself. A configuration decision, not a measurement: no control
#: loop has ever run here.
DEFAULT_OSCILLATION_LIMIT = 3

#: The window direction changes are counted over.
DEFAULT_OSCILLATION_WINDOW = timedelta(hours=1)


@dataclass
class ActionLedger:
    """What has recently been done, so the loop can notice it is looping.

    Holds only what the window needs. This is deliberately not a durable store:
    it answers "am I thrashing right now", and a restart legitimately resets
    that question -- unlike a capital reservation, where a restart losing state
    was the L54 defect.
    """

    limit: int = DEFAULT_OSCILLATION_LIMIT
    window: timedelta = DEFAULT_OSCILLATION_WINDOW
    applied: deque[tuple[datetime, str, Effect]] = field(default_factory=deque)

    def record(self, action: Action, *, at: datetime) -> None:
        self.applied.append((at, action.target, action.effect))
        self._trim(at)

    def _trim(self, now: datetime) -> None:
        cutoff = now - self.window
        while self.applied and self.applied[0][0] < cutoff:
            self.applied.popleft()

    def direction_changes(self, target: str, *, now: datetime) -> int:
        """How many times this target has reversed direction in the window."""
        self._trim(now)
        directions = [
            effect for _, t, effect in self.applied if t == target and effect is not Effect.NEUTRAL
        ]
        return sum(1 for a, b in zip(directions, directions[1:], strict=False) if a is not b)

    def oscillating(self, target: str, *, now: datetime) -> str | None:
        """Why this target should stop being adjusted, or None.

        Returns a sentence rather than a boolean for the same reason every
        other gate in this platform does: the refusal an operator reads has to
        name the condition.
        """
        changes = self.direction_changes(target, now=now)
        if changes < self.limit:
            return None
        hours = self.window.total_seconds() / 3600
        return (
            f"{target} has reversed direction {changes} times in {hours:.0f}h, which is "
            f"its limit of {self.limit}. A control that alternates is not responding to "
            "conditions, it is arguing with itself; it holds until a person looks"
        )


def permitted(action: Action, ledger: ActionLedger, *, now: datetime) -> tuple[ActionClass, str]:
    """Classification, with oscillation able to demote it but never promote it.

    A thrashing target drops to `REQUIRES_APPROVAL` -- **except** when the
    action tightens a hard constraint. An emergency stop must not be blocked
    because the loop has been busy: that would be the loop's instability
    disabling the response to it.
    """
    verdict, reason = classify(action)
    if verdict is ActionClass.FORBIDDEN:
        return verdict, reason
    if action.touches_hard_limit and action.effect is Effect.TIGHTENS:
        return verdict, reason

    loop = ledger.oscillating(action.target, now=now)
    if loop is not None and verdict is ActionClass.AUTO_APPLY_BOUNDED:
        return ActionClass.REQUIRES_APPROVAL, loop
    return verdict, reason


def as_dict(action: Action, verdict: ActionClass, reason: str) -> dict[str, Any]:
    """The record an audit trail keeps of a classification."""
    return {
        "kind": action.kind,
        "target": action.target,
        "effect": str(action.effect),
        "touches_hard_limit": action.touches_hard_limit,
        "within_bounds": action.within_bounds,
        "reversible": action.reversible,
        "classification": str(verdict),
        "reason": reason,
        "authority": (
            "A classification, not an application. The RiskEngine remains the final "
            "veto and the OMS owns order state."
        ),
    }


__all__ = [
    "DEFAULT_OSCILLATION_LIMIT",
    "DEFAULT_OSCILLATION_WINDOW",
    "Action",
    "ActionClass",
    "ActionLedger",
    "Effect",
    "as_dict",
    "classify",
    "permitted",
]
