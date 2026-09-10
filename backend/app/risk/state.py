"""Risk state: the latched locks, and the rules for getting out of them.

A limit breach is not a moment, it is a **state**. Once the daily loss limit is
hit, the account is locked -- not "the next order fails the same check". The
difference matters because a check is recomputed from a portfolio snapshot and
a snapshot can move: a losing day followed by an unrealised bounce would let
trading resume on a limit that was already breached, which is exactly the
behaviour a daily loss limit exists to prevent.

So the locks latch, they are persisted, and **they do not clear themselves**:

| state | clears when |
|---|---|
| `daily_loss_locked` | the configured trading-day boundary passes -- and only then |
| `drawdown_locked` | an authorised human clears it. A drawdown does not expire |
| `emergency_stop` | an authorised human clears it |
| `disabled` | an authorised human re-enables the account |

A strategy cannot unlock any of them, and neither can a restart. `RiskService`
reloads them from the database before it evaluates anything, so a process that
died holding a lock comes back holding it.

The trading-day boundary is **explicit UTC**, never the machine's local
midnight. This repository has already shipped that bug: `.replace(hour=0)` on a
server-rendered stamp counted the day from 18:30 on a UTC+5:30 laptop and from
midnight on a UTC one, so a risk limit moved with the operator's timezone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.risk.decision import RejectionCode


class RiskState(StrEnum):
    normal = "NORMAL"
    warning = "WARNING"
    daily_loss_locked = "DAILY_LOSS_LOCKED"
    #: Its own state, not a reuse of the daily one. `refresh()` clears a
    #: daily lock when the DAY ends, so a week latched there would
    #: release itself overnight while the week was still breached.
    weekly_loss_locked = "WEEKLY_LOSS_LOCKED"
    drawdown_locked = "DRAWDOWN_LOCKED"
    emergency_stop = "EMERGENCY_STOP"
    disabled = "DISABLED"


# States in which no new order may be created, and the code each one reports.
BLOCKING: dict[RiskState, RejectionCode] = {
    RiskState.daily_loss_locked: RejectionCode.daily_loss_locked,
    RiskState.weekly_loss_locked: RejectionCode.weekly_loss_locked,
    RiskState.drawdown_locked: RejectionCode.drawdown_locked,
    RiskState.emergency_stop: RejectionCode.emergency_stop,
    RiskState.disabled: RejectionCode.account_disabled,
}

# States that only an authorised human clears. A drawdown lock is deliberately
# here and not on a timer: the drawdown is still there tomorrow.
# A weekly lock is in here and a daily one is not, and the difference is
# that `refresh()` can prove a DAY has ended. It cannot prove a week has:
# it would have to know which day the broker's week starts on, and that
# varies by venue. So the weekly lock is released by a person who has
# looked, which is the right amount of friction for a limit that took a
# week to breach.
NEEDS_AUTHORISED_RESET = frozenset(
    {
        RiskState.weekly_loss_locked,
        RiskState.drawdown_locked,
        RiskState.emergency_stop,
        RiskState.disabled,
    }
)


class IllegalRiskTransition(Exception):
    """A move the risk state machine does not allow."""


class ResetNotPermitted(Exception):
    """An attempt to clear a lock that has not earned its release."""


def day_start(now: datetime, *, boundary_hour: int = 0) -> datetime:
    """The start of the current trading day, in UTC.

    `boundary_hour` shifts the boundary for venues whose day does not start at
    UTC midnight -- the broker's rollover, typically. It is a configured number,
    never read from the machine.
    """
    if not 0 <= boundary_hour <= 23:
        raise ValueError(f"boundary_hour must be 0-23, not {boundary_hour}")
    moment = now.astimezone(UTC)
    start = moment.replace(hour=boundary_hour, minute=0, second=0, microsecond=0)
    if moment < start:
        start -= timedelta(days=1)
    return start


@dataclass
class AccountRiskState:
    """One account's latched risk state. Persisted; never rebuilt by guessing."""

    scope: str  # "global" | "paper_account" | "broker_account" | "strategy"
    scope_ref: str | None
    state: RiskState = RiskState.normal
    reason: str = ""
    since: datetime | None = None
    set_by: str | None = None
    # For a daily lock: the trading day it belongs to. It clears when that day
    # is over, and a lock with no day recorded never clears on its own.
    locked_day: datetime | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.state in BLOCKING

    @property
    def code(self) -> RejectionCode | None:
        return BLOCKING.get(self.state)

    def lock(
        self,
        state: RiskState,
        reason: str,
        at: datetime,
        *,
        set_by: str | None = None,
        day: datetime | None = None,
    ) -> AccountRiskState:
        if state not in BLOCKING:
            raise IllegalRiskTransition(f"{state} is not a locking state")
        # A stronger lock replaces a weaker one; a weaker one never displaces a
        # stronger. Emergency stop outranks everything.
        if self.state is RiskState.emergency_stop and state is not RiskState.emergency_stop:
            return self
        self.history.append(self._entry(at, f"lock -> {state}", reason))
        self.state = state
        self.reason = reason[:500]
        self.since = at
        self.set_by = set_by
        self.locked_day = day
        return self

    def clear(
        self, at: datetime, *, authorised_by: str | None = None, force: bool = False
    ) -> AccountRiskState:
        """Release a lock. Refuses unless the release has been earned.

        `force` is the authorised-human path and requires `authorised_by`, so a
        release always has a name attached to it in the history.
        """
        if not self.blocking:
            return self
        if self.state in NEEDS_AUTHORISED_RESET and not force:
            raise ResetNotPermitted(
                f"a {self.state} lock does not clear on its own; it requires an authorised reset"
            )
        if force and not authorised_by:
            raise ResetNotPermitted("an authorised reset must record who authorised it")
        self.history.append(
            self._entry(at, f"clear <- {self.state}", authorised_by or "boundary passed")
        )
        self.state = RiskState.normal
        self.reason = ""
        self.since = at
        self.set_by = authorised_by
        self.locked_day = None
        return self

    def refresh(self, now: datetime, *, boundary_hour: int = 0) -> AccountRiskState:
        """Clear a daily lock whose trading day has ended. Nothing else.

        Called before every evaluation. It is the ONLY automatic release in the
        module, and it exists because a daily loss limit is defined per day --
        a lock that outlived its day would be a different limit.
        """
        if self.state is not RiskState.daily_loss_locked:
            return self
        if self.locked_day is None:
            # No day recorded: it cannot be shown to have ended, so it stands.
            return self
        if day_start(now, boundary_hour=boundary_hour) > self.locked_day:
            self.clear(now)
        return self

    def _entry(self, at: datetime, action: str, detail: str) -> dict:
        return {
            "at": at.isoformat(),
            "action": action,
            "detail": detail[:300],
            "from": str(self.state),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "state": str(self.state),
            "blocking": self.blocking,
            "code": str(self.code) if self.code else None,
            "reason": self.reason,
            "since": self.since.isoformat() if self.since else None,
            "set_by": self.set_by,
            "locked_day": self.locked_day.isoformat() if self.locked_day else None,
            "clears": (
                "an authorised reset"
                if self.state in NEEDS_AUTHORISED_RESET
                else "the trading-day boundary"
                if self.state is RiskState.daily_loss_locked
                else "n/a"
            ),
            "history": self.history[-10:],
        }
