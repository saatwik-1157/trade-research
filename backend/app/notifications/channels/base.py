"""The channel abstraction: one interface, three destinations.

Section 11 and section 27. A channel adapter is handed a
`NotificationEnvelope` and reports a `DeliveryResult`. That is the whole
contract, and it is deliberately narrow:

  * **An adapter receives no session and no database.** It cannot read another
    user's notification, cannot write a delivery row and cannot mark anything
    read. Persistence belongs to `service.py`; an adapter sends.
  * **An adapter receives no credential.** Its configuration is read from
    settings inside its own constructor and never leaves it. Nothing an
    adapter is given can leak a secret, because nothing it is given contains
    one -- section 43 of L35 and section 50 of L34 are then properties of the
    signature rather than promises about the code.
  * **An adapter never raises into the worker.** It classifies its own failure
    as retryable or not and returns it. A raised exception is caught by the
    worker and treated as retryable, which is the safe reading, but an adapter
    that means "this webhook does not exist" should say so -- retrying a
    permanent error forever is how a queue stops draining (section 30).

`describe()` is what the health endpoint and the admin panel read. It reports
whether the channel is configured and never how: `configured: true` and never
the URL, the host or the token.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.notifications.contract import (
    Channel,
    DeliveryResult,
    DeliveryStatus,
    NotificationEnvelope,
)


class ChannelState:
    """The words a channel's availability is reported in. Section 41 of L35."""

    not_configured = "NOT_CONFIGURED"
    disabled = "DISABLED"
    configured = "CONFIGURED"


@runtime_checkable
class NotificationChannelAdapter(Protocol):
    """One destination. Section 11."""

    channel: Channel

    @property
    def available(self) -> bool:
        """Whether a send would have somewhere to go.

        False is not an error state. A platform with no SMTP server configured
        is a correctly configured platform that does not send email, and
        section 41 of L35 is explicit that an optional integration must not
        make startup fail.
        """
        ...

    async def send(self, envelope: NotificationEnvelope) -> DeliveryResult: ...

    def describe(self) -> dict[str, Any]:
        """Status only. Never a URL, a host, a token or a redacted secret."""
        ...


class UnavailableChannel:
    """A channel with nothing behind it. It refuses rather than pretending.

    This is what `DISCORD` is at L34: the seat exists so the routing table, the
    preference grid and the delivery schema already have a shape for it, and
    L35 replaces the adapter without any of them changing. It is also what
    `EMAIL` is on a deployment with no SMTP host.

    The refusal is `SKIPPED`, not `FAILED`, and the distinction matters:
    FAILED reads as "we tried and the provider broke", which is a different
    operational fact from "nobody ever configured this".
    """

    def __init__(self, channel: Channel, reason: str, state: str = ChannelState.not_configured):
        self.channel = channel
        self.reason = reason
        self.state = state

    @property
    def available(self) -> bool:
        return False

    async def send(self, envelope: NotificationEnvelope) -> DeliveryResult:
        return DeliveryResult(
            status=DeliveryStatus.skipped,
            detail=self.reason,
            retryable=False,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "channel": str(self.channel),
            "state": self.state,
            "available": False,
            "detail": self.reason,
        }


class ChannelRegistry:
    """The adapters this process has. One per channel, at most.

    Built once at startup and read by the worker, the API and the health
    check, so all three answer from the same object rather than each deciding
    for itself whether email is configured.
    """

    def __init__(self) -> None:
        self._adapters: dict[Channel, NotificationChannelAdapter] = {}

    def register(self, adapter: NotificationChannelAdapter) -> NotificationChannelAdapter:
        self._adapters[adapter.channel] = adapter
        return adapter

    def get(self, channel: Channel) -> NotificationChannelAdapter:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return UnavailableChannel(
                channel, f"no adapter is registered for {channel} in this process"
            )
        return adapter

    def available(self) -> list[Channel]:
        """Channels that could actually deliver something right now."""
        return [c for c in Channel if self.get(c).available]

    def describe(self) -> list[dict[str, Any]]:
        return [self.get(c).describe() for c in Channel]


__all__ = [
    "ChannelRegistry",
    "ChannelState",
    "NotificationChannelAdapter",
    "UnavailableChannel",
]
