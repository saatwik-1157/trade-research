"""The observability vocabulary: component states, categories, trading safety.

Sections 11, 40 and 52. Enums and dataclasses only -- no probe, no session, no
clock -- so the vocabulary can be imported by a collector, an incident and a
test without dragging the machinery in behind it.

**Five component states, not two.** Section 11 is explicit that a component
must not be forced into healthy/unhealthy. `NOT_CONFIGURED` and `UNKNOWN` are
the two that earn their place: Discord with no webhook is *correctly configured
and not sending*, which is a different fact from a webhook that broke, and a
broker adapter nobody registered is *not observed*, which is a different fact
from one that is down. Collapsing either into "unhealthy" is how an operator
spends an afternoon looking for a fault that does not exist.

**`HealthStatus` is not replaced.** `app/core/health.py` has had three states
since L02 and `/health/ready` derives its HTTP code from them. This enum is a
superset used for the richer per-component view, and `from_health` maps the old
onto the new so there is one translation rather than two vocabularies drifting.

**Trading safety is derived, never asserted.** Section 40: it comes from the
authoritative components and a UI may not invent it. It is also purely
observational -- nothing in the platform reads it to decide whether to trade,
and `app/observability` imports no risk engine, no order manager and no adapter
to make sure it could not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.health import HealthStatus


class ComponentState(StrEnum):
    """Section 11. What a single component is doing."""

    healthy = "HEALTHY"
    degraded = "DEGRADED"
    unhealthy = "UNHEALTHY"
    #: Observed as absent, not observed as broken. A registry with no adapter,
    #: a feed with no tick yet, a check whose input does not exist.
    unknown = "UNKNOWN"
    #: Deliberately switched off or never set up. A correctly configured
    #: platform that does not send email is in this state, not in `unhealthy`.
    not_configured = "NOT_CONFIGURED"


#: How bad each state is, for aggregation. `NOT_CONFIGURED` ranks below
#: `UNKNOWN` on purpose: "nobody set this up" is a decision, "we cannot see it"
#: is a gap in the monitoring.
STATE_RANK: dict[ComponentState, int] = {
    ComponentState.healthy: 0,
    ComponentState.not_configured: 1,
    ComponentState.unknown: 2,
    ComponentState.degraded: 3,
    ComponentState.unhealthy: 4,
}


def from_health(status: HealthStatus) -> ComponentState:
    """L02's three states onto L37's five. One translation, in one place."""
    return {
        HealthStatus.healthy: ComponentState.healthy,
        HealthStatus.degraded: ComponentState.degraded,
        HealthStatus.unavailable: ComponentState.unhealthy,
    }[status]


class Layer(StrEnum):
    """Section 38's dashboard sections, as data rather than a UI convention."""

    infrastructure = "INFRASTRUCTURE"  # database, redis, event bus, api
    workers = "WORKERS"  # background loops and their queues
    market_data = "MARKET_DATA"
    trading = "TRADING"  # broker, OMS, positions, risk
    ai = "AI"  # models, monitoring, providers
    notifications = "NOTIFICATIONS"  # channels and delivery
    realtime = "REALTIME"  # the hub and its sockets
    # L39. Which security controls are actually in force, reported here rather
    # than on a security dashboard of its own: two views of one fact eventually
    # disagree, and the one on the screen is not the one the system acted on.
    security = "SECURITY"


class Criticality(StrEnum):
    """Section 39. Overall health is not an average.

    `critical` means the platform cannot do its job without it. `important`
    means something is lost. `optional` means a correctly configured deployment
    may simply not have it, and its absence must never make the platform report
    unhealthy -- section 12 in as many words.
    """

    critical = "CRITICAL"
    important = "IMPORTANT"
    optional = "OPTIONAL"


class TradingSafety(StrEnum):
    """Section 40. Derived from authoritative components, never from a UI."""

    safe = "SAFE"
    degraded = "DEGRADED"
    blocked = "BLOCKED"
    unknown = "UNKNOWN"


class ErrorCategory(StrEnum):
    """Section 52. A small, closed vocabulary for operational failures."""

    configuration = "CONFIGURATION_ERROR"
    network = "NETWORK_ERROR"
    timeout = "TIMEOUT"
    authentication = "AUTHENTICATION_ERROR"
    validation = "VALIDATION_ERROR"
    broker = "BROKER_ERROR"
    database = "DATABASE_ERROR"
    queue = "QUEUE_ERROR"
    provider = "AI_PROVIDER_ERROR"
    unknown = "UNKNOWN_ERROR"


class IncidentType(StrEnum):
    """Section 43. Only conditions this platform can actually observe.

    These are `system_events.event_type` values, not bus event types. L37
    invents no catalogue entry: the realtime notification it sends is
    `SYSTEM_ALERT`, which has been catalogued since L07, and these words travel
    in its payload.
    """

    service_down = "SERVICE_DOWN"
    service_degraded = "SERVICE_DEGRADED"
    service_recovered = "SERVICE_RECOVERED"
    worker_stale = "WORKER_STALE"
    worker_recovered = "WORKER_RECOVERED"
    queue_backlog = "QUEUE_BACKLOG"
    queue_drained = "QUEUE_DRAINED"
    stale_market_data = "STALE_MARKET_DATA"
    market_data_recovered = "MARKET_DATA_RECOVERED"
    notification_failures = "NOTIFICATION_FAILURES"
    reconciliation_required = "RECONCILIATION_REQUIRED"


#: Incident types that mean a condition CLEARED. Section 45: a recovery is
#: announced, so the operational record reads "raised at 09:00, cleared at
#: 14:20" rather than leaving a gap somebody has to interpret -- the rule
#: `app/monitoring/alerts.py` recorded for L29.
RECOVERIES: frozenset[IncidentType] = frozenset(
    {
        IncidentType.service_recovered,
        IncidentType.worker_recovered,
        IncidentType.queue_drained,
        IncidentType.market_data_recovered,
    }
)

#: `system_events.level` accepts these five (L05's CHECK constraint).
LEVELS = ("debug", "info", "warning", "error", "critical")


@dataclass(frozen=True)
class ComponentHealth:
    """One component, as observed. Section 42.

    `last_checked` is when this observation was made, not when the component
    was last known good. `error_count` and `last_error` are what the owning
    system reported; nothing here is inferred from the absence of an error.

    There is no field on this object that could hold a URL, a host, a
    credential or a connection string. Section 58: a health response reveals
    minimal detail, and the cheapest way to guarantee that is to give the shape
    nowhere to put one.
    """

    name: str
    layer: Layer
    state: ComponentState
    criticality: Criticality
    detail: str
    checked_at: datetime
    latency_ms: float | None = None
    error_count: int = 0
    last_error: str | None = None
    #: Counts, states and ages. Never a payload, an address or a body.
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state is ComponentState.healthy

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "layer": str(self.layer),
            "status": str(self.state),
            "criticality": str(self.criticality),
            "detail": self.detail,
            "last_checked": self.checked_at.isoformat(),
            "latency_ms": self.latency_ms,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "facts": dict(self.facts),
        }


def aggregate(components: list[ComponentHealth]) -> ComponentState:
    """Section 39. Weighted by criticality, never averaged.

    The rules, in order, and each of them is a claim somebody has to be able to
    read off a dashboard without a footnote:

      * a CRITICAL component that is UNHEALTHY makes the platform UNHEALTHY;
      * a CRITICAL component that is anything but HEALTHY makes it DEGRADED;
      * an IMPORTANT component that is DEGRADED or UNHEALTHY makes it DEGRADED;
      * an OPTIONAL component never moves the overall state at all -- section
        12: an unconfigured integration must not make the platform unhealthy.

    An empty set is HEALTHY, because "nothing is being monitored" is not a
    failure of the thing being monitored. `monitoring.self` is what reports
    that, so the case is visible rather than silently reassuring.
    """
    if not components:
        return ComponentState.healthy
    critical = [c for c in components if c.criticality is Criticality.critical]
    if any(c.state is ComponentState.unhealthy for c in critical):
        return ComponentState.unhealthy
    if any(c.state is not ComponentState.healthy for c in critical):
        return ComponentState.degraded
    important = [c for c in components if c.criticality is Criticality.important]
    if any(c.state in (ComponentState.degraded, ComponentState.unhealthy) for c in important):
        return ComponentState.degraded
    return ComponentState.healthy


__all__ = [
    "LEVELS",
    "RECOVERIES",
    "STATE_RANK",
    "ComponentHealth",
    "ComponentState",
    "Criticality",
    "ErrorCategory",
    "IncidentType",
    "Layer",
    "TradingSafety",
    "aggregate",
    "from_health",
]
