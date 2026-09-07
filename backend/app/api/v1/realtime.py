"""Realtime routes: the WebSocket, the channel list and the event catalogue.

`/v1/realtime/channels` answers what *this* user may subscribe to, computed
from their accounts and bots rather than described in general terms, so a
client does not have to guess and then be refused.

`/v1/realtime/catalogue` publishes the event contract, including which level
starts producing each type. A type that nothing produces yet says so, because
"subscribed and silent" and "subscribed to something that cannot happen yet"
look identical from a browser and are not the same thing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.realtime import catalogue as cat
from app.realtime import channels as ch
from app.realtime import ws as ws_protocol
from app.realtime.hub import MAX_CLIENT_FRAME_BYTES

router = APIRouter(prefix="/realtime", tags=["realtime"])

_SIGNED_IN = Depends(current_user)
_SYSTEM_SETTINGS = Depends(require_permission(Permission.manage_system_settings))


@router.get(
    "/channels",
    summary="The channels this user may subscribe to",
    description=(
        "Account and bot channels are listed because a caller cannot guess "
        "them. Symbol and strategy channels are open-ended and described "
        "instead. Subscribing is authorized again on every subscribe frame, "
        "against the database -- this list is a convenience, not the gate."
    ),
)
async def channels(
    db: AsyncSession = Depends(get_db), user: User = _SIGNED_IN
) -> dict[str, object]:
    return {
        "endpoint": "/v1/realtime/ws",
        "protocol": {
            "subscribe": {"action": "subscribe", "channels": ["account:<id>"]},
            "unsubscribe": {"action": "unsubscribe", "channels": ["account:<id>"]},
            "ping": {"action": "ping"},
            "max_channels_per_connection": ch.MAX_SUBSCRIPTIONS,
            "max_client_frame_bytes": MAX_CLIENT_FRAME_BYTES,
            "server_ping_seconds": ws_protocol.PING_SECONDS,
        },
        "subscribable": await ch.authorized_channels(db, user),
        "open_ended": {
            "symbol": "symbol:<internal code>, e.g. symbol:EURUSD (needs a signed-in session)",
            "strategy": "strategy:<id> (needs the trader role)",
        },
    }


@router.get(
    "/catalogue",
    summary="Every event type, its scope, and the level that starts producing it",
    description=(
        "`producing_now` is false for a type whose emitter has not been built. "
        "Subscribing to it is legal and will simply never fire, and the field "
        "exists so that silence can be told apart from a missing producer."
    ),
)
async def event_catalogue(_: User = _SIGNED_IN) -> dict[str, object]:
    return {
        "events": cat.as_dict(),
        "scopes": [str(s) for s in cat.Scope],
        "producing_now": sorted(str(t) for t in cat.PRODUCED_NOW),
    }


@router.get(
    "/status",
    summary="Hub and bus observability",
    description=(
        "Counts only, never payloads. `bus_healthy` reports what the last "
        "publish actually did rather than whether a URL is configured."
    ),
)
async def status(request: Request, _: User = _SYSTEM_SETTINGS) -> dict[str, object]:
    return request.app.state.hub.status()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """One socket per client. Authentication runs before the accept.

    The session *factory* is passed rather than a session: a WebSocket is not
    an HTTP request, and holding one request-scoped session open for the life
    of a socket would pin a pool connection for as long as the browser tab
    stays open. `serve` opens one per check and closes it.
    """
    app = websocket.app
    settings = app.state.settings
    await ws_protocol.serve(
        websocket, app.state.session_factory, app.state.hub, settings.session_cookie_name
    )
