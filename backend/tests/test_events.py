"""Event bus: envelope, validation, delivery, and honest labelling."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from app.core.events import (
    MAX_PAYLOAD_BYTES,
    Event,
    EventError,
    InMemoryEventBus,
    RedisEventBus,
    make_event_bus,
)


def test_an_event_carries_its_own_id_and_time() -> None:
    a = Event(type="ORDER_FILLED", payload={"order_id": "1"})
    b = Event(type="ORDER_FILLED", payload={"order_id": "1"})
    assert a.id != b.id  # so a subscriber can deduplicate
    assert a.at is not None and a.source == "api"


@pytest.mark.parametrize("bad_type", ["", "   "])
def test_an_event_without_a_type_is_refused(bad_type: str) -> None:
    with pytest.raises(EventError):
        Event(type=bad_type, payload={})


def test_a_non_object_payload_is_refused() -> None:
    with pytest.raises(EventError):
        Event(type="X", payload=[1, 2, 3])  # type: ignore[arg-type]


def test_round_trip_preserves_the_envelope() -> None:
    original = Event(type="MARKET_TICK", payload={"symbol": "EURUSD"}, source="worker")
    restored = Event.decode(original.encode())
    assert restored.id == original.id
    assert restored.type == "MARKET_TICK"
    assert restored.source == "worker"
    assert restored.payload == {"symbol": "EURUSD"}


def test_an_oversized_event_is_refused_rather_than_truncated() -> None:
    big = Event(type="X", payload={"blob": "y" * (MAX_PAYLOAD_BYTES + 10)})
    with pytest.raises(EventError, match="larger than"):
        big.encode()


def test_undecodable_input_raises_instead_of_guessing() -> None:
    with pytest.raises(EventError):
        Event.decode("not json")
    with pytest.raises(EventError):
        Event.decode('{"payload": {}}')  # no type


async def test_in_memory_delivery_to_a_type_subscriber() -> None:
    bus = InMemoryEventBus()
    received: list[Event] = []

    async def listen() -> None:
        async for event in await bus.subscribe("ORDER_FILLED"):
            received.append(event)
            return

    task = asyncio.create_task(listen())
    await asyncio.sleep(0)
    await bus.publish(Event(type="ORDER_FILLED", payload={"n": 1}))
    await asyncio.wait_for(task, timeout=1)
    assert [e.payload["n"] for e in received] == [1]


async def test_a_subscriber_does_not_receive_other_types() -> None:
    bus = InMemoryEventBus()
    got: list[str] = []

    async def listen() -> None:
        async for event in await bus.subscribe("ORDER_FILLED"):
            got.append(event.type)

    task = asyncio.create_task(listen())
    await asyncio.sleep(0)
    await bus.publish(Event(type="MARKET_TICK", payload={}))
    await asyncio.sleep(0.02)
    task.cancel()
    assert got == []
    # It was published, just not delivered to this subscriber.
    assert [e.type for e in bus.published] == ["MARKET_TICK"]


async def test_publishing_validates_on_the_same_path_as_redis() -> None:
    bus = InMemoryEventBus()
    with pytest.raises(EventError):
        await bus.publish(Event(type="X", payload={"blob": "y" * (MAX_PAYLOAD_BYTES + 10)}))
    assert bus.published == []


def test_the_bus_says_which_kind_it_is() -> None:
    # A local delivery must never be mistaken for a distributed one.
    assert InMemoryEventBus().kind == "in_memory"
    assert RedisEventBus("redis://127.0.0.1:6390/0").kind == "redis"
    assert make_event_bus(None).kind == "in_memory"
    assert make_event_bus("redis://127.0.0.1:6390/0").kind == "redis"


# ============================================= the bus circuit breaker (L43)


def _dead_bus():  # noqa: ANN202
    """A bus pointed at a port nothing listens on."""
    from app.core.events import RedisEventBus

    return RedisEventBus("redis://127.0.0.1:6399/0")


async def test_the_breaker_opens_after_repeated_failures() -> None:
    """L43. A dead Redis must stop costing a connect timeout per request.

    The failure mode this fixes was the wrong way round: the platform got
    SLOWEST exactly when it was already degraded, on the webhook endpoint whose
    caller retries on timeout. L40 bounded it to 0.5s with a connect timeout;
    this removes the repeat cost entirely.
    """
    from app.core.events import FAILURES_BEFORE_SKIP, Event, EventError

    bus = _dead_bus()
    skipped_message = 0
    for _ in range(FAILURES_BEFORE_SKIP + 4):
        try:
            await bus.publish(Event(type="SYSTEM_ALERT", payload={}, channel="system"))
        except EventError as exc:
            if "skipped without" in str(exc):
                skipped_message += 1
        except Exception:  # noqa: BLE001 - a connection error is the point
            pass
    assert bus.skipped > 0, "the breaker never opened"
    assert skipped_message > 0, "a skipped publish did not say so"


async def test_a_skipped_publish_still_raises_so_the_caller_decides() -> None:
    """The breaker must not make a failure look like a success.

    Every call site already guards `publish` and treats a failure as a warning.
    Returning quietly would turn "the event was not delivered" into "the event
    was delivered", which is the one outcome worse than being slow.
    """
    from app.core.events import FAILURES_BEFORE_SKIP, Event, EventError

    bus = _dead_bus()
    for _ in range(FAILURES_BEFORE_SKIP):
        with contextlib.suppress(Exception):
            await bus.publish(Event(type="SYSTEM_ALERT", payload={}, channel="system"))
    with pytest.raises(EventError):
        await bus.publish(Event(type="SYSTEM_ALERT", payload={}, channel="system"))


async def test_a_successful_probe_closes_the_breaker() -> None:
    """It must recover on its own; an operator should not have to restart."""
    from app.core.events import FAILURES_BEFORE_SKIP, Event

    bus = _dead_bus()
    for _ in range(FAILURES_BEFORE_SKIP + 1):
        with contextlib.suppress(Exception):
            await bus.publish(Event(type="SYSTEM_ALERT", payload={}, channel="system"))
    assert bus._consecutive_failures >= FAILURES_BEFORE_SKIP

    # The cooldown elapses and the endpoint comes back.
    bus.url = "redis://127.0.0.1:6390/0"
    bus._closed_until = 0.0
    try:
        await bus.publish(Event(type="SYSTEM_ALERT", payload={}, channel="system"))
    except Exception:  # noqa: BLE001
        pytest.skip("no Redis on 6390; the recovery leg needs one")
    assert bus._consecutive_failures == 0, "a success did not close the breaker"
    assert bus.skipped >= 0


def test_the_breaker_cannot_be_reached_by_the_in_memory_bus() -> None:
    """The in-process bus has nothing to fail, and must not grow a breaker.

    A second implementation of the skip logic is a second thing to get wrong.
    """
    from app.core.events import InMemoryEventBus

    assert not hasattr(InMemoryEventBus, "_available")
