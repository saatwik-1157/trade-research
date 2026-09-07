"""Two background jobs: read the bus, and drain the delivery queue.

Section 28. Both reuse `app.workers.base` and `app.core.events`; neither
introduces a queue system, a scheduler or a second event bus.

**Why the consumer is a second SUBSCRIBER and not a second bus.** The hub in
`app/realtime/hub.py` already holds one subscription for the process and fans
events out to browsers. It is deliberately dumb -- its own docstring says *the
hub delivers; it never decides* -- and giving it a database session so it could
also persist notifications would put a write path inside the object whose whole
design property is that it has none. So the consumer opens its own
subscription onto the SAME bus. One bus, one Redis, two readers with different
jobs, which is what pub/sub is for.

**Why notification work is off the trading path entirely.** Section 28 asks
that notification generation must not block trade execution, order processing,
position updates or broker reconciliation. It cannot: a producer publishes and
returns, and everything in this module happens afterwards in a different task
reading a different connection. There is no code path from a slow SMTP server
back to an order.

**Both loops survive their own failures.** `Worker.run` already catches a
failing tick, records it and keeps going, because a worker that dies stops
watching. The consumer catches per-event so one undeliverable event does not
end the subscription -- an ended subscription is silent, and silence reads
exactly like a platform where nothing is happening.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.core.events import Event, EventBus
from app.notifications.service import MAX_PER_PASS, NotificationService
from app.realtime.hub import SeenEvents
from app.workers.base import Worker

log = logging.getLogger("app.notifications.worker")


class NotificationConsumer:
    """Reads the event bus and turns what it finds into notifications.

    Not a `Worker` subclass: a worker ticks on an interval and this blocks on a
    subscription. The lifecycle is the same shape as `Hub.start`/`Hub.stop`,
    including the part that matters -- the subscription is established before
    `start` returns, so an event published immediately afterwards is delivered
    rather than dropped into the gap between "the task exists" and "the task is
    listening".
    """

    name = "notification-consumer"

    def __init__(
        self,
        bus: EventBus,
        session_factory: Any,
        service: NotificationService,
    ) -> None:
        self.bus = bus
        self.session_factory = session_factory
        self.service = service
        self.seen = SeenEvents()
        self.handled = 0
        self.created = 0
        self.duplicates = 0
        self.failures = 0
        self._task: asyncio.Task | None = None
        self._healthy = True
        self._error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        iterator = await self.bus.subscribe()
        self._task = asyncio.create_task(self._read(iterator), name="notifications:consumer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _read(self, iterator: AsyncIterator[Event]) -> None:
        try:
            async for event in iterator:
                await self.handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never died quietly
            self._healthy = False
            self._error = f"{type(exc).__name__}: {exc}"[:200]
            log.exception(
                "the notification consumer stopped",
                extra={"event": "notification_consumer_failed"},
            )

    async def handle(self, event: Event) -> None:
        """One event. Every failure is contained here.

        The in-process `SeenEvents` is the cheap first line against Redis
        redelivery; the durable one is `UNIQUE (user_id, dedup_key)`. The hub's
        own docstring makes the same point about its own copy: an id can age
        out of a bounded set, so a consumer that must never act twice also
        checks its own state.
        """
        if self.seen.seen(event.id):
            self.duplicates += 1
            return
        try:
            async with self.session_factory() as db:
                outcome = await self.service.on_event(db, event)
                if outcome.created or outcome.duplicates or outcome.suppressed:
                    await db.commit()
                else:
                    await db.rollback()
        except Exception as exc:  # noqa: BLE001 - a bad event does not end the subscription
            self.failures += 1
            self._error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning(
                "an event could not be consumed",
                extra={
                    "event": "notification_consume_failed",
                    "event_type": event.type,
                    "event_id": event.id,
                },
            )
            return
        self.handled += 1
        self.created += len(outcome.created)
        self.duplicates += outcome.duplicates

    def status(self) -> dict[str, object]:
        return {
            "name": self.name,
            "running": self.running,
            "bus": self.bus.kind,
            "healthy": self._healthy,
            "last_error": self._error,
            "events_handled": self.handled,
            "notifications_created": self.created,
            "duplicates_ignored": self.duplicates,
            "failures": self.failures,
        }


class NotificationDeliveryWorker(Worker):
    """Drains the delivery queue on an interval. Sections 28, 29 and 30.

    In-app deliveries are usually already done by the time this runs -- the
    notification row IS the in-app delivery -- so in practice this worker is
    what sends email and, from L35, Discord. It is registered and started with
    the rest of the platform's workers, and its heartbeat is what
    `/health/ready` reads to say whether notifications are being processed at
    all.
    """

    name = "notification-delivery"

    def __init__(
        self,
        session_factory: Any,
        service: NotificationService,
        *,
        interval_seconds: float = 5.0,
        batch: int = MAX_PER_PASS,
    ) -> None:
        super().__init__(self.name, interval_seconds)
        self.session_factory = session_factory
        self.service = service
        self.batch = batch
        self.delivered = 0
        self.failed = 0

    async def tick(self) -> None:
        async with self.session_factory() as db:
            results = await self.service.deliver_pending(db, limit=self.batch)
            if results:
                await db.commit()
            else:
                await db.rollback()
        for result in results:
            if result.get("status") == "DELIVERED":
                self.delivered += 1
            elif result.get("status") == "FAILED":
                self.failed += 1


__all__ = ["NotificationConsumer", "NotificationDeliveryWorker"]
