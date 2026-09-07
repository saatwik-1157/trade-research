"""The recovery vocabulary: states, safe-mode reasons, and step results.

Step 4 asks every subsystem to define a normal, degraded, unavailable,
recovering and safe state, and step 4 also states the rule that shapes the
whole level:

    **Never jump from DISCONNECTED to EXECUTING without reconciliation.**

`RecoveryState` is that ladder as an enum, and `RECONCILING` sits between
`RECONNECTING` and `RECOVERED` because it is the step the rule exists to make
unskippable.

**This module names things. It cannot do any of them.** No session, no
adapter, no service. `app/recovery` as a whole imports no broker adapter class
and no order manager class -- it is handed registries and calls the
reconcilers those already own, and a parse test enumerates the package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class RecoveryState(StrEnum):
    """Step 4. The ladder, and the order it must be climbed in."""

    normal = "NORMAL"
    degraded = "DEGRADED"
    unavailable = "UNAVAILABLE"
    reconnecting = "RECONNECTING"
    #: The step that cannot be skipped. Between "the link is back" and "we may
    #: trade" there is always a comparison of what we believe against what the
    #: venue holds.
    reconciling = "RECONCILING"
    recovered = "RECOVERED"
    #: Not an error state. A deliberate, latched refusal to start new work,
    #: with a reason, while everything observational keeps running.
    safe_mode = "SAFE_MODE"
    #: Not observed. Never treated as normal -- the same rule L37 applies to a
    #: component nobody could probe.
    unknown = "UNKNOWN"


#: States in which new work must not be started. `unknown` is here on purpose:
#: not knowing is not permission.
BLOCKING: frozenset[RecoveryState] = frozenset(
    {
        RecoveryState.unavailable,
        RecoveryState.reconnecting,
        RecoveryState.reconciling,
        RecoveryState.safe_mode,
        RecoveryState.unknown,
    }
)


class SafeModeReason(StrEnum):
    """Step 18. Why safe mode is on. Never "an error occurred".

    Each of these is a condition the platform can actually observe, and each
    is recorded with a detail string when the latch closes. Step 18 also says
    not to use safe mode to hide errors: a reason that did not name the
    condition would be doing exactly that.
    """

    startup_reconciliation_failed = "STARTUP_RECONCILIATION_FAILED"
    unknown_order_state = "UNKNOWN_ORDER_STATE"
    position_mismatch = "POSITION_MISMATCH"
    broker_unreachable = "BROKER_UNREACHABLE"
    stale_market_data = "STALE_MARKET_DATA"
    database_inconsistent = "DATABASE_INCONSISTENT"
    recovery_failed = "RECOVERY_FAILED"
    #: Somebody decided. The one reason that is not derived from a check.
    operator = "OPERATOR"


class StepStatus(StrEnum):
    """What one startup or reconciliation step concluded."""

    ok = "OK"
    #: It ran and found something that needs a person. Not a crash.
    attention = "ATTENTION"
    #: It could not run. Distinguished from `attention` because "we did not
    #: check" and "we checked and it is wrong" are different facts, and the
    #: first must never be reported as the second or as OK.
    skipped = "SKIPPED"
    failed = "FAILED"


@dataclass(frozen=True)
class Step:
    """One step of the startup or reconciliation sequence, and what it found."""

    name: str
    status: StepStatus
    detail: str
    at: datetime
    duration_ms: float | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status in (StepStatus.attention, StepStatus.failed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.name,
            "status": str(self.status),
            "detail": self.detail,
            "at": self.at.isoformat(),
            "duration_ms": self.duration_ms,
            "blocking": self.blocking,
            "facts": dict(self.facts),
        }


@dataclass
class RecoveryReport:
    """The result of a startup sequence or a reconciliation sweep."""

    kind: str
    at: datetime
    steps: list[Step] = field(default_factory=list)
    safe_mode_engaged: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not any(step.blocking for step in self.steps)

    @property
    def attention(self) -> list[Step]:
        return [s for s in self.steps if s.blocking]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "clean": self.clean,
            "steps": [s.as_dict() for s in self.steps],
            "needs_attention": [s.as_dict() for s in self.attention],
            "safe_mode_engaged": list(self.safe_mode_engaged),
            "authority": (
                "reconciliation REPORTS. Nothing in this report was repaired "
                "automatically: a position the venue holds and we do not is never "
                "closed, and an order we sent and cannot account for is never "
                "re-sent."
            ),
        }


#: Step 17's ordering, as data. Served by the API so the sequence is checkable
#: rather than described, and read by `startup.py` so the two cannot disagree.
STARTUP_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("configuration", "settings load and the live-trading gates are read"),
    ("database", "connectivity, through the same check /health/ready uses"),
    ("migrations", "the schema the models expect is the schema that is there"),
    ("redis", "connectivity; the event bus reports which transport it chose"),
    ("event_bus", "the hub subscribed and its reader is running"),
    ("market_data", "provider usability and the age of the newest stored bar"),
    ("webhook_gateway", "whether a shared secret is configured at all"),
    ("broker", "adapters registered and what each reports about its link"),
    ("broker_reconciliation", "venue positions and orders against ours"),
    ("oms_reconciliation", "orders whose venue state was never established"),
    ("position_reconciliation", "local positions against the venue's"),
    ("risk_engine", "loaded, and which kill switches are engaged"),
    ("bot_recovery", "what each bot was doing, and what should happen to it"),
    ("notifications", "channels and the delivery queue"),
    ("monitoring", "the observability collector"),
    ("operational_mode", "the mode this process will run in, and why"),
)


__all__ = [
    "BLOCKING",
    "STARTUP_SEQUENCE",
    "RecoveryReport",
    "RecoveryState",
    "SafeModeReason",
    "Step",
    "StepStatus",
]
