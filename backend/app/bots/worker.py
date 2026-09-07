"""The supervisor as a background loop.

A `app.workers.Worker` — the same supervised loop with a heartbeat every other
background job uses. Not a second worker system, scheduler or queue.

**It watches the watchers.** Every other worker on this platform reports a
heartbeat that something must read; this is the thing that reads the bot runs'.
Its own heartbeat is read by the health check, in exactly the same way, which
is what stops the supervision chain from ending in something nobody watches.

**It changes state and asks; it never trades.** A sweep marks a silent run
crashed and asks a runner to restart one. It creates no order, holds no
adapter, and the package it lives in imports neither `app.oms` nor
`app.brokers` — a test parses every module to prove it.

**Refusing is the default.** With no safety check wired, every recovery is
refused with that as the reason. A supervisor that restarted everything
because nobody told it not to would be the most dangerous default in the
system, so the absence of a check is treated as absence of evidence rather
than as permission.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bots.supervisor import DEFAULT_STALE_AFTER, BotSupervisor, SafetyCheck, SweepReport
from app.workers.base import Worker

log = logging.getLogger("app.bots.worker")


class BotSupervisorWorker(Worker):
    """Sweeps bot runs on an interval, forever."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        interval_seconds: float = 30.0,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        safe_to_recover: SafetyCheck | None = None,
        name: str = "bot_supervisor",
    ) -> None:
        super().__init__(name=name, interval_seconds=interval_seconds)
        self.sessions = sessions
        self.stale_after = stale_after
        self.safe_to_recover = safe_to_recover
        self.last_report: SweepReport | None = None

    async def tick(self) -> None:
        async with self.sessions() as db:
            supervisor = BotSupervisor(
                db, stale_after=self.stale_after, safe_to_recover=self.safe_to_recover
            )
            report = await supervisor.sweep()
            await db.commit()
        self.last_report = report
        if report.stale or report.refused:
            log.warning(
                "the bot sweep found runs that need attention",
                extra={
                    "event": "bot_sweep_findings",
                    "checked": report.checked,
                    "stale": len(report.stale),
                    "refused": len(report.refused),
                    "recovered": len(report.recovered),
                },
            )

    def report(self) -> dict[str, object]:
        """The last sweep, plus this worker's own heartbeat.

        Named `report` rather than `status` because `Worker.status` is already
        the heartbeat property the registry reads, and shadowing it would make
        this worker lie to the thing that watches it.
        """
        return {
            "worker": self.name,
            "supervisor": self.status.as_dict(),
            "stale_after_seconds": self.stale_after.total_seconds(),
            "recovery": ("wired" if self.safe_to_recover is not None else "refused by default"),
            "last_sweep": self.last_report.as_dict() if self.last_report else None,
            "authority": (
                "This worker changes run state and asks a runner to restart one. It "
                "creates no order and holds no adapter."
            ),
        }
