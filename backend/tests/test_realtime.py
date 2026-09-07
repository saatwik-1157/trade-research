"""Realtime: catalogue, channels, authorization, hub fan-out, and the socket.

The WebSocket cases run through Starlette's real test client against the real
route, so the handshake, the close codes and the frames are the ones a browser
would see.

The case that matters most is `test_a_user_cannot_subscribe_to_another_users_account`.
Changing an id in a subscribe frame is the cheapest attack on a system like
this, and the whole channel layer exists to refuse it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from app.auth.models import Role, User
from app.core.events import Event, EventError, InMemoryEventBus
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.models.accounts import BrokerAccount, PaperAccount
from app.realtime import catalogue as cat
from app.realtime import channels as ch
from app.realtime.hub import QUEUE_DEPTH, Connection, Hub, SeenEvents
from app.realtime.ws import (
    CLOSE_BAD_FRAME,
    CLOSE_UNAUTHENTICATED,
    system_alert,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
BOB = {"email": "bob@tr-platform.io", "password": "another strong one"}


@pytest.fixture
async def app(settings: Settings, tmp_path: Path) -> AsyncIterator[FastAPI]:
    """A FILE-backed SQLite database, not in-memory with `StaticPool`.

    This is the fix for the flake `KNOWN_TEST_LIMITATIONS.md` recorded at L41 --
    1 to 2 failures per run, a different test each time, always a WebSocket
    handshake closing 4401 despite a register that had just returned 201.

    The cause is a **cross-event-loop connection**. This fixture is `async`, so
    it runs in pytest-asyncio's loop; `ws_client` below is `sync` and drives the
    app through `TestClient`, which spins up its OWN loop in a portal thread.
    `StaticPool` holds exactly one DBAPI connection and hands that same
    connection to both -- and an `aiosqlite` connection owns a thread and a
    queue bound to the loop that created it. The session lookup during the
    WebSocket handshake would intermittently run on the wrong loop and come
    back empty, which the handshake correctly reports as "not signed in".

    A file gives each loop its own connection to the same data, so nothing is
    shared across loops but the bytes on disk. `tmp_path` is per-test, so the
    isolation the in-memory database provided is unchanged.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'realtime.db'}")
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # In-process bus: the fan-out being tested is the hub's, and a test whose
    # outcome depends on a Redis container is testing the container.
    #
    # Workers off for the same reason, and it is the file-backed database that
    # makes it necessary. The notification-delivery and monitoring workers poll
    # on a timer, SQLite takes one writer at a time, and a poll landing on a
    # register inside a WebSocket test fails it with "database is locked" --
    # about the worker's timing, not about the hub. They only ever ran here on
    # a host with no Redis reachable, because until the lifespan learned to
    # start without one the whole application refused to come up first.
    application = create_app(
        settings.model_copy(update={"workers_enabled": False}), checks={}, engine=engine
    )
    application.state.event_bus = InMemoryEventBus()
    application.state.hub = Hub(application.state.event_bus)
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _register(client: AsyncClient, who: dict) -> str:
    r = await client.post("/auth/register", json=who)
    assert r.status_code == 201
    return str(r.json()["id"])


async def _session_cookie(app: FastAPI, client: AsyncClient, who: dict) -> str:
    await client.post("/auth/register", json=who)
    return client.cookies["tr_session"]


async def _promote(app: FastAPI, email: str, role: Role) -> None:
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role.value
        await db.commit()


# ------------------------------------------------------------- catalogue


def test_every_catalogue_type_has_a_spec() -> None:
    assert set(cat.CATALOGUE) == set(cat.EventType)
    # 52 since L39, which added SECURITY_ALERT (system-scoped, a count and a
    # class, never a subject) and ACCOUNT_SECURITY_ALERT (user-scoped). Two
    # types rather than one because the split is an authorization decision:
    # a `system` event never carries private figures, and a security alert is
    # the easiest place in the catalogue to break that rule.
    assert len(cat.CATALOGUE) == 52


def test_order_and_position_events_are_account_scoped() -> None:
    """A fill is private. If one of these were system-scoped, every signed-in
    user would receive another user's execution."""
    private = [t for t in cat.EventType if t.startswith(("ORDER_", "POSITION_", "RISK_"))]
    assert private
    for event_type in private:
        assert cat.scope_of(event_type) is cat.Scope.account


def test_every_produced_type_has_a_real_producer() -> None:
    """Everything else is a declared contract whose emitter arrives with its
    level. Publishing a fabricated ORDER_FILLED to demonstrate the transport
    would put a fill on the bus that no venue reported.

    The eight AI types joined at L28, which is also when they started being
    produced: `TrainingService._publish` had passed two positional arguments to
    a one-argument `Hub.publish` since L25, so every training and validation
    event raised a TypeError that the surrounding `except` swallowed. The
    events never reached the bus, and a subscriber that never fires looks
    exactly like a market that never moved.

    Eleven more joined at L30, L31, L33 and L34, and each has a real producer:
    `PortfolioService.publish` emits only what CHANGED since the previous view,
    `TradeJournalService.publish` fires when a journal row is written or a
    reconciliation flags one, and `TradeReviewService.publish` fires when a
    review finishes or fails, and `InAppChannel.send` publishes
    NOTIFICATION_CREATED when a notification has been stored. The ORDER_* and
    POSITION_* families are still
    absent from this set, which is the property the test exists to hold: a type
    is in `PRODUCED_NOW` only when something really emits it.
    """
    assert cat.PRODUCED_NOW == frozenset(
        {
            cat.EventType.SYSTEM_ALERT,
            cat.EventType.TRAINING_JOB_UPDATED,
            cat.EventType.VALIDATION_RUN_UPDATED,
            cat.EventType.MODEL_REGISTERED,
            cat.EventType.MODEL_PAPER_ACTIVATED,
            cat.EventType.MODEL_ACTIVATED,
            cat.EventType.MODEL_ROLLED_BACK,
            cat.EventType.MODEL_RETIRED,
            cat.EventType.MODEL_REJECTED,
            cat.EventType.MODEL_HEALTH_CHANGED,
            cat.EventType.MODEL_ALERT_CREATED,
            cat.EventType.MODEL_ALERT_RECOVERED,
            # L30 -- PortfolioService.publish, on what changed.
            cat.EventType.PORTFOLIO_UPDATED,
            cat.EventType.EXPOSURE_UPDATED,
            cat.EventType.DRAWDOWN_ALERT,
            cat.EventType.PORTFOLIO_HEALTH_CHANGED,
            # L31 -- TradeJournalService.publish.
            cat.EventType.TRADE_RECORDED,
            cat.EventType.TRADE_UPDATED,
            cat.EventType.TRADE_RECONCILIATION_REQUIRED,
            # L33 -- TradeReviewService.publish.
            cat.EventType.TRADE_REVIEW_COMPLETED,
            cat.EventType.TRADE_REVIEW_FAILED,
            cat.EventType.TRADE_PATTERN_DETECTED,
            # L34 -- InAppChannel, when a notification is stored. The type and
            # the `user` scope have been catalogued since L07 waiting for
            # exactly this producer; nothing about the routing changed.
            cat.EventType.NOTIFICATION_CREATED,
            # L39. `app/security/announce.py` is the producer, and it
            # publishes onto the L07 hub that `NotificationConsumer`
            # already reads -- so a security event becomes a notification
            # by the path every other event takes, with no second bus and
            # no direct call into the notification service.
            cat.EventType.SECURITY_ALERT,
            cat.EventType.ACCOUNT_SECURITY_ALERT,
        }
    )
    # The families with no producer are still absent, which is the point.
    for absent in ("ORDER_FILLED", "POSITION_OPENED", "SIGNAL_CREATED", "MARKET_UPDATE"):
        assert cat.EventType(absent) not in cat.PRODUCED_NOW


def test_every_model_event_is_model_scoped() -> None:
    """A lifecycle event is not an account's, a strategy's or a bot's.

    Delivering one on an existing channel would send it to whoever happened to
    be subscribed there -- and a model event says what a deployment is running.
    """
    model_events = [
        t for t in cat.EventType if t.startswith(("MODEL_", "TRAINING_", "VALIDATION_"))
    ]
    assert len(model_events) == 11
    for event_type in model_events:
        assert cat.scope_of(event_type) is cat.Scope.model


def test_an_unknown_type_is_not_in_the_catalogue() -> None:
    assert not cat.is_known("ORDER_DEFINITELY_FILLED")
    with pytest.raises(KeyError):
        cat.spec_for("NOPE")


def test_the_frontend_catalogue_matches_the_backend_one() -> None:
    """A type on one side and not the other is a frame `parseEvent` drops in
    silence, which looks exactly like a market that never moved."""
    import pathlib
    import re

    source = (
        pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "realtime.ts"
    )
    if not source.exists():  # pragma: no cover - backend-only checkouts
        pytest.skip("frontend not present")
    block = re.search(r"export const EVENT_TYPES = \[(.*?)\] as const;", source.read_text(), re.S)
    assert block is not None
    frontend = set(re.findall(r'"([A-Z_]+)"', block.group(1)))
    assert frontend == {str(t) for t in cat.EventType}


# --------------------------------------------------------------- channels


@pytest.mark.parametrize(
    "raw,scope,ref",
    [
        ("system", cat.Scope.system, None),
        ("user:abc", cat.Scope.user, "abc"),
        ("account:a-1", cat.Scope.account, "a-1"),
        ("symbol:EURUSD", cat.Scope.symbol, "EURUSD"),
    ],
)
def test_channel_parsing(raw: str, scope: cat.Scope, ref: str | None) -> None:
    channel = ch.parse(raw)
    assert channel.scope is scope
    assert channel.ref == ref
    assert str(channel) == raw


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "account", "nope:1", "system:1", "account:", "account:has space", "account:*"],
)
def test_bad_channels_are_refused_not_defaulted(raw: str) -> None:
    with pytest.raises(ch.ChannelError):
        ch.parse(raw)


def test_the_catalogue_decides_the_channel_not_the_caller() -> None:
    """A publisher cannot route a private fill onto `system` by passing the
    wrong channel, because it does not choose the scope."""
    assert str(ch.channel_for_event("SYSTEM_ALERT", None)) == "system"
    assert str(ch.channel_for_event("ORDER_FILLED", "acc-1")) == "account:acc-1"
    with pytest.raises(ch.ChannelError):
        ch.channel_for_event("ORDER_FILLED", None)


async def test_a_user_cannot_subscribe_to_another_users_account(
    app: FastAPI, client: AsyncClient
) -> None:
    await _register(client, ALICE)
    async with app.state.session_factory() as db:
        alice = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert alice is not None
        db.add(
            PaperAccount(
                id="acct-someone-else",
                user_id="a-different-user",
                name="theirs",
                currency="USD",
                starting_balance=Decimal("1000"),
                balance=Decimal("1000"),
                equity=Decimal("1000"),
            )
        )
        await db.commit()
        with pytest.raises(ch.NotAuthorized):
            await ch.authorize(db, alice, ch.parse("account:acct-someone-else"))


async def test_a_user_may_subscribe_to_their_own_account(app: FastAPI, client: AsyncClient) -> None:
    await _register(client, ALICE)
    async with app.state.session_factory() as db:
        alice = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert alice is not None
        db.add(
            BrokerAccount(
                id="acct-mine",
                user_id=alice.id,
                name="mine",
                broker="mt5",
                account_mode="demo",
            )
        )
        await db.commit()
        await ch.authorize(db, alice, ch.parse("account:acct-mine"))  # does not raise


async def test_a_user_channel_belongs_to_that_user_alone(app: FastAPI, client: AsyncClient) -> None:
    await _register(client, ALICE)
    async with app.state.session_factory() as db:
        alice = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert alice is not None
        await ch.authorize(db, alice, ch.parse(f"user:{alice.id}"))
        with pytest.raises(ch.NotAuthorized):
            await ch.authorize(db, alice, ch.parse("user:somebody-else"))


async def test_a_plain_user_cannot_watch_a_strategy(app: FastAPI, client: AsyncClient) -> None:
    await _register(client, ALICE)
    async with app.state.session_factory() as db:
        alice = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert alice is not None
        with pytest.raises(ch.NotAuthorized):
            await ch.authorize(db, alice, ch.parse("strategy:rsi-1"))


async def test_a_missing_account_and_an_unowned_one_read_the_same(
    app: FastAPI, client: AsyncClient
) -> None:
    """Telling an unauthorized caller that an id exists is a membership
    oracle, so both answers are identical."""
    await _register(client, ALICE)
    async with app.state.session_factory() as db:
        alice = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert alice is not None
        db.add(
            PaperAccount(
                id="acct-real",
                user_id="someone-else",
                name="theirs",
                currency="USD",
                starting_balance=Decimal("1"),
                balance=Decimal("1"),
                equity=Decimal("1"),
            )
        )
        await db.commit()
        with pytest.raises(ch.NotAuthorized) as exists:
            await ch.authorize(db, alice, ch.parse("account:acct-real"))
        with pytest.raises(ch.NotAuthorized) as absent:
            await ch.authorize(db, alice, ch.parse("account:acct-imaginary"))
        assert str(exists.value) == str(absent.value)


# -------------------------------------------------------------------- hub


async def test_the_hub_refuses_an_uncatalogued_type() -> None:
    hub = Hub(InMemoryEventBus())
    with pytest.raises(EventError, match="not in the event catalogue"):
        await hub.publish(Event(type="MADE_UP", payload={}, channel="system"))


class _RefusingBus:
    """A bus whose SUBSCRIBE round trip fails, the way a dead Redis does."""

    kind = "redis"

    async def publish(self, event: Event) -> None:
        raise AssertionError("nothing publishes through the refusing bus")

    async def subscribe(self, *types: str) -> AsyncIterator[Event]:
        raise ConnectionError("Error 111 connecting to 127.0.0.1:6390")

    async def close(self) -> None:
        return None


async def test_a_bus_that_will_not_subscribe_leaves_the_hub_degraded_not_dead() -> None:
    """The asymmetry this closes: a bus that died a second AFTER startup left
    the process up with `status()` reporting it, while a bus that was already
    down raised out of `start` and took the platform with it."""
    hub = Hub(_RefusingBus())
    await hub.start()
    status = hub.status()
    assert status["reader_running"] is False
    assert status["bus_healthy"] is False
    assert "ConnectionError" in str(status["bus_error"])


async def test_the_hub_refuses_an_unrouted_event() -> None:
    hub = Hub(InMemoryEventBus())
    with pytest.raises(EventError, match="no channel"):
        await hub.publish(Event(type="SYSTEM_ALERT", payload={}))


async def test_fan_out_reaches_only_subscribers_of_that_channel() -> None:
    hub = Hub(InMemoryEventBus())
    mine = hub.add(Connection(id="c1", user_id="u1"))
    theirs = hub.add(Connection(id="c2", user_id="u2"))
    mine.subscribe(ch.parse("account:a1"))
    theirs.subscribe(ch.parse("account:a2"))
    delivered = hub.dispatch(
        Event(type="ORDER_FILLED", payload={"order_id": "o1"}, channel="account:a1")
    )
    assert delivered == 1
    assert mine.queue.qsize() == 1
    assert theirs.queue.qsize() == 0


async def test_a_slow_connection_is_dropped_rather_than_buffered_forever() -> None:
    """An unbounded queue behind a stalled socket is a memory leak that ends
    the process; taking the API down to keep one tab updated is worse."""
    hub = Hub(InMemoryEventBus())
    slow = hub.add(Connection(id="c1", user_id="u1"))
    slow.subscribe(ch.parse("system"))
    for _ in range(QUEUE_DEPTH + 5):
        hub.dispatch(Event(type="SYSTEM_ALERT", payload={"m": "x"}, channel="system"))
    assert slow.dropped is True
    assert slow.queue.qsize() == QUEUE_DEPTH
    assert hub.counters.events_dropped_slow >= 5


async def test_a_connection_cannot_hold_unlimited_subscriptions() -> None:
    connection = Connection(id="c", user_id="u")
    for i in range(ch.MAX_SUBSCRIPTIONS):
        connection.subscribe(ch.parse(f"symbol:S{i}"))
    with pytest.raises(ValueError, match="at most"):
        connection.subscribe(ch.parse("symbol:ONE_MORE"))


async def test_publish_failure_is_recorded_and_raised_not_swallowed() -> None:
    class Broken(InMemoryEventBus):
        async def publish(self, event: Event) -> None:
            raise ConnectionError("redis is gone")

    hub = Hub(Broken())
    with pytest.raises(ConnectionError):
        await hub.publish(system_alert("hello"))
    assert hub.counters.publish_failures == 1
    assert hub.status()["bus_healthy"] is False


async def test_end_to_end_through_the_bus_reader() -> None:
    bus = InMemoryEventBus()
    hub = Hub(bus)
    await hub.start()
    connection = hub.add(Connection(id="c1", user_id="u1"))
    connection.subscribe(ch.SYSTEM)
    await hub.publish(system_alert("maintenance in 5 minutes", correlation_id="req-1"))
    event = await asyncio.wait_for(connection.queue.get(), timeout=2)
    assert event.type == "SYSTEM_ALERT"
    assert event.channel == "system"
    assert event.correlation_id == "req-1"
    await hub.stop()


# ------------------------------------------------------------ idempotency


def test_seen_events_reports_a_repeat() -> None:
    """Receiving ORDER_FILLED twice must not create two trades."""
    seen = SeenEvents(capacity=3)
    assert seen.seen("e1") is False
    assert seen.seen("e1") is True
    assert seen.seen("e2") is False


def test_seen_events_is_bounded() -> None:
    seen = SeenEvents(capacity=2)
    seen.seen("a")
    seen.seen("b")
    seen.seen("c")
    assert len(seen) == 2
    # 'a' aged out, so it would be reprocessed -- which is why a consumer that
    # must never act twice also checks its own domain state.
    assert seen.seen("a") is False


# --------------------------------------------------------------- websocket


@pytest.fixture
def ws_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_an_unauthenticated_socket_is_closed_during_the_handshake(
    ws_client: TestClient,
) -> None:
    """Refused before `accept()`, so no socket ever exists without a user.

    The test client surfaces the close code; a real WebSocket client sees the
    handshake rejected with HTTP 403, because the upgrade never completes.
    Both were checked against the running server.
    """
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:  # noqa: PT012
        with ws_client.websocket_connect("/v1/realtime/ws"):
            pass
    assert exc.value.code == CLOSE_UNAUTHENTICATED


def test_a_signed_in_socket_connects_and_answers_ping(ws_client: TestClient) -> None:
    assert ws_client.post("/auth/register", json=ALICE).status_code == 201
    with ws_client.websocket_connect("/v1/realtime/ws") as socket:
        socket.send_text(json.dumps({"action": "ping"}))
        assert json.loads(socket.receive_text())["type"] == "PONG"


def test_subscribing_to_system_is_allowed_and_delivers(ws_client: TestClient) -> None:
    ws_client.post("/auth/register", json=ALICE)
    with ws_client.websocket_connect("/v1/realtime/ws") as socket:
        socket.send_text(json.dumps({"action": "subscribe", "channels": ["system"]}))
        reply = json.loads(socket.receive_text())
        assert reply["type"] == "SUBSCRIBED"
        assert reply["accepted"] == ["system"]
        assert reply["refused"] == []

        hub = ws_client.app.state.hub  # type: ignore[attr-defined]
        assert hub.subscriber_count("system") == 1
        hub.dispatch(Event(type="SYSTEM_ALERT", payload={"message": "hello"}, channel="system"))
        event = json.loads(socket.receive_text())
        assert event["type"] == "SYSTEM_ALERT"
        assert event["payload"] == {"message": "hello"}


def test_subscribing_to_someone_elses_account_is_refused_over_the_socket(
    ws_client: TestClient,
) -> None:
    """The cheapest attack there is: change the id in the frame."""
    ws_client.post("/auth/register", json=ALICE)
    with ws_client.websocket_connect("/v1/realtime/ws") as socket:
        socket.send_text(
            json.dumps({"action": "subscribe", "channels": ["system", "account:not-mine"]})
        )
        reply = json.loads(socket.receive_text())
        # The legal one is granted; only the illegal one is refused.
        assert reply["accepted"] == ["system"]
        assert [r["channel"] for r in reply["refused"]] == ["account:not-mine"]
        assert reply["channels"] == ["system"]


def test_an_unsubscribed_connection_receives_nothing(ws_client: TestClient) -> None:
    ws_client.post("/auth/register", json=ALICE)
    with ws_client.websocket_connect("/v1/realtime/ws") as socket:
        hub = ws_client.app.state.hub  # type: ignore[attr-defined]
        assert hub.dispatch(Event(type="SYSTEM_ALERT", payload={"m": "x"}, channel="system")) == 0
        socket.send_text(json.dumps({"action": "ping"}))
        assert json.loads(socket.receive_text())["type"] == "PONG"


def test_unsubscribe_stops_delivery(ws_client: TestClient) -> None:
    ws_client.post("/auth/register", json=ALICE)
    with ws_client.websocket_connect("/v1/realtime/ws") as socket:
        socket.send_text(json.dumps({"action": "subscribe", "channels": ["system"]}))
        socket.receive_text()
        socket.send_text(json.dumps({"action": "unsubscribe", "channels": ["system"]}))
        reply = json.loads(socket.receive_text())
        assert reply["type"] == "UNSUBSCRIBED"
        assert reply["channels"] == []
        hub = ws_client.app.state.hub  # type: ignore[attr-defined]
        assert hub.subscriber_count("system") == 0


@pytest.mark.parametrize(
    "frame",
    ["not json", '["a list"]', '{"action":"publish","type":"ORDER_FILLED"}', '{"action":"nope"}'],
)
def test_a_bad_frame_closes_the_socket(ws_client: TestClient, frame: str) -> None:
    """There is no publish verb. A browser that could publish onto the bus
    could publish ORDER_FILLED, and something downstream would believe it."""
    from starlette.websockets import WebSocketDisconnect

    ws_client.post("/auth/register", json=ALICE)
    with pytest.raises(WebSocketDisconnect) as exc:  # noqa: PT012
        with ws_client.websocket_connect("/v1/realtime/ws") as socket:
            socket.send_text(frame)
            socket.receive_text()
    assert exc.value.code == CLOSE_BAD_FRAME


def test_an_oversized_frame_is_refused(ws_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    ws_client.post("/auth/register", json=ALICE)
    with pytest.raises(WebSocketDisconnect) as exc:  # noqa: PT012
        with ws_client.websocket_connect("/v1/realtime/ws") as socket:
            socket.send_text(json.dumps({"action": "ping", "pad": "x" * 8192}))
            socket.receive_text()
    assert exc.value.code == CLOSE_BAD_FRAME


def test_closing_releases_the_connection(ws_client: TestClient) -> None:
    """An abandoned subscription that outlives its socket leaks for as long as
    the process lives."""
    ws_client.post("/auth/register", json=ALICE)
    hub = ws_client.app.state.hub  # type: ignore[attr-defined]
    with ws_client.websocket_connect("/v1/realtime/ws") as socket:
        socket.send_text(json.dumps({"action": "subscribe", "channels": ["system"]}))
        socket.receive_text()
        assert len(hub.connections) == 1
    assert len(hub.connections) == 0
    assert hub.subscriber_count("system") == 0


# ------------------------------------------------------------------- routes


async def test_channels_route_lists_only_this_users_channels(
    app: FastAPI, client: AsyncClient
) -> None:
    await _register(client, ALICE)
    async with app.state.session_factory() as db:
        alice = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert alice is not None
        db.add(
            BrokerAccount(
                id="acct-mine", user_id=alice.id, name="mine", broker="mt5", account_mode="demo"
            )
        )
        db.add(
            BrokerAccount(
                id="acct-theirs",
                user_id="someone-else",
                name="theirs",
                broker="mt5",
                account_mode="demo",
            )
        )
        await db.commit()
    body = (await client.get("/v1/realtime/channels")).json()
    assert "account:acct-mine" in body["subscribable"]
    assert "account:acct-theirs" not in body["subscribable"]
    assert body["endpoint"] == "/v1/realtime/ws"


async def test_catalogue_route_says_what_is_actually_produced(client: AsyncClient) -> None:
    await _register(client, ALICE)
    body = (await client.get("/v1/realtime/catalogue")).json()
    assert len(body["events"]) == 52
    assert "SYSTEM_ALERT" in body["producing_now"]
    assert "MODEL_ACTIVATED" in body["producing_now"]
    filled = next(e for e in body["events"] if e["type"] == "ORDER_FILLED")
    assert filled["producing_now"] is False
    assert filled["produced_from_level"] == 19


async def test_realtime_routes_need_a_session(client: AsyncClient) -> None:
    assert (await client.get("/v1/realtime/channels")).status_code == 401
    assert (await client.get("/v1/realtime/catalogue")).status_code == 401
    assert (await client.get("/v1/realtime/status")).status_code == 401


async def test_status_needs_system_settings_permission(app: FastAPI, client: AsyncClient) -> None:
    await _register(client, ALICE)
    assert (await client.get("/v1/realtime/status")).status_code == 403
    await _promote(app, ALICE["email"], Role.admin)
    body = (await client.get("/v1/realtime/status")).json()
    assert body["bus"] == "in_memory"
    assert set(body) >= {"connections", "events_published", "events_dropped_slow"}


# -------------------------------------------------------------- safety


async def test_nothing_in_the_realtime_package_can_trade() -> None:
    """A bug in the hub can drop a message or deliver it twice. It cannot
    place an order, and this asserts the absence rather than trusting it."""
    import importlib
    import pkgutil

    import app.realtime as pkg

    forbidden = {"MetaTrader5", "mt5_paper", "OrderRequest", "BrokerAdapter"}
    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.realtime."):
        loaded = importlib.import_module(module.name)
        assert not (set(dir(loaded)) & forbidden), f"{module.name} reaches execution"


async def test_a_realtime_event_carries_no_credential() -> None:
    event = system_alert("all good")
    body = event.encode().lower()
    for leak in ("password", "secret", "token", "cookie", "api_key", "postgresql"):
        assert leak not in body
