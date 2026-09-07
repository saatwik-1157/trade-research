"""The observability service: one collection pass, one answer.

Sections 1, 39, 40, 66 and 67.

**One pass, read by everything.** The summary, the component list, the metrics
and the health endpoints all read the same snapshot. Four independent probes
would be four answers to "is the database up", and the one on the dashboard
would eventually disagree with the one the orchestrator restarted the container
over.

**Monitoring cannot control trading.** Section 72 and section 81. This package
imports no risk engine, no order manager, no sizing service, no broker adapter
class and no execution pipeline; a test parses every module to prove it. It
reads registries that were handed to it and calls their health and description
methods. A bug here can report the wrong colour. It cannot place an order.

**A failing collection is reported, not raised.** Section 66: monitoring
failure must not stop trading, and section 67: a monitor that cannot detect its
own failure is decoration. `collect` catches per-collector, turns the failure
into an UNKNOWN component carrying an error category, and counts it into
`monitoring.self`.

**Trading safety is derived here and nowhere else.** Section 40 is explicit
that a UI developer must not invent it. It is also purely observational --
nothing in the platform reads it to decide whether to trade, and it could not,
because the components it is derived from are the authorities and this is a
reading of them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.models.ops import SystemEvent
from app.observability import collectors, incidents
from app.observability.contract import (
    STATE_RANK,
    ComponentHealth,
    ComponentState,
    Criticality,
    Layer,
    TradingSafety,
    aggregate,
)
from app.observability.metrics import Registry
from app.observability.thresholds import DEFAULT_THRESHOLDS, Thresholds
from app.security.posture import component as security_posture

log = logging.getLogger("app.observability")

#: Components the trading-safety verdict is derived from. Section 40: the
#: authoritative ones, named rather than inferred from a layer.
SAFETY_COMPONENTS: tuple[str, ...] = (
    "database",
    "risk_engine",
    "broker",
    "oms",
    "positions",
    "market_data.freshness",
)


@dataclass
class Snapshot:
    """One collection pass."""

    at: datetime
    components: list[ComponentHealth]
    overall: ComponentState
    safety: TradingSafety
    safety_reasons: list[str]
    duration_ms: float
    incidents: list[incidents.Incident] = field(default_factory=list)

    def by_layer(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {str(layer): [] for layer in Layer}
        for component in self.components:
            out[str(component.layer)].append(component.as_dict())
        return {k: v for k, v in out.items() if v}

    def worst(self) -> list[dict[str, Any]]:
        ranked = sorted(
            (c for c in self.components if c.state is not ComponentState.healthy),
            key=lambda c: (-STATE_RANK[c.state], c.name),
        )
        return [c.as_dict() for c in ranked]


def trading_safety(
    components: list[ComponentHealth], settings: Any
) -> tuple[TradingSafety, list[str]]:
    """Section 40. Derived from the authoritative components, with its reasons.

    **This is a report, not a gate.** Nothing consults it before trading: the
    RiskEngine vetoes, the OMS reconciles, the adapter refuses. If this function
    returned SAFE while the risk engine was blocking, the platform would still
    not trade -- which is the property that makes it safe to compute a summary
    at all.

    UNKNOWN is a real verdict and is used. A component nobody could observe
    does not become SAFE by default.
    """
    named = {c.name: c for c in components if c.name in SAFETY_COMPONENTS}
    reasons: list[str] = []

    if not settings.live_execution_allowed:
        reasons.append(
            f"live execution is not allowed: {len(settings.live_execution_blockers())} "
            "gate(s) not built"
        )

    missing = [n for n in SAFETY_COMPONENTS if n not in named]
    unhealthy = [c.name for c in named.values() if c.state is ComponentState.unhealthy]
    unknown = [c.name for c in named.values() if c.state is ComponentState.unknown]
    degraded = [c.name for c in named.values() if c.state is ComponentState.degraded]

    for name in unhealthy:
        reasons.append(f"{name} is unhealthy")
    for name in unknown:
        reasons.append(f"{name} could not be observed")
    for name in degraded:
        reasons.append(f"{name} is degraded")
    for name in missing:
        reasons.append(f"{name} was not collected")

    if unhealthy:
        return TradingSafety.blocked, reasons
    if unknown or missing:
        # Not SAFE and not BLOCKED. We do not know, and saying so is the whole
        # point of the fourth state.
        return TradingSafety.unknown, reasons
    if degraded:
        return TradingSafety.degraded, reasons
    return TradingSafety.safe, reasons or ["every authoritative component is healthy"]


class ObservabilityService:
    """One per process. Holds the last snapshot, the tracker and the metrics."""

    def __init__(
        self,
        *,
        thresholds: Thresholds | None = None,
        registry: Registry | None = None,
    ) -> None:
        self.thresholds = thresholds or DEFAULT_THRESHOLDS
        self.metrics = registry or Registry()
        self.tracker = incidents.Tracker(self.thresholds)
        self.last: Snapshot | None = None
        self.collections = 0
        self.failures = 0

    # ================================================================ collect

    async def collect(self, db: AsyncSession, app: Any, *, publish: bool = True) -> Snapshot:
        """One pass. Never raises; a broken probe becomes an UNKNOWN component."""
        elapsed = collectors.timed()
        state = app.state
        bars = self.thresholds
        components: list[ComponentHealth] = []

        async def run(name: str, layer: Layer, criticality: Criticality, coro: Any) -> None:
            probe = collectors.timed()
            try:
                async with asyncio.timeout(bars.probe_timeout_seconds):
                    result = await coro
                components.extend(result if isinstance(result, list) else [result])
                self.metrics.increment(
                    "component_checks_total", labels={"group": name, "result": "ok"}
                )
            except Exception as exc:  # noqa: BLE001 - the pass survives one bad probe
                self.failures += 1
                components.append(collectors.failed(name, layer, criticality, exc))
                self.metrics.increment(
                    "component_checks_total", labels={"group": name, "result": "error"}
                )
                log.warning(
                    "a monitoring probe failed",
                    extra={"event": "probe_failed", "probe": name},
                )
            finally:
                self.metrics.observe("component_check_duration_ms", probe(), labels={"group": name})

        checks = getattr(state, "checks", {}) or {}
        await run(
            "infrastructure",
            Layer.infrastructure,
            Criticality.critical,
            collectors.infrastructure(checks, bars),
        )
        await run(
            "queues",
            Layer.workers,
            Criticality.important,
            collectors.queues(db, state, bars),
        )
        await run(
            "market_data",
            Layer.market_data,
            Criticality.important,
            collectors.market_data(db, state, bars),
        )
        await run(
            "trading",
            Layer.trading,
            Criticality.critical,
            collectors.trading(db, state, bars),
        )
        await run("ai", Layer.ai, Criticality.optional, collectors.ai(db, state))
        await run(
            "notifications",
            Layer.notifications,
            Criticality.optional,
            collectors.notifications(db, state, bars),
        )

        # Synchronous readers: no probe can fail, so no wrapper is needed.
        components.append(collectors.event_bus(state))
        components.append(collectors.realtime(state))
        components.extend(collectors.workers(state, bars))
        # L39. The security posture is one more component here rather than a
        # security dashboard beside the monitoring dashboard, so an operator
        # reads one page. It is derived from configuration and from counters
        # that already exist -- nothing about it is asserted, and a control this
        # deployment cannot determine reports UNKNOWN rather than HEALTHY.
        components.append(security_posture(state.settings, state))

        duration = elapsed()
        components.append(
            collectors.self_health(
                last_collection=self.last.at if self.last else None,
                failures=self.failures,
                duration_ms=duration,
                bars=bars,
            )
        )

        settings = state.settings
        overall = aggregate(components)
        safety, reasons = trading_safety(components, settings)

        raised: list[incidents.Incident] = []
        for component in components:
            incident = self.tracker.observe(component)
            if incident is not None:
                raised.append(incident)

        snapshot = Snapshot(
            at=utcnow(),
            components=components,
            overall=overall,
            safety=safety,
            safety_reasons=reasons,
            duration_ms=duration,
            incidents=raised,
        )
        self.last = snapshot
        self.collections += 1
        self._record_metrics(snapshot)

        for incident in raised:
            await incidents.record(db, incident)
            self.metrics.increment(
                "incidents_total", labels={"incident": str(incident.incident_type)}
            )
            if publish:
                await incidents.publish(getattr(state, "hub", None), incident)
        return snapshot

    def _record_metrics(self, snapshot: Snapshot) -> None:
        """Section 49. Bounded labels only: a component name is a closed set."""
        self.metrics.increment("monitoring_collections_total")
        self.metrics.observe("monitoring_collection_duration_ms", snapshot.duration_ms)
        for component in snapshot.components:
            self.metrics.gauge(
                "component_state",
                STATE_RANK[component.state],
                labels={"component": component.name},
                help="0 healthy, 1 not configured, 2 unknown, 3 degraded, 4 unhealthy",
            )
            if component.latency_ms is not None and component.name in ("database", "redis"):
                self.metrics.observe(
                    f"{component.name}_latency_ms"
                    if component.name == "redis"
                    else "db_query_duration_ms",
                    component.latency_ms,
                )
            facts = component.facts
            if component.name.startswith("worker:"):
                worker = component.name.split(":", 1)[1]
                self.metrics.gauge(
                    "worker_passes_total",
                    float(facts.get("passes") or 0),
                    labels={"worker": worker},
                )
                self.metrics.gauge(
                    "worker_failures_total",
                    float(component.error_count),
                    labels={"worker": worker},
                )
            if component.name.startswith("queue:"):
                self.metrics.gauge(
                    "queue_depth",
                    float(facts.get("depth") or 0),
                    labels={"queue": component.name.split(":", 1)[1]},
                )
            if component.name == "oms":
                self.metrics.gauge(
                    "orders_by_status",
                    float(facts.get("orders_working") or 0),
                    labels={"status": "working"},
                )
                self.metrics.gauge(
                    "orders_by_status",
                    float(facts.get("orders_unknown") or 0),
                    labels={"status": "unknown"},
                )
            if component.name == "realtime":
                self.metrics.gauge("realtime_connections", float(facts.get("connections") or 0))
                self.metrics.gauge(
                    "realtime_events_total", float(facts.get("events_published") or 0)
                )
            if component.name == "notifications.delivery":
                for status in ("delivered", "failed"):
                    self.metrics.gauge(
                        "notification_deliveries_total",
                        float(facts.get(status) or 0),
                        labels={"status": status},
                    )

    # ================================================================= serving

    def summary(self, settings: Any) -> dict[str, Any]:
        """Section 38 and 39. What the dashboard leads with."""
        snapshot = self.last
        if snapshot is None:
            return {
                "status": str(ComponentState.unknown),
                "trading_safety": str(TradingSafety.unknown),
                "trading_safety_reasons": ["no collection has run yet"],
                "environment": self._environment(settings),
                "collected_at": None,
                "components": 0,
                "note": (
                    "nothing has been collected. This is UNKNOWN rather than HEALTHY: "
                    "an unobserved platform is not a healthy one."
                ),
            }
        return {
            "status": str(snapshot.overall),
            "trading_safety": str(snapshot.safety),
            "trading_safety_reasons": snapshot.safety_reasons,
            "trading_safety_note": (
                "observational. Nothing in the platform reads this to decide whether "
                "to trade: the RiskEngine vetoes, the OMS reconciles and the adapter "
                "refuses. It is a reading of those, not a gate in front of them."
            ),
            "environment": self._environment(settings),
            "collected_at": snapshot.at.isoformat(),
            "duration_ms": snapshot.duration_ms,
            "components": len(snapshot.components),
            "not_healthy": snapshot.worst(),
            "aggregation": (
                "weighted, never averaged: a critical component unhealthy makes the "
                "platform unhealthy; anything else critical not healthy makes it "
                "degraded; an important component degraded makes it degraded; an "
                "optional one never moves it."
            ),
        }

    def _environment(self, settings: Any) -> dict[str, Any]:
        """Section 41. Always visible, always from the derived answer."""
        return {
            "environment": settings.environment.value,
            "trading_mode": settings.trading_mode.value,
            "live_trading": settings.live_trading,
            "live_execution_allowed": settings.live_execution_allowed,
            "live_execution_blockers": settings.live_execution_blockers(),
        }

    def components(self) -> dict[str, Any]:
        snapshot = self.last
        if snapshot is None:
            return {"collected_at": None, "layers": {}, "components": []}
        return {
            "collected_at": snapshot.at.isoformat(),
            "layers": snapshot.by_layer(),
            "components": [c.as_dict() for c in snapshot.components],
        }

    def component(self, name: str) -> dict[str, Any] | None:
        """Section 61. One component, with everything observed about it."""
        snapshot = self.last
        if snapshot is None:
            return None
        for component in snapshot.components:
            if component.name == name:
                return component.as_dict()
        return None

    async def events(
        self,
        db: AsyncSession,
        *,
        component: str | None = None,
        level: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Sections 62 and 63. The stored history, newest first.

        Read from `system_events`, which persists across a restart -- the
        in-process tracker does not, deliberately, so a restarted process
        establishes a baseline rather than announcing everything as new.
        """
        stmt = select(SystemEvent).order_by(SystemEvent.occurred_at.desc()).limit(limit)
        if component:
            stmt = stmt.where(SystemEvent.component == component)
        if level:
            stmt = stmt.where(SystemEvent.level == level)
        rows = (await db.scalars(stmt)).all()
        return [
            {
                "id": row.id,
                "component": row.component,
                "event_type": row.event_type,
                "level": row.level,
                "occurred_at": row.occurred_at.isoformat(),
                "correlation_id": row.correlation_id,
                "payload": row.payload or {},
            }
            for row in rows
        ]

    def uptime(self) -> dict[str, Any]:
        """Section 64. Never a percentage this platform cannot support.

        There is no uptime history: `system_events` records transitions, not
        samples, and a percentage computed from transitions since the process
        started would describe the process rather than the platform. Saying
        "insufficient history" is the honest answer and section 64 asks for
        exactly it.
        """
        return {
            "available": False,
            "why": (
                "insufficient history. Uptime needs a continuous sample series and this "
                "platform stores state TRANSITIONS, deliberately -- section 63 asks not "
                "to store every heartbeat forever. A percentage derived from transitions "
                "since this process started would describe the process, not the platform."
            ),
            "collections_this_process": self.collections,
            "probe_failures_this_process": self.failures,
        }


__all__ = ["SAFETY_COMPONENTS", "ObservabilityService", "Snapshot", "trading_safety"]
