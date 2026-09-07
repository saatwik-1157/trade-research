"""The WebSocket endpoint and its protocol.

**Authentication happens before the socket is accepted.** An unauthenticated
connection is refused during the handshake, so there is never a window in
which a socket exists without a user behind it. The credential is the same
server-side session cookie the REST surface uses -- no second token
mechanism, so revoking a session closes the door here too.

What the client observes depends on where it is standing, and both were
measured against the running server: a real WebSocket client sees the
handshake rejected with **HTTP 403**, because closing before `accept()` never
completes the upgrade; Starlette's test client surfaces the close code
`CLOSE_UNAUTHENTICATED` instead. Rejecting at the handshake is the safer of
the two options available -- accepting first so the 4401 is visible would
create exactly the window this paragraph says does not exist.

The client protocol is four frames in and three out, all JSON:

  in   {"action":"subscribe",   "channels":["account:abc"]}
       {"action":"unsubscribe", "channels":["account:abc"]}
       {"action":"ping"}
  out  {"type":"SUBSCRIBED",   "channels":[...], "refused":[{...}]}
       {"type":"PONG", "at":"..."}
       an event envelope, as `app.core.events.Event.encode` produces it

A refused subscription is reported per channel and does not close the socket
or cancel the others: a client asking for four channels and holding three
should get three, and be told precisely which one it may not have.

**Nothing a client sends can cause a trade.** The only verbs are subscribe,
unsubscribe and ping. There is no publish frame, by design: a browser that
could publish onto the bus could publish ORDER_FILLED, and something
downstream would eventually believe it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import User
from app.auth.service import user_for_token
from app.core.events import Event
from app.realtime import channels as ch
from app.realtime.hub import MAX_CLIENT_FRAME_BYTES, Connection, ConnectionLimit, Hub

log = logging.getLogger("app.realtime")

# Close codes in the application range (4000-4999). 4401/4403 mirror HTTP
# 401/403 so a client can tell "sign in again" from "you may not have this".
# CLOSE_UNAUTHENTICATED is used before `accept()`, where a real client sees an
# HTTP 403 handshake rejection rather than this code; the others are sent on an
# established socket and reach the client as written.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_BAD_FRAME = 4400
CLOSE_TOO_SLOW = 4408
# L39. The account already holds the maximum number of live sockets. A
# distinct code from 4403 on purpose: a client that is over its limit
# should back off and retry, and one that is forbidden should not.
CLOSE_TOO_MANY = 4429

# The server pings on this interval and expects the socket to answer. A
# connection that has not been readable for PING_SECONDS * MISSES is dead
# whatever TCP believes, and its subscriptions are released.
PING_SECONDS = 20.0
PING_MISSES = 3


async def authenticate(websocket: WebSocket, db: AsyncSession, cookie_name: str) -> User | None:
    """The session cookie, checked before the handshake is accepted."""
    token = websocket.cookies.get(cookie_name)
    return await user_for_token(db, token)


def _decode(raw: str) -> dict:
    if len(raw.encode("utf-8")) > MAX_CLIENT_FRAME_BYTES:
        raise ValueError("frame too large")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("frame must be an object")
    return parsed


async def _apply_subscriptions(
    sessions: async_sessionmaker[AsyncSession],
    user: User,
    connection: Connection,
    names: object,
    *,
    subscribing: bool,
    hub: Hub,
) -> dict[str, object]:
    """Subscribe or unsubscribe, reporting each refusal by name."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("channels must be a list of strings")
    if len(names) > ch.MAX_SUBSCRIPTIONS:
        raise ValueError(f"at most {ch.MAX_SUBSCRIPTIONS} channels per frame")

    accepted: list[str] = []
    refused: list[dict[str, str]] = []
    for name in names:
        try:
            channel = ch.parse(name)
            if subscribing:
                # A short-lived session per frame. Authorization is re-read
                # from the database every time: a role or an account can be
                # revoked while a socket is open, and a long-lived connection
                # must not outlive the permission that opened it.
                async with sessions() as db:
                    await ch.authorize(db, user, channel)
                connection.subscribe(channel)
            else:
                connection.unsubscribe(channel)
            accepted.append(str(channel))
        except (ch.ChannelError, ValueError) as exc:
            hub.counters.subscribe_refusals += 1
            refused.append({"channel": name[:128], "reason": str(exc)})
        except ch.NotAuthorized as exc:
            hub.counters.subscribe_refusals += 1
            # Logged with the user so a probing client is visible, and
            # answered without confirming whether the id exists.
            log.warning(
                "subscription refused",
                extra={
                    "event": "realtime_subscribe_refused",
                    "user_id": user.id,
                    "channel": name[:128],
                },
            )
            refused.append({"channel": name[:128], "reason": str(exc)})
    return {
        "type": "SUBSCRIBED" if subscribing else "UNSUBSCRIBED",
        "channels": sorted(connection.channels),
        "accepted": accepted,
        "refused": refused,
    }


async def _pump(websocket: WebSocket, connection: Connection) -> None:
    """Queue -> socket. The only place an event reaches a browser."""
    while True:
        event = await connection.queue.get()
        await websocket.send_text(event.encode())


async def serve(
    websocket: WebSocket,
    sessions: async_sessionmaker[AsyncSession],
    hub: Hub,
    cookie_name: str,
    *,
    max_frames: int | None = None,
) -> None:
    """Handle one connection from handshake to close.

    Takes the session *factory*, not a session. A socket lives as long as the
    browser tab, and holding one request-scoped session open for that whole
    time would pin a pool connection per idle tab. Sessions are opened per
    check and closed immediately.

    `max_frames` exists for tests: it bounds the read loop so a test can drive
    a finite conversation without racing a background task.
    """
    async with sessions() as db:
        user = await authenticate(websocket, db, cookie_name)
    if user is None:
        # Closed during the handshake: the socket is never accepted, so no
        # frame can be exchanged before authentication.
        await websocket.close(code=CLOSE_UNAUTHENTICATED, reason="not signed in")
        return

    await websocket.accept()
    try:
        connection = hub.add(Connection(id=uuid.uuid4().hex, user_id=user.id))
    except ConnectionLimit as exc:
        # Accepted and then closed rather than refused at the handshake: a
        # browser is told nothing useful about a rejected handshake, so a
        # client hitting its own limit would see an indistinguishable network
        # error and retry forever. The close frame carries the reason.
        log.warning(
            "realtime connection refused",
            extra={
                "event": "realtime_refused",
                "user_id": user.id,
                "reason": "connection_limit",
            },
        )
        await websocket.close(code=CLOSE_TOO_MANY, reason=str(exc)[:120])
        return
    log.info(
        "realtime connection opened",
        extra={"event": "realtime_open", "connection": connection.id, "user_id": user.id},
    )
    pump = asyncio.create_task(_pump(websocket, connection), name=f"ws:pump:{connection.id}")
    frames = 0
    try:
        while True:
            if max_frames is not None and frames >= max_frames:
                break
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=PING_SECONDS * PING_MISSES
                )
            except TimeoutError:
                # Nothing for three ping intervals. The peer is gone whatever
                # the socket thinks; holding its subscriptions open would leak
                # them for as long as the process lives.
                await websocket.close(code=CLOSE_TOO_SLOW, reason="no traffic")
                break
            frames += 1
            try:
                frame = _decode(raw)
            except (ValueError, json.JSONDecodeError):
                await websocket.close(code=CLOSE_BAD_FRAME, reason="undecodable frame")
                break

            action = frame.get("action")
            if action == "ping":
                await websocket.send_text(
                    json.dumps(
                        {"type": "PONG", "at": datetime.now(UTC).isoformat(timespec="seconds")}
                    )
                )
            elif action in ("subscribe", "unsubscribe"):
                try:
                    reply = await _apply_subscriptions(
                        sessions,
                        user,
                        connection,
                        frame.get("channels"),
                        subscribing=action == "subscribe",
                        hub=hub,
                    )
                except ValueError as exc:
                    await websocket.close(code=CLOSE_BAD_FRAME, reason=str(exc)[:120])
                    break
                await websocket.send_text(json.dumps(reply))
            else:
                await websocket.close(
                    code=CLOSE_BAD_FRAME, reason="action must be subscribe, unsubscribe or ping"
                )
                break

            if connection.dropped:
                await websocket.close(code=CLOSE_TOO_SLOW, reason="client fell behind")
                break
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        hub.remove(connection.id)
        log.info(
            "realtime connection closed",
            extra={"event": "realtime_close", "connection": connection.id, "user_id": user.id},
        )


def system_alert(message: str, *, correlation_id: str | None = None, **fields: object) -> Event:
    """The one event this level can actually produce.

    Everything else in the catalogue is a contract whose producer arrives with
    its level. Publishing a fabricated ORDER_FILLED to demonstrate the
    transport would put a fill on the bus that no venue reported, which is the
    exact confusion the whole architecture is built to prevent.
    """
    from app.realtime.catalogue import EventType

    return Event(
        type=str(EventType.SYSTEM_ALERT),
        payload={"message": message, **fields},
        channel=str(ch.SYSTEM),
        correlation_id=correlation_id,
        source="api",
    )
