"""The collection loop. One worker, on L02's base.

Sections 15, 66, 67 and 75.

It ticks, collects and commits. It restarts nothing, reconnects nothing and
retries nothing -- section 47 is explicit that L37 detects and L38 recovers,
and a monitor that also restarts the thing it is watching cannot tell you it
failed to restart it.

**It cannot take the process down.** `Worker.run` already catches a failing
tick, records it and keeps looping, because a worker that dies stops watching.
`ObservabilityService.collect` catches per-probe on top of that, so one broken
collector degrades one component rather than ending the pass.

**It is cheap on purpose.** Section 75: monitoring must not become a major
load. One pass is a handful of indexed counts, the health checks
`/health/ready` already runs, and some in-memory reads -- every fifteen
seconds. There is no per-tick market data call and no full table scan.
"""

from __future__ import annotations

import logging
from typing import Any

from app.observability.service import ObservabilityService
from app.workers.base import Worker

log = logging.getLogger("app.observability.worker")


class MonitoringWorker(Worker):
    """Collects a snapshot on an interval. Registered like every other worker.

    Started with the platform rather than by an operator action, unlike the
    execution worker and the bot supervisor: those begin consuming signals and
    supervising bots, which is a decision. This one reads. A platform whose
    monitoring begins only when somebody remembers to switch it on is a
    platform that misses the first outage.
    """

    name = "monitoring"

    def __init__(
        self,
        session_factory: Any,
        service: ObservabilityService,
        app: Any,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        super().__init__(self.name, interval_seconds or service.thresholds.interval_seconds)
        self.session_factory = session_factory
        self.service = service
        self.app = app

    async def tick(self) -> None:
        async with self.session_factory() as db:
            snapshot = await self.service.collect(db, self.app)
            if snapshot.incidents:
                # Only a pass that raised something writes. A commit per tick
                # on an unchanged platform would be 5,760 empty transactions a
                # day for nothing.
                await db.commit()
            else:
                await db.rollback()


__all__ = ["MonitoringWorker"]
