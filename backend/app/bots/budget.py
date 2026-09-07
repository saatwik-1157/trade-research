"""How often an autonomous recovery may be attempted. **L51 Phase 19.**

`BotSupervisor` restarts a crashed run when every safety gate agrees. The gates
are good -- safe mode, kill switches, unresolved orders, unsettled positions, an
unusable adapter -- and they are all about whether recovery is *safe right now*.
**None of them is about how many times it has already been tried.**

That is the gap this module closes, and the failure it prevents is specific: a
bot that crashes immediately on start (a bad configuration, a strategy that
raises on its first bar) passes every gate every time. Each restart produces a
new run, that run crashes, the next sweep finds a new crashed run, and it
restarts again -- for as long as nobody is watching, at the sweep interval.

**It is latent today and this is deliberately fixed before it is not.**
`BotSupervisorWorker.tick()` constructs its supervisor without a `restart`
callable, so nothing actually restarts a bot in the deployed application; the
run is marked `recovering` and the supervisor says honestly that no runner is
wired to it. Wiring one is what L51 Phase 9 asks for, and wiring one without a
budget is what creates the loop.

**Attempts are counted per BOT, not per run.** Counting per run would always
return one and prove nothing: the whole point is that each attempt creates a
*new* run. The count comes from `bot_events`, which already records
`recovery_started` -- no new table, no new counter to drift.

**A refusal is not a failure.** L51 Phase 27 says it plainly: a system that
escalates appropriately is better than one that performs unsafe recovery. An
exhausted budget means a person should look at this bot, and the event says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: Three attempts in an hour. Three rather than one because a single transient
#: failure -- a database blip, a broker that was briefly unreachable -- is
#: exactly what automatic recovery is for, and stopping after one would make
#: the mechanism useless. Rather than ten because a bot that has crashed three
#: times in an hour is not having a transient problem, and the fourth restart
#: is not the one that fixes it.
#:
#: These are configuration decisions, not measurements: no bot has ever
#: crashed on this platform, so there is no observed distribution to fit. They
#: are recorded here as the assumption they are.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_WINDOW = timedelta(hours=1)
#: Long enough that a bot which dies on startup cannot spin. The sweep runs
#: every 30s by default, so without this a crash-on-start loops twice a minute.
DEFAULT_COOLDOWN = timedelta(minutes=5)


@dataclass(frozen=True)
class RecoveryBudget:
    """The bound on autonomous restarts for one bot.

    Both limits must pass. They stop different things: `cooldown` stops a fast
    loop, `max_attempts` stops a slow one, and a bot that crashes every six
    minutes would defeat either on its own.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    window: timedelta = DEFAULT_WINDOW
    cooldown: timedelta = DEFAULT_COOLDOWN

    def refusal(
        self, *, attempts_in_window: int, last_attempt_at: datetime | None, now: datetime
    ) -> str | None:
        """Why recovery must not be attempted, or None.

        Returns a sentence rather than a boolean for the same reason every
        other gate in this platform does: the refusal an operator reads has to
        name the condition, or the next person rediscovers it.
        """
        if last_attempt_at is not None:
            since = now - last_attempt_at
            if since < self.cooldown:
                remaining = self.cooldown - since
                return (
                    f"the last automatic restart was {since.total_seconds():.0f}s ago and "
                    f"the cooldown is {self.cooldown.total_seconds():.0f}s; waiting "
                    f"{remaining.total_seconds():.0f}s more. A bot that fails on start "
                    "restarts as fast as it is swept, and that is a loop rather than a "
                    "recovery"
                )
        if attempts_in_window >= self.max_attempts:
            hours = self.window.total_seconds() / 3600
            return (
                f"this bot has been restarted automatically {attempts_in_window} time(s) "
                f"in the last {hours:.0f}h, which is its budget of {self.max_attempts}. "
                "A bot that has crashed this often is not having a transient problem, "
                "so it stays crashed and needs a person"
            )
        return None


__all__ = [
    "DEFAULT_COOLDOWN",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_WINDOW",
    "RecoveryBudget",
]
