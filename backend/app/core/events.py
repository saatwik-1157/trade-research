"""Event bus infrastructure.

Foundation level: the *transport* and the envelope, not the trading event
architecture. Level 07 defines the event catalogue (MARKET_TICK, ORDER_FILLED
and the rest) and the WebSocket fan-out; this module gives it something to
publish onto and gives tests something that needs no server.

Two implementations behind one protocol:

  `RedisEventBus`    real pub/sub, for the API and the workers.
  `InMemoryEventBus` same semantics in-process, for tests and for a single
                     process that has no Redis. It is explicitly labelled, so
                     nothing can mistake a local delivery for a distributed one.

An event carries its own id and timestamp so a subscriber can deduplicate, and
`source` so a loop can tell its own publications from someone else's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.auth.models import utcnow

log = logging.getLogger("app.events")

MAX_PAYLOAD_BYTES = 256 * 1024


class EventError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    """One message on the bus."""

    type: str
    payload: dict
    source: str = "api"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    at: datetime = field(default_factory=utcnow)

    # --- Routing and correlation, added at L07. -------------------------
    # All optional, all defaulted, so every Event constructed before this
    # level still constructs. `channel` is where the event is delivered;
    # `correlation_id` is the request or run id that caused it, so a trace
    # spans API -> service -> worker -> integration. None of the three is
    # ever inferred: an event whose channel was guessed would be delivered
    # to whoever the guess named.
    channel: str | None = None
    correlation_id: str | None = None
    user_id: str | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        if not self.type or not self.type.strip():
            raise EventError("event type is required")
        if not isinstance(self.payload, dict):
            raise EventError("event payload must be an object")

    def encode(self) -> str:
        body = json.dumps(
            {
                "id": self.id,
                "type": self.type,
                "source": self.source,
                "at": self.at.isoformat(timespec="milliseconds"),
                "channel": self.channel,
                "correlation_id": self.correlation_id,
                "user_id": self.user_id,
                "account_id": self.account_id,
                "payload": self.payload,
            },
            default=str,
        )
        if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise EventError(
                f"event {self.type} is larger than {MAX_PAYLOAD_BYTES} bytes; "
                "publish a reference rather than the body"
            )
        return body

    @staticmethod
    def decode(raw: str) -> Event:
        try:
            data = json.loads(raw)
            return Event(
                type=data["type"],
                payload=data.get("payload") or {},
                source=data.get("source", "unknown"),
                id=data.get("id", uuid.uuid4().hex),
                at=datetime.fromisoformat(data["at"]) if data.get("at") else utcnow(),
                # Absent is None, never a substituted value: a decoded event
                # from a publisher that predates these fields must not claim a
                # channel or a user it never named.
                channel=data.get("channel"),
                correlation_id=data.get("correlation_id"),
                user_id=data.get("user_id"),
                account_id=data.get("account_id"),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise EventError(f"undecodable event: {exc}") from exc


class EventBus(Protocol):
    kind: str

    async def publish(self, event: Event) -> None: ...

    async def subscribe(self, *types: str) -> AsyncIterator[Event]: ...

    async def close(self) -> None: ...


class InMemoryEventBus:
    """In-process delivery. Labelled, so it is never mistaken for Redis."""

    kind = "in_memory"

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        event.encode()  # validate and size-check on the same path as Redis
        self.published.append(event)
        for queue in list(self._queues[event.type]) + list(self._queues["*"]):
            queue.put_nowait(event)

    async def subscribe(self, *types: str) -> AsyncIterator[Event]:
        """Register the queue, then return an iterator over it.

        Registration happens before this returns, not on the first
        `__anext__`. As an async generator it did the opposite, which left a
        window between "subscribed" and "actually listening" in which a
        publish was silently dropped -- and a dropped event looks exactly like
        an event that never happened.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue()
        keys = types or ("*",)
        for key in keys:
            self._queues[key].append(queue)

        async def drain() -> AsyncIterator[Event]:
            try:
                while True:
                    yield await queue.get()
            finally:
                for key in keys:
                    if queue in self._queues[key]:
                        self._queues[key].remove(queue)

        return drain()

    async def close(self) -> None:
        self._queues.clear()


#: How long to wait for a TCP connection to Redis before giving up. Generous
#: against a healthy deployment and strict against a dead one; see `_connect`.
CONNECT_TIMEOUT_SECONDS = 0.5

#: How long a single command may take once connected. Longer than the connect
#: budget because a real operation on a loaded server legitimately takes longer
#: than opening a socket, and still bounded.
OPERATION_TIMEOUT_SECONDS = 2.0

#: Consecutive failures before the breaker opens. Three rather than one so a
#: single dropped connection does not stop the platform publishing.
FAILURES_BEFORE_SKIP = 3

#: How long to skip for before probing again.
SKIP_SECONDS = 5.0


class RedisEventBus:
    """Redis pub/sub. One channel per event type, prefixed."""

    kind = "redis"

    def __init__(self, url: str, prefix: str = "tr.events") -> None:
        self.url = url
        self.prefix = prefix
        self._client: Any | None = None
        # L43 circuit breaker. See `_available` for the reasoning.
        self._consecutive_failures = 0
        self._closed_until = 0.0
        self.skipped = 0

    # -------------------------------------------------------------- breaker

    def _available(self) -> bool:
        """Whether to attempt Redis at all, or skip and let the caller carry on.

        L40 bounded a dead Redis to a 0.5s connect timeout, down from 2.06s.
        This removes most of what is left, and the reasoning is the same: the
        failure mode was the wrong way round. **The platform got slowest exactly
        when it was already degraded**, on the webhook endpoint whose caller --
        TradingView -- retries on timeout. Paying 0.5s to re-learn a fact
        established half a second ago buys nothing.

        Correctness never depended on Redis. Signals, orders, positions and
        journal rows are all in PostgreSQL; a publish failure is already a
        warning that the caller absorbs, and every call site guards it. So the
        breaker can only make an already-safe failure faster.

        Deliberately simple: N consecutive failures opens it, one probe closes
        it. There is no half-open request budget and no exponential backoff,
        because a fixed 5-second retry against a local Redis is already cheap
        and the complexity would buy nothing measurable.
        """
        if self._consecutive_failures < FAILURES_BEFORE_SKIP:
            return True
        if time.monotonic() >= self._closed_until:
            # Probe. If it fails, `_record_failure` pushes the window out again.
            return True
        self.skipped += 1
        return False

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        self._closed_until = time.monotonic() + SKIP_SECONDS
        # A connection that failed is not reusable: redis-py caches the pool,
        # and holding it means the next probe re-uses a broken socket.
        self._client = None

    def _record_success(self) -> None:
        if self._consecutive_failures:
            log.info(
                "event bus reachable again",
                extra={"event": "event_bus_recovered", "skipped": self.skipped},
            )
        self._consecutive_failures = 0

    def _channel(self, event_type: str) -> str:
        return f"{self.prefix}.{event_type}"

    async def _connect(self):  # noqa: ANN202 - redis client type is dynamic
        if self._client is None:
            import redis.asyncio as aioredis

            # Bounded timeouts, added at L40 against a measurement rather than a
            # hunch. `from_url` with no timeouts inherits redis-py's defaults,
            # which retry a refused connection with backoff -- and a webhook
            # POST measured at **2.06 seconds** on a host with Redis down,
            # essentially all of it spent failing to connect before the caller's
            # own guard logged "event publish failed" and carried on.
            #
            # The publish is already non-fatal: the signal is committed before
            # it and a failure is a warning, so correctness never depended on
            # Redis being up. What did depend on it was latency, and the failure
            # mode is the wrong way round -- the platform got SLOWEST exactly
            # when it was already degraded. TradingView times out its webhook
            # deliveries and retries, so two seconds of dead-socket backoff per
            # alert turns one outage into duplicate deliveries.
            #
            # 0.5s to connect is far above a healthy local or same-VPC round
            # trip (sub-millisecond to single-digit milliseconds) and far below
            # anything a caller notices. `retry_on_timeout=False` because the
            # caller's guard is the retry policy: one attempt, then carry on and
            # say so.
            self._client = aioredis.from_url(
                self.url,
                decode_responses=True,
                socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
                socket_timeout=OPERATION_TIMEOUT_SECONDS,
                retry_on_timeout=False,
                health_check_interval=30,
            )
        return self._client

    async def publish(self, event: Event) -> None:
        if not self._available():
            raise EventError(
                "the event bus is unreachable; this publish was skipped without "
                "attempting a connection. The caller's own guard decides what that "
                "means -- nothing here retries."
            )
        try:
            client = await self._connect()
            await client.publish(self._channel(event.type), event.encode())
        except Exception:
            self._record_failure()
            raise
        self._record_success()

    async def subscribe(self, *types: str) -> AsyncIterator[Event]:
        """Subscribe on Redis, then return an iterator over what arrives.

        The SUBSCRIBE round trip completes before this returns, so a caller
        that awaits it knows the server is listening. Doing it inside a
        generator body deferred the round trip to the first `__anext__` and
        lost anything published in between.
        """
        client = await self._connect()
        pubsub = client.pubsub()
        if types:
            await pubsub.subscribe(*[self._channel(t) for t in types])
        else:
            await pubsub.psubscribe(f"{self.prefix}.*")

        async def drain() -> AsyncIterator[Event]:
            try:
                async for message in pubsub.listen():
                    if message.get("type") not in ("message", "pmessage"):
                        continue
                    try:
                        yield Event.decode(message["data"])
                    except EventError:
                        # A malformed message is dropped and logged, never
                        # guessed at: acting on half an event is worse than
                        # missing it.
                        log.warning(
                            "dropped undecodable event",
                            extra={
                                "event": "event_decode_failed",
                                "channel": message.get("channel"),
                            },
                        )
            finally:
                await pubsub.aclose()

        return drain()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def make_event_bus(redis_url: str | None) -> EventBus:
    """Redis when a URL is configured, in-process otherwise."""
    return RedisEventBus(redis_url) if redis_url else InMemoryEventBus()
