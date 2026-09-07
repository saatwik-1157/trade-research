"""The realtime hub: connections, subscriptions, fan-out and counters.

One hub per process. It owns the set of live connections and the single
subscription to the event bus that feeds them, so N sockets share one bus
reader rather than opening N Redis subscriptions.

The design rules, each of which a test enforces:

1. **The hub delivers; it never decides.** Nothing here places an order,
   changes a position or calls a service. A bug in this file can drop a
   message or deliver it twice; it cannot trade. That is why the fan-out is
   deliberately dumb.
2. **A slow client is dropped, not buffered forever.** Each connection has a
   bounded queue. When it overflows the connection is closed with a reason,
   because an unbounded queue behind a stalled socket is a memory leak that
   ends the process -- and taking the API down to keep one browser tab
   updated is the wrong trade.
3. **The bus is not the source of truth.** Events are notifications that
   something changed in the database. A consumer that needs the value reads
   it back; the frame is a nudge, not a record.
4. **Delivery is at-most-once to a browser and at-least-once from the bus.**
   Redis pub/sub drops messages to a disconnected subscriber and may deliver
   the same message to a reconnecting one, so every event carries an id and
   `SeenEvents` below is how a consumer refuses to act twice.
5. **A realtime failure never triggers a trading action.** There is no path
   from a dropped frame to an order. When the bus is unavailable the hub
   reports it and delivers nothing, and nothing downstream treats silence as
   a signal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.core.events import Event, EventBus, EventError
from app.realtime.catalogue import is_known
from app.realtime.channels import MAX_SUBSCRIPTIONS, Channel

log = logging.getLogger("app.realtime")

# Frames a connection may fall behind by before it is dropped. Sized for a
# burst, not for an absent reader.
QUEUE_DEPTH = 256

# Inbound control frames are tiny; anything larger is not one of ours.
MAX_CLIENT_FRAME_BYTES = 4 * 1024

#: L39. Live sockets one account may hold at once. A browser needs one and
#: a few tabs need a few; a hundred is a script. Overridable per deployment
#: through `WS_MAX_CONNECTIONS_PER_USER`; 0 disables the cap, which the
#: security posture then reports as a control that is not in force.
MAX_CONNECTIONS_PER_USER = 8


class ConnectionLimit(Exception):
    """One account tried to hold more live sockets than it may."""


class SeenEvents:
    """Bounded set of event ids already handled, for idempotent consumers.

    Receiving ORDER_FILLED twice must not create two trades, and receiving
    BOT_STARTED twice must not start two workers. The consumer asks this
    first. It is bounded and ordered so a long-running process cannot grow
    without limit; an id that ages out would be re-processed, which is why a
    consumer that must never act twice also checks its own domain state. This
    is the cheap first line, not the only one.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self.capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()

    def seen(self, event_id: str) -> bool:
        """True if this id has been offered before. Records it either way."""
        if event_id in self._ids:
            self._ids.move_to_end(event_id)
            return True
        self._ids[event_id] = None
        if len(self._ids) > self.capacity:
            self._ids.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._ids)


@dataclass
class Connection:
    """One WebSocket, its subscriptions and its outbound queue."""

    id: str
    user_id: str
    queue: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_DEPTH))
    channels: set[str] = field(default_factory=set)
    dropped: bool = False

    def subscribe(self, channel: Channel) -> None:
        if len(self.channels) >= MAX_SUBSCRIPTIONS:
            raise ValueError(f"a connection may hold at most {MAX_SUBSCRIPTIONS} subscriptions")
        self.channels.add(str(channel))

    def unsubscribe(self, channel: Channel) -> None:
        self.channels.discard(str(channel))

    def wants(self, event: Event) -> bool:
        return event.channel is not None and event.channel in self.channels

    def offer(self, event: Event) -> bool:
        """Queue the event. False means this connection is too far behind."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped = True
            return False
        return True


@dataclass
class HubCounters:
    """What the hub has observed. Counts only -- never payloads."""

    connections_opened: int = 0
    connections_closed: int = 0
    events_published: int = 0
    events_delivered: int = 0
    events_dropped_slow: int = 0
    publish_failures: int = 0
    subscribe_refusals: int = 0
    #: L39. Sockets refused because the account already held the maximum.
    connections_refused: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "connections_opened": self.connections_opened,
            "connections_closed": self.connections_closed,
            "events_published": self.events_published,
            "events_delivered": self.events_delivered,
            "events_dropped_slow": self.events_dropped_slow,
            "publish_failures": self.publish_failures,
            "subscribe_refusals": self.subscribe_refusals,
            "connections_refused": self.connections_refused,
        }


class Hub:
    """Connections plus one bus reader."""

    def __init__(self, bus: EventBus, max_per_user: int = MAX_CONNECTIONS_PER_USER) -> None:
        self.bus = bus
        self.connections: dict[str, Connection] = {}
        self.counters = HubCounters()
        self.max_per_user = max_per_user
        self._reader: asyncio.Task | None = None
        self._bus_healthy = True
        self._bus_error: str | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Subscribe, then start reading.

        The subscription is established before this returns, so an event
        published immediately after startup is delivered rather than dropped
        into the gap between "the task exists" and "the task is listening".

        A bus that will not answer does NOT stop the process. `_read_bus`
        already treats a bus that dies a second AFTER startup as degraded and
        reports it through `status()`; a bus that was down a second BEFORE
        startup has to mean the same thing, or availability depends on Redis in
        exactly the direction `RedisEventBus` says it must not -- its breaker
        exists because correctness never depended on Redis. Live delivery is
        off until something calls `start` again; nothing else in the process is
        waiting on this subscription.
        """
        if self._reader is not None and not self._reader.done():
            return
        try:
            iterator = await self.bus.subscribe()
        except Exception as exc:  # noqa: BLE001 - the hub reports, it does not die quietly
            self._bus_healthy = False
            self._bus_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning(
                "realtime bus subscribe failed; live delivery is off",
                extra={"event": "realtime_bus_subscribe_failed"},
            )
            return
        self._bus_healthy = True
        self._bus_error = None
        self._reader = asyncio.create_task(self._read_bus(iterator), name="realtime:bus-reader")

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        for connection in list(self.connections.values()):
            self.remove(connection.id)

    # ---------------------------------------------------------- connections

    def connections_for(self, user_id: str) -> int:
        return sum(1 for c in self.connections.values() if c.user_id == user_id)

    def add(self, connection: Connection) -> Connection:
        """Register a connection, or refuse because the account holds too many.

        L39. The hub already capped subscriptions per connection and bytes per
        frame; **nothing capped connections per account**, so one script could
        open sockets until the process ran out of them and take the live feed
        down for everybody else. That is a denial of service that needs a valid
        session and no privileges at all, which makes it the cheapest one this
        platform had.

        The cap is per user rather than per address on purpose: an address is
        shared by an office and spoofed by an attacker, and the socket is
        already authenticated by the time it gets here, so the account is both
        the more accurate identity and the one an operator can act on.

        Raising rather than silently evicting the oldest: closing somebody's
        working tab to make room for a new one is indistinguishable, from the
        browser, from the bug this is meant to prevent.
        """
        if self.max_per_user > 0:
            held = self.connections_for(connection.user_id)
            if held >= self.max_per_user:
                self.counters.connections_refused += 1
                raise ConnectionLimit(
                    f"this account already holds {held} live connections, which is "
                    f"the limit of {self.max_per_user}. Close a tab and reconnect."
                )
        self.connections[connection.id] = connection
        self.counters.connections_opened += 1
        return connection

    def remove(self, connection_id: str) -> None:
        if self.connections.pop(connection_id, None) is not None:
            self.counters.connections_closed += 1

    def subscriber_count(self, channel: str) -> int:
        return sum(1 for c in self.connections.values() if channel in c.channels)

    # ------------------------------------------------------------ publishing

    async def publish(self, event: Event) -> None:
        """Validate against the catalogue, then hand to the bus.

        An unknown type is refused rather than delivered: a subscriber that
        never fires is indistinguishable from a market that never moved, and
        a typo'd event type would produce exactly that silence.
        """
        if not is_known(event.type):
            raise EventError(f"{event.type!r} is not in the event catalogue")
        if event.channel is None:
            raise EventError(f"{event.type} has no channel; it cannot be routed")
        try:
            await self.bus.publish(event)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            self.counters.publish_failures += 1
            self._bus_healthy = False
            self._bus_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning(
                "event publish failed",
                extra={
                    "event": "event_publish_failed",
                    "event_type": event.type,
                    "correlation_id": event.correlation_id,
                },
            )
            raise
        self._bus_healthy = True
        self._bus_error = None
        self.counters.events_published += 1

    # -------------------------------------------------------------- fan-out

    def dispatch(self, event: Event) -> int:
        """Deliver one event to every connection subscribed to its channel."""
        delivered = 0
        for connection in list(self.connections.values()):
            if not connection.wants(event):
                continue
            if connection.offer(event):
                delivered += 1
            else:
                self.counters.events_dropped_slow += 1
                log.warning(
                    "connection too far behind; dropping it",
                    extra={
                        "event": "realtime_slow_consumer",
                        "connection": connection.id,
                        "channel": event.channel,
                    },
                )
        self.counters.events_delivered += delivered
        return delivered

    async def _read_bus(self, iterator: AsyncIterator[Event]) -> None:
        """One subscription for the whole process, fanned out to connections."""
        try:
            async for event in iterator:
                self.dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the hub reports, it does not die quietly
            self._bus_healthy = False
            self._bus_error = f"{type(exc).__name__}: {exc}"[:200]
            log.exception("realtime bus reader stopped", extra={"event": "realtime_bus_failed"})

    # ------------------------------------------------------------ reporting

    def status(self) -> dict[str, object]:
        return {
            "bus": self.bus.kind,
            "bus_healthy": self._bus_healthy,
            "bus_error": self._bus_error,
            "reader_running": self._reader is not None and not self._reader.done(),
            "connections": len(self.connections),
            "subscriptions": sum(len(c.channels) for c in self.connections.values()),
            **self.counters.as_dict(),
        }
