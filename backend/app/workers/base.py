"""Background worker infrastructure.

Foundation level: the loop, the heartbeat and the registry. What the workers
*do* belongs to their own levels (the position monitor is L21, the bot manager
L22, market data L08).

The contract every worker gets:

  * a tick that is called on an interval,
  * a failing tick that is logged and does not kill the loop, because a
    worker that dies stops watching,
  * a heartbeat the health check can read, so "running" is observed rather
    than assumed,
  * cooperative shutdown through an event, so a stop is not a kill.

`app.positions.monitor.PositionMonitor` was written with its own copy of this
loop at L21 and now uses this base instead, so there is one implementation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.auth.models import utcnow

log = logging.getLogger("app.workers")

# A worker is stale when its last heartbeat is older than this multiple of its
# own interval. Two intervals allows one slow pass without crying wolf.
STALE_INTERVALS = 2.0
MIN_STALE_SECONDS = 30.0


@dataclass
class WorkerStatus:
    name: str
    running: bool = False
    passes: int = 0
    failures: int = 0
    started_at: datetime | None = None
    last_heartbeat: datetime | None = None
    last_error: str | None = None
    interval_seconds: float = 5.0

    def is_stale(self, now: datetime | None = None) -> bool:
        if not self.running:
            return False
        if self.last_heartbeat is None:
            return True
        now = now or utcnow()
        allowance = max(self.interval_seconds * STALE_INTERVALS, MIN_STALE_SECONDS)
        return now - self.last_heartbeat > timedelta(seconds=allowance)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "running": self.running,
            "passes": self.passes,
            "failures": self.failures,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "last_error": self.last_error,
            "stale": self.is_stale(),
        }


class Worker:
    """A loop with a heartbeat. Subclass and implement `tick`."""

    name = "worker"

    def __init__(self, name: str | None = None, interval_seconds: float = 5.0) -> None:
        if name:
            self.name = name
        self.interval_seconds = interval_seconds
        self.stop_event = asyncio.Event()
        self.status = WorkerStatus(name=self.name, interval_seconds=interval_seconds)

    async def tick(self) -> None:  # pragma: no cover - subclasses implement
        raise NotImplementedError

    async def on_start(self) -> None:
        return None

    async def on_stop(self) -> None:
        return None

    async def run(self, max_passes: int | None = None) -> int:
        """Loop until stopped. Returns the number of passes completed."""
        self.status.running = True
        self.status.started_at = utcnow()
        await self.on_start()
        log.info("worker started", extra={"event": "worker_started", "worker": self.name})
        try:
            while not self.stop_event.is_set():
                try:
                    await self.tick()
                except Exception as exc:  # noqa: BLE001 - a dead worker stops watching
                    self.status.failures += 1
                    self.status.last_error = f"{type(exc).__name__}: {exc}"[:300]
                    log.exception(
                        "worker pass failed",
                        extra={"event": "worker_error", "worker": self.name},
                    )
                self.status.passes += 1
                self.status.last_heartbeat = utcnow()
                if max_passes is not None and self.status.passes >= max_passes:
                    break
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    continue
        finally:
            self.status.running = False
            await self.on_stop()
            log.info(
                "worker stopped",
                extra={
                    "event": "worker_stopped",
                    "worker": self.name,
                    "passes": self.status.passes,
                    "failures": self.status.failures,
                },
            )
        return self.status.passes

    def stop(self) -> None:
        self.stop_event.set()


@dataclass
class WorkerRegistry:
    """Every worker this process supervises, for health reporting.

    An empty registry is healthy: at foundation level no worker is defined
    yet, and reporting "none running" is different from reporting a failure.
    """

    workers: dict[str, Worker] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def register(self, worker: Worker) -> Worker:
        if worker.name in self.workers:
            raise ValueError(f"worker {worker.name!r} is already registered")
        self.workers[worker.name] = worker
        return worker

    def start(self, worker: Worker) -> asyncio.Task:
        if worker.name not in self.workers:
            self.register(worker)
        task = asyncio.create_task(worker.run(), name=f"worker:{worker.name}")
        self._tasks[worker.name] = task
        return task

    async def stop_all(self, timeout: float = 10.0) -> None:
        for worker in self.workers.values():
            worker.stop()
        if not self._tasks:
            return
        await asyncio.wait(set(self._tasks.values()), timeout=timeout)
        for name, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                log.warning(
                    "worker did not stop in time; cancelled",
                    extra={"event": "worker_cancelled", "worker": name},
                )
        self._tasks.clear()

    def statuses(self) -> list[dict[str, object]]:
        return [w.status.as_dict() for w in self.workers.values()]

    def stale(self) -> list[str]:
        return [name for name, w in self.workers.items() if w.status.is_stale()]
