"""Channel adapters, and the registry that holds one of each.

`build_registry` is the single place a deployment decides what it can deliver
on. The API, the delivery worker and the health check all read the registry it
returns, so all three answer from the same object -- three independent readings
of "is email configured" would eventually be three different answers.
"""

from __future__ import annotations

from typing import Any

from app.notifications.channels.base import (
    ChannelRegistry,
    ChannelState,
    NotificationChannelAdapter,
    UnavailableChannel,
)
from app.notifications.channels.discord import discord_channel_from
from app.notifications.channels.email import (
    EmailChannel,
    EmailProvider,
    NotificationResetDelivery,
    provider_from,
)
from app.notifications.channels.inapp import InAppChannel


def build_registry(settings: Any, hub: Any | None = None) -> ChannelRegistry:
    """Every adapter this process has, built from configuration alone.

    Nothing here connects, authenticates or sends. Constructing an SMTP
    provider opens no socket -- the same rule the broker registry follows, and
    for the same reason: a process that reached out to three providers on
    startup would fail to start because one of them was down.
    """
    registry = ChannelRegistry()
    registry.register(InAppChannel(hub))
    registry.register(
        EmailChannel(
            provider_from(settings),
            enabled=bool(getattr(settings, "email_notifications_enabled", True)),
        )
    )
    registry.register(discord_channel_from(settings))
    return registry


__all__ = [
    "ChannelRegistry",
    "ChannelState",
    "EmailChannel",
    "EmailProvider",
    "InAppChannel",
    "NotificationChannelAdapter",
    "NotificationResetDelivery",
    "UnavailableChannel",
    "build_registry",
    "discord_channel_from",
    "provider_from",
]
