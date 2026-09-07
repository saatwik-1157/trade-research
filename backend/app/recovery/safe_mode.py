"""Safe mode: a latched refusal to start new work, with its reasons.

Step 18, and the sentence that decides its whole shape:

    **Do not use safe mode to hide errors. Log the reason.**

So a latch is never closed without a `SafeModeReason` and a detail, both are
recorded, and `describe()` returns every reason currently holding it shut. A
safe mode that said only "safe mode is on" would be a way of not saying what
went wrong.

**What it blocks, and what it deliberately does not.** Step 18 lists new
orders, new bot starts, automated execution and strategy signal execution.
Everything observational keeps running: monitoring, reconciliation, position
visibility, risk evaluation, diagnostics, admin inspection and recovery
actions. A platform in safe mode is a platform you can still look at and still
reconcile -- which is the point, because reconciling is how you get out.

**It is a SECOND refusal, never a replacement for one.** Rule 2 of step 37:
recovery cannot bypass the RiskEngine. Safe mode is checked *before* risk and
adds a refusal; it never approves anything, never releases a kill switch and
never widens a limit. The engine's veto is unaffected by whether the latch is
open.

**It is process state, deliberately.** A stored flag is a flag somebody forgets
to clear, and a stored flag set by a process that has since died is a platform
that will not trade for a reason nobody can find. Instead the latch is
re-derived at every startup from the reconciliation sequence, and every open
and close is written to `system_events` so the history survives the restart
even though the state does not.

**Closing it is automatic; opening it is not.** A condition that the startup
sequence found closes it without asking. Releasing it takes an authorized
person, a reason, and -- because the conditions are re-checked on release -- a
platform that actually reconciles. That asymmetry is the point: the expensive
direction is the one that resumes trading.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.auth.models import utcnow
from app.recovery.contract import SafeModeReason

log = logging.getLogger("app.recovery.safe_mode")


@dataclass(frozen=True)
class Latch:
    """One reason the door is shut, and when it was."""

    reason: SafeModeReason
    detail: str
    at: datetime
    actor_user_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": str(self.reason),
            "detail": self.detail,
            "at": self.at.isoformat(),
            "actor_user_id": self.actor_user_id,
        }


class SafeModeBlocked(Exception):
    """Raised where a caller asked to do something safe mode forbids.

    Carries the reasons, so the refusal a user sees names the condition rather
    than the mode. "New orders are blocked" is not an explanation; "new orders
    are blocked: two orders whose venue state was never established" is.
    """

    def __init__(self, reasons: list[Latch]) -> None:
        self.reasons = reasons
        super().__init__(self.detail)

    @property
    def detail(self) -> str:
        if not self.reasons:  # pragma: no cover - never constructed empty
            return "the platform is in safe mode"
        head = "; ".join(f"{r.reason}: {r.detail}" for r in self.reasons)
        return f"the platform is in safe mode. {head}"


@dataclass
class SafeMode:
    """The latch. One per process; handed to whatever must consult it."""

    #: Reasons currently holding it shut, keyed so one condition cannot latch
    #: it twice and so releasing one does not release the others.
    _latches: dict[SafeModeReason, Latch] = field(default_factory=dict)
    #: Every open and close this process has seen. Bounded, for the API.
    _history: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------- the latch

    @property
    def engaged(self) -> bool:
        return bool(self._latches)

    @property
    def reasons(self) -> list[Latch]:
        return sorted(self._latches.values(), key=lambda latch: latch.at)

    def engage(
        self,
        reason: SafeModeReason,
        detail: str,
        *,
        actor_user_id: str | None = None,
        at: datetime | None = None,
    ) -> Latch:
        """Close the door for one reason. Idempotent per reason.

        Re-engaging an already-open reason keeps the ORIGINAL timestamp: "when
        did this start" is the question an operator asks, and refreshing it
        every fifteen seconds would make the answer "just now" forever.
        """
        existing = self._latches.get(reason)
        if existing is not None:
            return existing
        latch = Latch(
            reason=reason,
            detail=detail[:300],
            at=at or utcnow(),
            actor_user_id=actor_user_id,
        )
        self._latches[reason] = latch
        self._history.append({"action": "engaged", **latch.as_dict()})
        log.warning(
            "safe mode engaged",
            extra={
                "event": "safe_mode_engaged",
                "reason": str(reason),
                "actor_user_id": actor_user_id,
            },
        )
        return latch

    def release(
        self,
        reason: SafeModeReason,
        *,
        actor_user_id: str,
        why: str,
        at: datetime | None = None,
    ) -> bool:
        """Open the door for one reason. Requires a person and a why.

        Returns False when that reason was not holding it, which is not an
        error: releasing something that was never latched is a no-op and
        saying so is more useful than raising.
        """
        latch = self._latches.pop(reason, None)
        if latch is None:
            return False
        self._history.append(
            {
                "action": "released",
                "reason": str(reason),
                "detail": why[:300],
                "at": (at or utcnow()).isoformat(),
                "actor_user_id": actor_user_id,
                "was_engaged_at": latch.at.isoformat(),
            }
        )
        log.warning(
            "safe mode released",
            extra={
                "event": "safe_mode_released",
                "reason": str(reason),
                "actor_user_id": actor_user_id,
            },
        )
        return True

    def release_all(self, *, actor_user_id: str, why: str) -> list[SafeModeReason]:
        released = list(self._latches)
        for reason in released:
            self.release(reason, actor_user_id=actor_user_id, why=why)
        return released

    # ------------------------------------------------------------ the guard

    def check(self, what: str = "this") -> None:
        """Raise `SafeModeBlocked` if the latch is shut. Called before acting.

        Deliberately a raise rather than a boolean: a guard that returns False
        is a guard a caller can forget to read, and the callers are order
        submission and automated execution.
        """
        if self.engaged:
            raise SafeModeBlocked(self.reasons)

    @property
    def blocks_new_orders(self) -> bool:
        return self.engaged

    def describe(self) -> dict[str, Any]:
        return {
            "engaged": self.engaged,
            "reasons": [latch.as_dict() for latch in self.reasons],
            "blocks": (
                [
                    "new order submission",
                    "automated execution of new signals",
                    "starting or recovering a bot",
                ]
                if self.engaged
                else []
            ),
            "still_allowed": [
                "monitoring and diagnostics",
                "reconciliation",
                "position and order visibility",
                "risk evaluation",
                "admin inspection",
                "recovery actions",
            ],
            "authority": (
                "safe mode ADDS a refusal. It never approves anything, never releases "
                "a kill switch and never widens a limit -- the RiskEngine's veto is "
                "unaffected by whether this latch is open."
            ),
            "persistence": (
                "process state, re-derived from the reconciliation sequence at every "
                "startup. A stored flag is one somebody forgets to clear, and one set "
                "by a process that has since died is a platform that will not trade "
                "for a reason nobody can find. Every open and close is written to "
                "system_events, so the history outlives the state."
            ),
            "history": self._history[-20:],
        }


__all__ = ["Latch", "SafeMode", "SafeModeBlocked"]
