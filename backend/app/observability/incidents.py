"""Turning a state change into an incident, once, with hysteresis.

Sections 43, 45, 46 and 62.

**A state change is not an incident.** Section 45: a service that fails for
100ms must not produce DOWN/UP/DOWN/UP. `Tracker` requires a condition to be
observed `consecutive_failures` times in a row before it is raised and
`consecutive_successes` times before it is cleared. On a fifteen-second
interval that is a real fault reported within thirty seconds and a blip
reported not at all.

**A recovery is announced.** Section 43 lists `SERVICE_RECOVERED` beside
`SERVICE_DOWN`, and section 45 asks for recovery confirmation. That is the rule
`app/monitoring/alerts.py` recorded for L29: a condition that simply stops
appearing must not vanish from the history, or the operational record has a gap
somebody has to interpret rather than reading "raised at 09:00, cleared at
14:20".

**Two destinations, both existing.** An incident becomes:

  * a `system_events` row -- the table L05 created for exactly this
    ("operational events: connects, disconnects, reconciliations, halts") and
    which nothing had ever written to. Section 76 asks not to create tables
    when the infrastructure already stores these records; this is that table.
  * a `SYSTEM_ALERT` on L07's bus, which L34 turns into a notification through
    the preference engine and L35 delivers to Discord.

Section 46 is explicit: monitoring must not send Discord or email directly.
This module imports neither. It publishes one catalogued event and stops
thinking about it -- and it invents no event type, because `SYSTEM_ALERT` has
been in the catalogue since L07 and the incident vocabulary travels in its
payload and in `system_events.event_type`.

**Nothing here recovers anything.** Section 47: L37 detects, L38 recovers. An
incident is a row and a message. There is no reconnect, no restart and no
retry in this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.models.ops import SystemEvent
from app.observability.contract import (
    RECOVERIES,
    ComponentHealth,
    ComponentState,
    Criticality,
    IncidentType,
)
from app.observability.thresholds import Thresholds

log = logging.getLogger("app.observability.incidents")

#: Which incident a component raises when it goes bad, by component-name
#: prefix. Section 43: only conditions this platform can actually observe.
_RAISED: tuple[tuple[str, IncidentType, IncidentType], ...] = (
    ("worker:", IncidentType.worker_stale, IncidentType.worker_recovered),
    ("queue:", IncidentType.queue_backlog, IncidentType.queue_drained),
    (
        "market_data.freshness",
        IncidentType.stale_market_data,
        IncidentType.market_data_recovered,
    ),
    (
        "notifications.",
        IncidentType.notification_failures,
        IncidentType.service_recovered,
    ),
    ("oms", IncidentType.reconciliation_required, IncidentType.service_recovered),
    ("positions", IncidentType.reconciliation_required, IncidentType.service_recovered),
)

#: `system_events.level`, from the component's state and how much it matters.
_LEVEL: dict[tuple[ComponentState, Criticality], str] = {
    (ComponentState.unhealthy, Criticality.critical): "critical",
    (ComponentState.unhealthy, Criticality.important): "error",
    (ComponentState.unhealthy, Criticality.optional): "warning",
    (ComponentState.degraded, Criticality.critical): "error",
    (ComponentState.degraded, Criticality.important): "warning",
    (ComponentState.degraded, Criticality.optional): "warning",
    (ComponentState.unknown, Criticality.critical): "warning",
    (ComponentState.unknown, Criticality.important): "warning",
    (ComponentState.unknown, Criticality.optional): "info",
}

#: The health word `app.notifications.catalogue._health_severity` reads off a
#: SYSTEM_ALERT payload. Mapped here rather than passing the component state
#: through, so the two vocabularies meet in exactly one place.
_HEALTH_WORD: dict[ComponentState, str] = {
    ComponentState.healthy: "healthy",
    ComponentState.degraded: "degraded",
    ComponentState.unhealthy: "unavailable",
    ComponentState.unknown: "warning",
    ComponentState.not_configured: "healthy",
}

#: States that count as "the condition is present". `NOT_CONFIGURED` is
#: deliberately absent: a channel nobody set up is not an incident, and
#: raising one every fifteen seconds for an optional integration is how a
#: dashboard becomes wallpaper.
BAD: frozenset[ComponentState] = frozenset(
    {ComponentState.degraded, ComponentState.unhealthy, ComponentState.unknown}
)


@dataclass(frozen=True)
class Incident:
    """One state transition worth recording."""

    component: str
    incident_type: IncidentType
    level: str
    previous: ComponentState
    current: ComponentState
    detail: str
    at: datetime
    criticality: Criticality
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def is_recovery(self) -> bool:
        return self.incident_type in RECOVERIES

    def title(self) -> str:
        if self.is_recovery:
            return f"{self.component} recovered"
        return f"{self.component} is {self.current}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "event_type": str(self.incident_type),
            "level": self.level,
            "from": str(self.previous),
            "to": str(self.current),
            "detail": self.detail,
            "at": self.at.isoformat(),
            "criticality": str(self.criticality),
            "recovery": self.is_recovery,
            "facts": dict(self.facts),
        }


def _types_for(component: str) -> tuple[IncidentType, IncidentType]:
    for prefix, bad, good in _RAISED:
        if component.startswith(prefix):
            return bad, good
    return IncidentType.service_down, IncidentType.service_recovered


@dataclass
class _Streak:
    state: ComponentState
    bad_in_a_row: int = 0
    good_in_a_row: int = 0


class Tracker:
    """Per-component hysteresis. Section 45.

    In-process and deliberately not persisted: a restarted process has not
    observed anything yet, and the first pass after a restart should establish
    a baseline rather than announce every component as new. The durable record
    is `system_events`, which survives the restart and is what an operator
    reads.
    """

    def __init__(self, bars: Thresholds | None = None) -> None:
        self.bars = bars or Thresholds()
        self._streaks: dict[str, _Streak] = {}

    def observe(
        self, component: ComponentHealth, *, now: datetime | None = None
    ) -> Incident | None:
        """Return an incident if this observation crossed a threshold."""
        now = now or utcnow()
        name = component.name
        state = component.state
        streak = self._streaks.get(name)
        if streak is None:
            # First sighting. A baseline, never an incident: a process that
            # started while a component was already down should say so on the
            # dashboard, not page somebody as though it had just happened.
            self._streaks[name] = _Streak(state=state)
            return None

        bad_now = state in BAD
        if bad_now:
            streak.bad_in_a_row += 1
            streak.good_in_a_row = 0
        else:
            streak.good_in_a_row += 1
            streak.bad_in_a_row = 0

        previous = streak.state
        was_bad = previous in BAD
        raised, recovered = _types_for(name)

        if bad_now and not was_bad and streak.bad_in_a_row >= self.bars.consecutive_failures:
            streak.state = state
            return Incident(
                component=name,
                incident_type=raised,
                level=_LEVEL.get((state, component.criticality), "warning"),
                previous=previous,
                current=state,
                detail=component.detail,
                at=now,
                criticality=component.criticality,
                facts=dict(component.facts),
            )
        if not bad_now and was_bad and streak.good_in_a_row >= self.bars.consecutive_successes:
            streak.state = state
            return Incident(
                component=name,
                incident_type=recovered,
                level="info",
                previous=previous,
                current=state,
                detail=component.detail,
                at=now,
                criticality=component.criticality,
                facts=dict(component.facts),
            )
        # A worsening within "bad" is still a transition worth recording --
        # DEGRADED becoming UNHEALTHY is the escalation an operator reacts to,
        # and suppressing it hides the state change, which is the same argument
        # L29 and L34 both make about severity.
        if bad_now and was_bad and state is not previous and streak.bad_in_a_row >= 1:
            streak.state = state
            return Incident(
                component=name,
                incident_type=(
                    IncidentType.service_degraded if state is ComponentState.degraded else raised
                ),
                level=_LEVEL.get((state, component.criticality), "warning"),
                previous=previous,
                current=state,
                detail=component.detail,
                at=now,
                criticality=component.criticality,
                facts=dict(component.facts),
            )
        if not bad_now and not was_bad:
            # Healthy and was healthy: keep the recorded state in step. It is
            # deliberately NOT updated while `was_bad` is still true -- doing so
            # would erase the "it was down" that the recovery threshold is
            # counting towards, and no recovery would ever be announced.
            streak.state = state
        return None

    def snapshot(self) -> dict[str, str]:
        return {name: str(s.state) for name, s in self._streaks.items()}


# ============================================================== persistence


def _scrub(facts: dict[str, Any]) -> dict[str, Any]:
    """Section 53 and section 7. Nothing sensitive reaches a stored payload.

    Collectors already build facts out of counts and state words, so this is a
    second line rather than the only one: a key whose name looks like a secret
    is dropped, at any depth, on the way in.
    """
    forbidden = ("password", "token", "secret", "credential", "url", "webhook", "key", "dsn")
    out: dict[str, Any] = {}
    for key, value in facts.items():
        if any(word in key.lower() for word in forbidden):
            continue
        out[key] = _scrub(value) if isinstance(value, dict) else value
    return out


async def record(
    db: AsyncSession, incident: Incident, *, correlation_id: str | None = None
) -> SystemEvent:
    """Write the incident to `system_events`. Section 27's audit trail.

    The table L05 created and nothing wrote to. Its columns are exactly what
    section 53 asks an incident to carry -- component, event type, level, when,
    a correlation id and a structured payload -- so no new table was needed.
    """
    row = SystemEvent(
        component=incident.component[:32],
        event_type=str(incident.incident_type)[:32],
        level=incident.level,
        occurred_at=incident.at,
        correlation_id=correlation_id,
        payload={
            "from": str(incident.previous),
            "to": str(incident.current),
            "detail": incident.detail[:500],
            "criticality": str(incident.criticality),
            "recovery": incident.is_recovery,
            **_scrub(incident.facts),
        },
    )
    db.add(row)
    log.info(
        "incident",
        extra={
            "event": "observability_incident",
            "component": incident.component,
            "incident": str(incident.incident_type),
            "level": incident.level,
            "from": str(incident.previous),
            "to": str(incident.current),
            "correlation_id": correlation_id,
        },
    )
    return row


async def publish(hub: Any | None, incident: Incident) -> bool:
    """One `SYSTEM_ALERT` onto L07's bus. Section 46.

    L34's notification consumer picks it up, grades it from the `health` word,
    applies each recipient's preferences and routes it to in-app, email and --
    from L35 -- Discord. Nothing here knows any of that: this module imports no
    notification service, no email provider and no Discord adapter, which is
    what section 46 asks for in as many words.

    A publish failure is logged and swallowed. Section 66: monitoring failure
    must not stop anything, and that includes not failing the collection pass
    because a socket was unavailable.
    """
    if hub is None:
        return False
    from app.core.events import Event

    try:
        await hub.publish(
            Event(
                type="SYSTEM_ALERT",
                payload={
                    "component": incident.component,
                    "check": str(incident.incident_type),
                    "title": incident.title(),
                    "reason": incident.detail[:300],
                    # The word L34 grades severity from.
                    "health": _HEALTH_WORD[incident.current],
                    "from": _HEALTH_WORD[incident.previous],
                    "environment": None,
                },
                source="observability",
                channel="system",
            )
        )
    except Exception:  # noqa: BLE001 - an incident is recorded even if it is not announced
        log.warning(
            "an incident could not be published",
            extra={
                "event": "incident_publish_failed",
                "component": incident.component,
                "incident": str(incident.incident_type),
            },
        )
        return False
    return True


__all__ = ["BAD", "Incident", "Tracker", "publish", "record"]
