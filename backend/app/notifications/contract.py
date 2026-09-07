"""The notification vocabulary: severity, category, channel, delivery status.

Sections 8, 10 and 11. Enums only, plus the envelope a channel adapter is
given. Nothing here reads a database, calls a service or sends anything, so
the vocabulary can be imported by a template, a preference and a test without
dragging the delivery machinery in behind it.

**Severity is operational importance, not a log level.** Section 8 asks for
five and no more. The mapping that matters:

    a trade closed              INFO
    a risk limit is approached  WARNING
    a risk limit was breached   ERROR
    the kill switch latched     CRITICAL

`SUCCESS` sits beside `INFO` rather than above it: "the thing you asked for
worked" is not more urgent than "something happened", it is a different
colour on the same shelf.

**A category is what a preference is expressed against.** A user does not
switch off `TRADE_RECONCILIATION_REQUIRED`; they switch off trading email.
Categories are therefore coarse, stable and few -- one per area of the
platform an operator thinks about separately.

**Channel is a destination, not a transport detail.** `IN_APP` is the record
in this platform, `EMAIL` is L34's outbound provider, `DISCORD` is L35's. The
enum carries all three from L34 so the routing table, the preference schema
and the delivery table do not change shape when L35 lands; the `DISCORD`
adapter at L34 is the one that says it is not configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """Section 8. Five, ordered by operational importance."""

    info = "INFO"
    success = "SUCCESS"
    warning = "WARNING"
    error = "ERROR"
    critical = "CRITICAL"


#: Rank, for comparison and for the delivery worker's priority. `SUCCESS`
#: shares INFO's rank on purpose: it is a different reading of the same
#: urgency, not a step up from it.
SEVERITY_RANK: dict[Severity, int] = {
    Severity.info: 0,
    Severity.success: 0,
    Severity.warning: 1,
    Severity.error: 2,
    Severity.critical: 3,
}

#: Severities a user may never switch off in-app. Sections 16 and 17: a
#: preference is about being *told*, and switching off the record of a breach
#: is how an operator learns about one from the broker instead.
UNSUPPRESSIBLE: frozenset[Severity] = frozenset({Severity.error, Severity.critical})


def at_least(have: Severity, floor: Severity) -> bool:
    return SEVERITY_RANK[have] >= SEVERITY_RANK[floor]


class Category(StrEnum):
    """Section 4. What a preference is expressed against.

    Coarse on purpose. A per-event-type preference table would hold fifty rows
    per user, forty of which name an event nothing produces yet, and a user
    would have to revisit it every time a level added a type.
    """

    trading = "TRADING"  # orders, executions, positions, journalled trades
    risk = "RISK"  # limits, breaches, the kill switch
    portfolio = "PORTFOLIO"  # balance, exposure, drawdown, valuation health
    bots = "BOTS"  # bot lifecycle
    strategies = "STRATEGIES"  # strategy and signal lifecycle
    ai = "AI"  # training, validation, model lifecycle, trade reviews
    monitoring = "MONITORING"  # model health and monitoring alerts
    broker = "BROKER"  # adapter connectivity and reconciliation
    system = "SYSTEM"  # platform notices, worker and dependency health
    security = "SECURITY"  # authentication and privilege events


class Channel(StrEnum):
    """Section 11. Destinations, not transports."""

    in_app = "IN_APP"
    email = "EMAIL"
    discord = "DISCORD"  # the adapter arrives at L35; the seat exists here


class DeliveryStatus(StrEnum):
    """Section 10."""

    pending = "PENDING"
    processing = "PROCESSING"
    delivered = "DELIVERED"
    failed = "FAILED"
    retrying = "RETRYING"
    skipped = "SKIPPED"  # the channel is not configured, or a preference said no


#: Statuses a delivery never leaves. The worker passes over them rather than
#: re-reading them every pass.
TERMINAL: frozenset[DeliveryStatus] = frozenset(
    {DeliveryStatus.delivered, DeliveryStatus.failed, DeliveryStatus.skipped}
)


class Audience(StrEnum):
    """Who a catalogue entry is addressed to.

    Resolved from the event's own routing, never from its payload. An `owner`
    event reaches whoever owns the account or bot it was published on; the
    other three are role-derived, because a model lifecycle transition and a
    platform notice have no single owner.
    """

    owner = "OWNER"  # the account's or bot's owner
    operators = "OPERATORS"  # everyone who may manage AI models (trader and above)
    admins = "ADMINS"  # everyone who may change system settings
    everyone = "EVERYONE"  # every active user


#: The four environments a notification may be stamped with, plus the honest
#: fifth. Section 23: a paper trade must never read as a live one, and where
#: the platform genuinely does not know, it says so rather than defaulting to
#: the safe-looking word. "unknown" is a real state, not a placeholder.
ENVIRONMENTS: tuple[str, ...] = ("backtest", "paper", "demo", "live", "unknown")


@dataclass(frozen=True)
class NotificationEnvelope:
    """What a channel adapter is handed. Sections 7 and 22.

    Every field is a fact the platform recorded or derived from one. There is
    no room in it for an estimate: a template that wants a figure the event did
    not carry leaves the line out (see `templates.py`), and an adapter cannot
    add one because it never sees anything but this.

    It carries no credential, no session and no broker identifier, so nothing
    an adapter logs or transmits can leak one.
    """

    notification_id: str
    event_id: str
    event_type: str
    category: Category
    severity: Severity
    environment: str
    title: str
    body: str
    user_id: str
    #: What the notification is *about*, for the frontend to resolve into a
    #: route. Section 35: the backend names the entity, the browser decides the
    #: URL, so a route rename does not invalidate a stored notification.
    entity_type: str | None = None
    entity_id: str | None = None
    created_at: datetime | None = None
    #: The event payload filtered to the fields the template actually used.
    #: Never the raw event: an event body may carry more than a channel needs.
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "category": str(self.category),
            "severity": str(self.severity),
            "environment": self.environment,
            "title": self.title,
            "body": self.body,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class DeliveryResult:
    """What an adapter reports back. Sections 26, 30 and 43.

    `retryable` is the adapter's judgement and the worker's instruction: a
    timeout is retryable, a webhook that does not exist is not. Section 30 asks
    for exactly that distinction, because retrying a permanent error forever is
    how a queue stops draining.

    `provider_message_id` is recorded when the provider returns one, so a
    message in a channel can be traced back to the event that caused it.
    """

    status: DeliveryStatus
    detail: str = ""
    retryable: bool = False
    provider_message_id: str | None = None
    #: Seconds the provider asked us to wait, when it said so (Discord's
    #: `retry_after`, an SMTP 4xx carrying a delay). None means use the backoff.
    retry_after_seconds: float | None = None

    @property
    def delivered(self) -> bool:
        return self.status is DeliveryStatus.delivered


__all__ = [
    "ENVIRONMENTS",
    "SEVERITY_RANK",
    "TERMINAL",
    "UNSUPPRESSIBLE",
    "Audience",
    "Category",
    "Channel",
    "DeliveryResult",
    "DeliveryStatus",
    "NotificationEnvelope",
    "Severity",
    "at_least",
]
