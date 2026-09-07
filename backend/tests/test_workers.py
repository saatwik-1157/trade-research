"""Worker infrastructure: the loop, the heartbeat, the registry."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from app.auth.models import utcnow
from app.workers.base import Worker, WorkerRegistry, WorkerStatus


class Counter(Worker):
    name = "counter"

    def __init__(self, fail_on: set[int] | None = None, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.ticks = 0
        self.fail_on = fail_on or set()
        self.started = False
        self.stopped = False

    async def on_start(self) -> None:
        self.started = True

    async def on_stop(self) -> None:
        self.stopped = True

    async def tick(self) -> None:
        self.ticks += 1
        if self.ticks in self.fail_on:
            raise RuntimeError(f"tick {self.ticks} failed")


async def test_a_worker_runs_and_records_a_heartbeat() -> None:
    worker = Counter(interval_seconds=0.01)
    passes = await worker.run(max_passes=3)
    assert passes == 3 and worker.ticks == 3
    assert worker.started and worker.stopped
    assert worker.status.last_heartbeat is not None
    assert worker.status.failures == 0
    assert worker.status.running is False


async def test_a_failing_tick_is_recorded_and_the_loop_survives() -> None:
    worker = Counter(fail_on={1, 2}, interval_seconds=0.01)
    passes = await worker.run(max_passes=4)
    assert passes == 4  # it kept going
    assert worker.status.failures == 2
    assert worker.status.last_error is not None
    assert "tick 2 failed" in worker.status.last_error


async def test_stop_is_cooperative() -> None:
    worker = Counter(interval_seconds=0.05)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.02)
    worker.stop()
    passes = await asyncio.wait_for(task, timeout=2)
    assert passes >= 1
    assert worker.stopped and not worker.status.running


def test_staleness_needs_a_running_worker_and_a_late_heartbeat() -> None:
    status = WorkerStatus(name="w", interval_seconds=1.0)
    # Not running: never stale, because nothing is expected of it.
    assert status.is_stale() is False

    status.running = True
    assert status.is_stale() is True  # running with no heartbeat yet

    status.last_heartbeat = utcnow()
    assert status.is_stale() is False

    status.last_heartbeat = utcnow() - timedelta(seconds=120)
    assert status.is_stale() is True


async def test_registry_reports_statuses_and_refuses_duplicate_names() -> None:
    registry = WorkerRegistry()
    registry.register(Counter(name="a", interval_seconds=0.01))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Counter(name="a"))

    statuses = registry.statuses()
    assert [s["name"] for s in statuses] == ["a"]
    assert statuses[0]["running"] is False
    assert registry.stale() == []


async def test_registry_starts_and_stops_everything() -> None:
    registry = WorkerRegistry()
    first = Counter(name="first", interval_seconds=0.01)
    second = Counter(name="second", interval_seconds=0.01)
    registry.start(first)
    registry.start(second)
    await asyncio.sleep(0.05)
    assert first.status.running and second.status.running

    await registry.stop_all(timeout=2)
    assert not first.status.running and not second.status.running
    assert first.ticks >= 1 and second.ticks >= 1


async def test_stopping_an_empty_registry_is_harmless() -> None:
    await WorkerRegistry().stop_all()
