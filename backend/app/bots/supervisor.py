"""Watching bots: stale heartbeats, orphans, recovery, and restart.

**"The database says RUNNING" is not evidence that a bot is running.** That is
section 26's sentence and it is the whole reason this module exists. A row
says what the last process to touch it believed; a heartbeat says something is
still alive. When the two disagree, the heartbeat is the one that is measured.

**Nothing here trades.** The supervisor moves runs between states and asks a
runner to restart one. It creates no order, holds no adapter, and imports
neither `app.oms` nor `app.brokers` — a test parses the package to prove it.

**Recovery is refused far more often than it is attempted.** A crashed bot is
restarted only when every one of these is true:

  * no kill switch is engaged, globally or for that bot's account or strategy;
  * the bot is not disabled;
  * the account has no unresolved order — an order the venue may be holding
    that nobody has settled makes a restart a second order waiting to happen;
  * the run is `crashed`, not `halted`. A kill switch is a decision somebody
    made, and recovering out of it automatically would be exactly the
    bot-level bypass section 22 forbids.

Anything else leaves the run `crashed` with the reason recorded, which is the
honest state: it needs a person.

**A restart resumes; it does not re-run.** `plan_restart` reads what each bot
was doing and says what should happen to it. A `stopped` bot stays stopped, a
`paused` bot stays paused, and a `running` bot is a bot whose process is gone
— it is marked `crashed` for the supervisor to consider, never silently
started. Section 29 asks for exactly that, and "do not blindly start every
bot" is the sentence that matters.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.budget import RecoveryBudget
from app.bots.state import ACTIVE, BotState, IllegalBotTransition, check_transition
from app.models.bots import Bot, BotEvent, BotRun

log = logging.getLogger("app.bots.supervisor")

#: How long a heartbeat may be silent before the run is treated as gone. Three
#: times the paper runner's own pass interval, so a slow pass is not mistaken
#: for a dead process -- the failure that would make a supervisor restart a
#: perfectly healthy bot and produce two of them.
DEFAULT_STALE_AFTER = timedelta(seconds=90)


@dataclass(frozen=True)
class Finding:
    """One thing the sweep noticed about one run."""

    run_id: str
    bot_id: str
    was: BotState
    now: BotState
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "bot_id": self.bot_id,
            "was": str(self.was),
            "now": str(self.now),
            "reason": self.reason,
        }


@dataclass
class SweepReport:
    """What one supervisory pass found and did."""

    at: datetime
    checked: int = 0
    stale: list[Finding] = field(default_factory=list)
    recovered: list[Finding] = field(default_factory=list)
    refused: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "checked": self.checked,
            "stale": [f.as_dict() for f in self.stale],
            "recovered": [f.as_dict() for f in self.recovered],
            "refused": [f.as_dict() for f in self.refused],
            "note": (
                "A heartbeat is measured; a status column is remembered. Where the two "
                "disagree the heartbeat wins, and a run whose recovery was refused is "
                "left crashed with the reason rather than restarted hopefully."
            ),
        }


@dataclass(frozen=True)
class RestartPlan:
    """What should happen to one bot after the application restarts."""

    bot_id: str
    run_id: str | None
    was: BotState | None
    action: str  # "leave" | "mark_crashed" | "resume_paused"
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "bot_id": self.bot_id,
            "run_id": self.run_id,
            "was": str(self.was) if self.was else None,
            "action": self.action,
            "reason": self.reason,
        }


#: Asked before a crashed run is restarted. Returns a refusal reason, or None
#: when it is safe. A callable rather than a service so the supervisor can be
#: driven from a worker, a request or a test without dragging one in.
SafetyCheck = Callable[[Bot], Awaitable[str | None]]


class BotSupervisor:
    """Sweeps bot runs. Moves state, asks a runner to restart. Trades nothing."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        safe_to_recover: SafetyCheck | None = None,
        restart: Callable[[Bot, BotRun], Awaitable[bool]] | None = None,
        # L51 Phase 19. The gates above answer "is recovery safe right now?";
        # none of them answers "how many times has this already been tried?".
        # A bot that crashes on start passes every gate every time.
        budget: RecoveryBudget | None = None,
    ) -> None:
        self.db = db
        self.budget = budget or RecoveryBudget()
        self.stale_after = stale_after
        # Default: refuse. A supervisor with no safety check must not be a
        # supervisor that recovers everything -- the absence of a check is not
        # evidence that recovery is safe.
        self.safe_to_recover = safe_to_recover or _refuse_by_default
        self.restart = restart

    # ==================================================================== sweep

    async def sweep(self, *, now: datetime | None = None) -> SweepReport:
        at = now or datetime.now(UTC)
        report = SweepReport(at=at)

        runs = await self._active_runs()
        report.checked = len(runs)
        for run, bot in runs:
            finding = await self._check_heartbeat(run, bot, at)
            if finding is not None:
                report.stale.append(finding)

        # Recovery is a second pass over what the first pass just marked, plus
        # anything that was already crashed. Separate on purpose: marking a
        # run dead and deciding to restart it are different decisions, and
        # doing both in one loop makes the second invisible.
        for run, bot in await self._crashed_runs():
            outcome = await self._consider_recovery(run, bot, at)
            if outcome is None:
                continue
            (report.recovered if outcome.now is BotState.recovering else report.refused).append(
                outcome
            )

        await self.db.flush()
        return report

    async def _check_heartbeat(self, run: BotRun, bot: Bot, at: datetime) -> Finding | None:
        """A run whose heartbeat has stopped is a run whose process is gone."""
        beat = run.last_heartbeat_at
        if beat is None:
            # Never beat. That is only suspicious once it has had time to.
            reference = run.started_at
        else:
            reference = beat
        if reference is None:  # pragma: no cover - started_at is NOT NULL
            return None
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=at.tzinfo)
        if at - reference <= self.stale_after:
            return None

        was = BotState(run.status)
        age = (at - reference).total_seconds()
        reason = (
            f"no heartbeat for {age:.0f}s against a "
            f"{self.stale_after.total_seconds():.0f}s limit; the process is gone"
        )
        moved = self._move(run, BotState.crashed, at, reason)
        if not moved:
            return None
        await self._event(run.id, "heartbeat_stale", at, {"detail": reason, "was": str(was)})
        log.error(
            "a bot run stopped beating; it is marked crashed, not restarted",
            extra={
                "event": "bot_heartbeat_stale",
                "run_id": run.id,
                "bot_id": bot.id,
                "was": str(was),
                "age_seconds": round(age),
            },
        )
        return Finding(run.id, bot.id, was, BotState.crashed, reason)

    async def _recovery_attempts(self, bot: Bot, since: datetime) -> tuple[int, datetime | None]:
        """How often this BOT has been restarted automatically, and when last.

        Counted per bot rather than per run, because each attempt produces a
        new run: a per-run count is always one and would prove nothing. The
        source is `bot_events`, which already records `recovery_started`, so
        there is no second counter to drift out of step with it.
        """
        rows = (
            await self.db.scalars(
                select(BotEvent.occurred_at)
                .join(BotRun, BotRun.id == BotEvent.bot_run_id)
                .where(
                    BotRun.bot_id == bot.id,
                    BotEvent.event_type == "recovery_started",
                    BotEvent.occurred_at >= since.replace(tzinfo=None),
                )
                .order_by(BotEvent.occurred_at.desc())
            )
        ).all()
        if not rows:
            return 0, None
        latest = rows[0]
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=UTC)
        return len(rows), latest

    async def _consider_recovery(self, run: BotRun, bot: Bot, at: datetime) -> Finding | None:
        """Restart a crashed run, but only when every gate agrees."""
        refusal = await self.safe_to_recover(bot)
        if refusal is None:
            # **L51 Phase 19.** The safety gates all ask whether recovery is
            # safe now; this asks whether it has already been tried enough.
            # Runs last so a genuine safety refusal keeps its own message --
            # "the platform is in safe mode" is more useful than "out of
            # budget", and both are true.
            attempts, last = await self._recovery_attempts(bot, at - self.budget.window)
            refusal = self.budget.refusal(attempts_in_window=attempts, last_attempt_at=last, now=at)
            if refusal is not None:
                await self._event(
                    run.id,
                    "recovery_budget_exhausted",
                    at,
                    {"detail": refusal, "attempts": attempts},
                )
                log.warning(
                    "automatic recovery is out of budget; the run stays crashed and needs a person",
                    extra={
                        "event": "bot_recovery_budget_exhausted",
                        "run_id": run.id,
                        "bot_id": bot.id,
                        "attempts": attempts,
                        "reason": refusal[:300],
                    },
                )
                return Finding(run.id, bot.id, BotState.crashed, BotState.crashed, refusal)
        if refusal is not None:
            await self._event(run.id, "recovery_refused", at, {"detail": refusal})
            log.warning(
                "recovery refused; the run stays crashed and needs a person",
                extra={
                    "event": "bot_recovery_refused",
                    "run_id": run.id,
                    "bot_id": bot.id,
                    "reason": refusal[:300],
                },
            )
            return Finding(run.id, bot.id, BotState.crashed, BotState.crashed, refusal)

        if not self._move(run, BotState.recovering, at, "supervisor is restarting it"):
            return None
        await self._event(run.id, "recovery_started", at, {"detail": "supervisor restart"})

        if self.restart is None:
            # Nothing to restart it WITH. Honest: the run is marked recovering
            # and a caller with a runner finishes the job.
            return Finding(
                run.id,
                bot.id,
                BotState.crashed,
                BotState.recovering,
                "marked for recovery; no runner is wired to this supervisor",
            )
        try:
            started = await self.restart(bot, run)
        except Exception as exc:  # noqa: BLE001
            self._move(run, BotState.crashed, at, f"restart raised: {exc}")
            await self._event(run.id, "recovery_failed", at, {"detail": str(exc)[:300]})
            return Finding(run.id, bot.id, BotState.recovering, BotState.crashed, str(exc)[:300])

        if not started:
            self._move(run, BotState.crashed, at, "the runner declined to restart it")
            await self._event(run.id, "recovery_failed", at, {"detail": "runner declined"})
            return Finding(
                run.id, bot.id, BotState.recovering, BotState.crashed, "the runner declined"
            )

        await self._event(run.id, "recovery_completed", at, {"detail": "restarted"})
        return Finding(run.id, bot.id, BotState.crashed, BotState.recovering, "restarted")

    # ============================================================ restart plan

    async def plan_restart(self, *, now: datetime | None = None) -> list[RestartPlan]:
        """What should happen to each bot after the application restarts.

        It PLANS and does not act, so a caller can log the whole plan before
        any of it happens. Section 29's rule is the one this encodes: a
        `stopped` bot stays stopped, a `paused` bot stays paused, and a
        `running` bot is one whose process is gone — marked crashed for the
        supervisor to consider, never silently started.
        """
        at = now or datetime.now(UTC)
        plans: list[RestartPlan] = []
        for run, bot in await self._latest_runs():
            state = BotState(run.status) if run is not None else None
            if bot.is_disabled:
                plans.append(
                    RestartPlan(
                        bot.id, run.id if run else None, state, "leave", "the bot is disabled"
                    )
                )
                continue
            if run is None or state in (BotState.stopped, BotState.halted, BotState.disabled):
                plans.append(
                    RestartPlan(
                        bot.id,
                        run.id if run else None,
                        state,
                        "leave",
                        "the run is over; a restart does not revive it",
                    )
                )
                continue
            if state is BotState.paused:
                plans.append(
                    RestartPlan(
                        bot.id,
                        run.id,
                        state,
                        "resume_paused",
                        "somebody paused this deliberately; it stays paused",
                    )
                )
                continue
            # starting, running, stopping, crashed, recovering: the process
            # that owned it is gone, whatever the row says.
            reason = (
                f"the row says {state} but this process did not start it; "
                "marked crashed for the supervisor to consider"
            )
            self._move(run, BotState.crashed, at, reason)
            await self._event(run.id, "restart_orphan", at, {"detail": reason, "was": str(state)})
            plans.append(RestartPlan(bot.id, run.id, state, "mark_crashed", reason))
        await self.db.flush()
        return plans

    # =============================================================== internals

    def _move(self, run: BotRun, wanted: BotState, at: datetime, reason: str) -> bool:
        """Apply a transition, or record that it was not legal and change nothing."""
        current = BotState(run.status)
        if current is wanted:
            return False
        try:
            check_transition(current, wanted)
        except IllegalBotTransition as exc:
            log.warning(
                "the supervisor tried an illegal bot transition; nothing changed",
                extra={"event": "bot_transition_refused", "run_id": run.id, "reason": str(exc)},
            )
            return False
        run.status = str(wanted)
        if wanted in (BotState.crashed, BotState.halted, BotState.stopped):
            run.ended_at = run.ended_at or at.replace(tzinfo=None)
            run.stop_reason = run.stop_reason or reason[:500]
        return True

    async def _active_runs(self) -> list[tuple[BotRun, Bot]]:
        rows = (
            await self.db.execute(
                select(BotRun, Bot)
                .join(Bot, Bot.id == BotRun.bot_id)
                .where(BotRun.status.in_(sorted(str(s) for s in ACTIVE)))
                .order_by(BotRun.started_at)
            )
        ).all()
        return [(run, bot) for run, bot in rows]

    async def _crashed_runs(self) -> list[tuple[BotRun, Bot]]:
        rows = (
            await self.db.execute(
                select(BotRun, Bot)
                .join(Bot, Bot.id == BotRun.bot_id)
                .where(BotRun.status == str(BotState.crashed))
                .order_by(BotRun.started_at)
            )
        ).all()
        return [(run, bot) for run, bot in rows]

    async def _latest_runs(self) -> list[tuple[BotRun | None, Bot]]:
        """Every bot, with its most recent run if it has one."""
        bots = list((await self.db.scalars(select(Bot).order_by(Bot.created_at))).all())
        out: list[tuple[BotRun | None, Bot]] = []
        for bot in bots:
            run = await self.db.scalar(
                select(BotRun)
                .where(BotRun.bot_id == bot.id)
                .order_by(BotRun.started_at.desc())
                .limit(1)
            )
            out.append((run, bot))
        return out

    async def _event(self, run_id: str, event_type: str, at: datetime, payload: dict) -> None:
        self.db.add(
            BotEvent(
                bot_run_id=run_id,
                event_type=event_type[:32],
                level="error" if "fail" in event_type or "stale" in event_type else "info",
                occurred_at=at.replace(tzinfo=None) if at.tzinfo else at,
                payload=payload,
            )
        )


async def _refuse_by_default(bot: Bot) -> str | None:
    """The safety check a supervisor gets when nobody supplied one.

    It refuses. The absence of a check is not evidence that recovery is safe,
    and a supervisor that restarted everything because nobody told it not to
    would be the most dangerous default in the system.
    """
    return (
        "no safety check is wired to this supervisor, so recovery cannot be shown "
        "to be safe; refusing rather than restarting hopefully"
    )
