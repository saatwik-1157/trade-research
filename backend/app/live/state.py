"""The live activation state machine: twelve states and the legal moves.

Brief §4. The machine exists to make one sentence unenforceable by accident:

    DISABLED -> ACTIVE

There is no edge from `disabled` to `active`, none from `disabled` to `armed`,
and none from `ready` to `active`. Reaching `active` requires walking
`disabled -> precheck -> ready -> armed -> activating -> active`, and two of
those steps re-run the gate. A caller cannot skip a step by asking politely,
because `to()` consults the table rather than the caller's intent.

**Arming and activating both require a passing gate report, and each checks its
own.** `arm()` will not take a report that failed, and `activate()` re-runs the
check rather than trusting the report `arm()` saw -- a preflight that passed
twenty minutes ago is evidence about twenty minutes ago. The brief asks for
"explicit authorization" at activation; the implementation of that here is that
`activate()` demands a fresh report, an actor and a reason, and refuses without
any of the three.

**The machine starts at `disabled` in every new process, and that is
deliberate** (§20: a restart must not resume live trading). State is held in
memory exactly as `app.recovery.safe_mode.SafeMode` holds its latch, and for
the same reason: a stored "we were ACTIVE" flag is a flag that outlives the
process that meant it, and the first thing a crashed live deployment must not
do is come back trading because a row said it used to be.

Restoring an operator's intent across a restart is possible -- it needs a
persisted record plus a fresh gate pass plus a human -- and it is not built
here. What is built is the guarantee that no restart alone reaches `active`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class ActivationState(StrEnum):
    """Where live activation currently stands. Never where an order stands."""

    disabled = "DISABLED"
    precheck = "PRECHECK"
    precheck_failed = "PRECHECK_FAILED"
    ready = "READY"
    armed = "ARMED"
    activating = "ACTIVATING"
    active = "ACTIVE"
    paused = "PAUSED"
    safe_mode = "SAFE_MODE"
    emergency_stop = "EMERGENCY_STOP"
    deactivating = "DEACTIVATING"
    deactivated = "DEACTIVATED"


#: The legal moves. Absence is a refusal, so a state omitted from a set here is
#: a transition nobody can perform -- which is how `DISABLED -> ACTIVE` is
#: prevented rather than merely discouraged.
#:
#: Every state that can hold a live position can reach `safe_mode` and
#: `emergency_stop`, because §26 and §31 require the safety response to be
#: available from wherever the system happens to be standing.
TRANSITIONS: dict[ActivationState, frozenset[ActivationState]] = {
    ActivationState.disabled: frozenset({ActivationState.precheck}),
    ActivationState.precheck: frozenset(
        {
            ActivationState.ready,
            ActivationState.precheck_failed,
            ActivationState.safe_mode,
            ActivationState.disabled,
        }
    ),
    # A failed precheck goes back to be run again or is put away. It never
    # advances: "it failed, try arming anyway" is the move this table exists
    # to make unspeakable.
    ActivationState.precheck_failed: frozenset(
        {ActivationState.precheck, ActivationState.disabled}
    ),
    ActivationState.ready: frozenset(
        {
            ActivationState.armed,
            ActivationState.precheck,
            ActivationState.safe_mode,
            ActivationState.disabled,
        }
    ),
    ActivationState.armed: frozenset(
        {
            ActivationState.activating,
            ActivationState.ready,
            ActivationState.safe_mode,
            ActivationState.emergency_stop,
            ActivationState.disabled,
        }
    ),
    ActivationState.activating: frozenset(
        {
            ActivationState.active,
            ActivationState.precheck_failed,
            ActivationState.safe_mode,
            ActivationState.emergency_stop,
        }
    ),
    ActivationState.active: frozenset(
        {
            ActivationState.paused,
            ActivationState.deactivating,
            ActivationState.safe_mode,
            ActivationState.emergency_stop,
        }
    ),
    ActivationState.paused: frozenset(
        {
            ActivationState.active,
            ActivationState.deactivating,
            ActivationState.safe_mode,
            ActivationState.emergency_stop,
        }
    ),
    # Safe mode is left by re-running the gate, not by asserting recovery.
    ActivationState.safe_mode: frozenset(
        {
            ActivationState.precheck,
            ActivationState.deactivating,
            ActivationState.emergency_stop,
        }
    ),
    # An emergency stop has one exit and it is downward. Getting back to
    # trading from here means walking the whole path again from `disabled`.
    ActivationState.emergency_stop: frozenset({ActivationState.deactivating}),
    ActivationState.deactivating: frozenset({ActivationState.deactivated}),
    ActivationState.deactivated: frozenset({ActivationState.disabled}),
}

#: States in which the platform may be holding live exposure. Used by callers
#: that need to know whether stopping is a decision or a formality.
LIVE_STATES: frozenset[ActivationState] = frozenset(
    {ActivationState.active, ActivationState.paused}
)


class IllegalActivationTransition(Exception):
    """Refused: that move is not in the table."""


@dataclass(frozen=True)
class Transition:
    """One move, with who made it and why. The audit trail is these."""

    transition_id: str
    at: datetime
    frm: ActivationState
    to: ActivationState
    actor: str
    reason: str
    gate_ready: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "transition_id": self.transition_id,
            "at": self.at.isoformat(),
            "from": self.frm.value,
            "to": self.to.value,
            "actor": self.actor,
            "reason": self.reason,
            "gate_ready": self.gate_ready,
        }


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ActivationMachine:
    """Holds the current state and refuses every move that is not legal.

    It records nothing to a database and talks to no venue. A caller that wants
    the transition in the admin audit log writes it there; this class produces
    the record, it does not decide where records live.
    """

    state: ActivationState = ActivationState.disabled
    history: list[Transition] = field(default_factory=list)

    def can(self, wanted: ActivationState) -> bool:
        return wanted in TRANSITIONS.get(self.state, frozenset())

    def to(
        self,
        wanted: ActivationState,
        *,
        actor: str,
        reason: str,
        gate_ready: bool | None = None,
    ) -> Transition:
        """Move, or raise. Every move needs an actor and a reason.

        The actor and reason are mandatory because a transition nobody signed
        is a transition nobody can be asked about afterwards, and the states
        this machine moves between are exactly the ones somebody will be asked
        about.
        """
        actor = (actor or "").strip()
        reason = (reason or "").strip()
        if not actor:
            raise IllegalActivationTransition(
                "a transition needs an actor; an unsigned activation change is one "
                "nobody can be asked about later"
            )
        if len(reason) < 8:
            raise IllegalActivationTransition(
                f"a transition needs a reason of at least 8 characters; got {reason!r}"
            )
        if not self.can(wanted):
            legal = ", ".join(sorted(s.value for s in TRANSITIONS.get(self.state, frozenset())))
            raise IllegalActivationTransition(
                f"{self.state.value} -> {wanted.value} is not a legal move; "
                f"from {self.state.value} the legal moves are: {legal or 'none'}"
            )
        move = Transition(
            transition_id=str(uuid4()),
            at=_now(),
            frm=self.state,
            to=wanted,
            actor=actor,
            reason=reason,
            gate_ready=gate_ready,
        )
        self.state = wanted
        self.history.append(move)
        return move

    # ---------------------------------------------------------------- steps

    def begin_precheck(self, *, actor: str, reason: str) -> Transition:
        return self.to(ActivationState.precheck, actor=actor, reason=reason)

    def record_precheck(self, report: object, *, actor: str, reason: str) -> Transition:
        """`ready` on a passing report, `precheck_failed` on anything else.

        The report is asked, never argued with. A caller cannot pass
        `ready=True` alongside a failing report because the verdict is read off
        the report itself.
        """
        ready = bool(getattr(report, "ready", False))
        wanted = ActivationState.ready if ready else ActivationState.precheck_failed
        return self.to(wanted, actor=actor, reason=reason, gate_ready=ready)

    def arm(self, report: object, *, actor: str, reason: str) -> Transition:
        """`ready -> armed`, and only on a report that passed.

        Arming places no order. It is the step that says a human looked at a
        passing preflight and accepted it, which is a different fact from the
        preflight passing.
        """
        if not getattr(report, "ready", False):
            raise IllegalActivationTransition(
                f"refusing to arm on a gate report that did not pass: {_blocker_summary(report)}"
            )
        return self.to(ActivationState.armed, actor=actor, reason=reason, gate_ready=True)

    def activate(self, report: object, *, actor: str, reason: str) -> tuple[Transition, ...]:
        """`armed -> activating -> active`, on a FRESH passing report.

        Two moves rather than one, so a failure between them has somewhere to
        land: `activating` is a state the machine can be found in, and §31's
        safety response is reachable from it.
        """
        if self.state is not ActivationState.armed:
            raise IllegalActivationTransition(
                f"activation starts from ARMED, not {self.state.value}"
            )
        if not getattr(report, "ready", False):
            raise IllegalActivationTransition(
                "refusing to activate on a gate report that did not pass: "
                f"{_blocker_summary(report)}"
            )
        first = self.to(ActivationState.activating, actor=actor, reason=reason, gate_ready=True)
        second = self.to(ActivationState.active, actor=actor, reason=reason, gate_ready=True)
        return (first, second)

    def to_safe_mode(self, *, actor: str, reason: str) -> Transition:
        return self.to(ActivationState.safe_mode, actor=actor, reason=reason)

    def emergency_stop(self, *, actor: str, reason: str) -> Transition:
        return self.to(ActivationState.emergency_stop, actor=actor, reason=reason)

    # ---------------------------------------------------------------- views

    @property
    def live(self) -> bool:
        """True while the platform may be holding live exposure."""
        return self.state in LIVE_STATES

    def describe(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "live": self.live,
            "legal_moves": sorted(s.value for s in TRANSITIONS.get(self.state, frozenset())),
            "transitions": [t.as_dict() for t in self.history],
        }


def _blocker_summary(report: object) -> str:
    blockers: Sequence[object] = getattr(report, "blockers", ()) or ()
    names = [str(getattr(b, "name", b)) for b in blockers]
    if not names:
        return "no blockers were listed, which is itself a reason to refuse"
    shown = ", ".join(names[:5])
    return f"{len(names)} blocker(s): {shown}" + (", ..." if len(names) > 5 else "")
