"""The in-app channel: the platform's own record, plus a nudge down the socket.

Sections 12, 13 and 14.

**The row is the delivery.** By the time this adapter runs, the notification
has been persisted -- `service.py` writes it first and queues delivery after,
which is section 13's rule stated as an ordering: *persist before realtime
delivery, so a temporarily disconnected browser does not lose the
notification*. So `send` reports DELIVERED because the durable part already
happened, and the WebSocket frame is a nudge on top of it.

**A failed publish is not a failed delivery.** If the bus is down the frame is
not sent and the notification is still there; the browser will read it on its
next `GET /v1/notifications`. Reporting FAILED here and retrying would mean
retrying something that already succeeded. The publish failure is recorded in
the delivery's detail so it is visible, and `app.realtime.hub` counts it.

**It publishes NOTIFICATION_CREATED and nothing else.** That type has been in
the catalogue since L07 scoped to `user`, and `channels.py` has authorized
`user:{id}` to exactly that user since then. L34 is the level that starts
producing it -- no new type, no new scope, no new authorization rule.

The frame carries the notification's identity and its headline, not its whole
body: the hub's own docstring says an event is a nudge that something changed
and a consumer that needs the value reads it back.
"""

from __future__ import annotations

import logging
from typing import Any

from app.notifications.channels.base import ChannelState
from app.notifications.contract import (
    Channel,
    DeliveryResult,
    DeliveryStatus,
    NotificationEnvelope,
)

log = logging.getLogger("app.notifications.inapp")

EVENT_TYPE = "NOTIFICATION_CREATED"


class InAppChannel:
    """Always available: it needs no provider, only the hub it already has."""

    channel = Channel.in_app

    def __init__(self, hub: Any | None = None) -> None:
        self.hub = hub

    @property
    def available(self) -> bool:
        # True even with no hub. The record exists either way, and that is what
        # in-app delivery IS. A deployment with no realtime bus still has a
        # working notification centre; it just does not update without a reload.
        return True

    async def send(self, envelope: NotificationEnvelope) -> DeliveryResult:
        published = await self._publish(envelope)
        return DeliveryResult(
            status=DeliveryStatus.delivered,
            detail=(
                "stored and published on the user channel"
                if published
                else "stored; the realtime frame was not published, so the browser will "
                "see it on its next read rather than immediately"
            ),
            provider_message_id=envelope.notification_id,
        )

    async def _publish(self, envelope: NotificationEnvelope) -> bool:
        if self.hub is None:
            return False
        from app.core.events import Event

        try:
            await self.hub.publish(
                Event(
                    type=EVENT_TYPE,
                    payload={
                        "notification_id": envelope.notification_id,
                        "event_type": envelope.event_type,
                        "category": str(envelope.category),
                        "severity": str(envelope.severity),
                        "environment": envelope.environment,
                        "title": envelope.title,
                        "entity_type": envelope.entity_type,
                        "entity_id": envelope.entity_id,
                    },
                    source="notifications",
                    channel=f"user:{envelope.user_id}",
                    user_id=envelope.user_id,
                )
            )
        except Exception:  # noqa: BLE001 - a stored notification is not lost by a socket
            log.warning(
                "a notification frame could not be published",
                extra={
                    "event": "notification_publish_failed",
                    "notification_id": envelope.notification_id,
                },
            )
            return False
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "channel": str(self.channel),
            "state": ChannelState.configured,
            "available": True,
            "realtime": self.hub is not None,
            "detail": (
                "the notification centre. Records are stored before any realtime "
                "frame is sent, so a disconnected browser loses nothing."
            ),
        }


__all__ = ["EVENT_TYPE", "InAppChannel"]
