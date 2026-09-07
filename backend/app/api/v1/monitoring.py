"""Platform monitoring: is it healthy, what is failing, and is trading safe.

Sections 56, 57, 58 and 59.

**Two tiers, using the RBAC that already exists.** `/summary` is readable by
any signed-in user: an overall state, the environment, and the derived trading
safety with its reasons. Everything detailed -- per-component state, error
strings, incident history, metrics, thresholds -- needs
`manage_system_settings`, which is the permission this platform already gates
deployment configuration on. Section 59 suggests `monitoring.read`; adding a
permission means placing it in the role table, and `manage_system_settings`
already means exactly "may see how this deployment is put together".

**`/health`, `/health/live` and `/health/ready` stay where they are.** They are
an infrastructure contract that Compose and any orchestrator probe at the root,
and section 58 says a public health endpoint must reveal minimal detail. They
do: a status word and the mode. This router is the authorized detail.

**This is PLATFORM monitoring. L29's model monitoring stays at
`/v1/ai/monitoring`.** Two different questions -- is the deployment working,
and is the model still measuring what it measured -- and section 31 is explicit
that L37 consumes L29's results rather than recomputing them. `ai.models` is
one component here, read from L29's own tables.

**No route here changes anything.** There is no restart, no reconnect, no
retry and no acknowledge. Section 47: L37 detects, L38 recovers. The only
non-GET is a collection trigger, which runs the same read-only pass the worker
runs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import NotFound
from app.observability.contract import ComponentState, Criticality, IncidentType, Layer
from app.observability.metrics import FORBIDDEN_LABELS, MAX_SERIES
from app.observability.service import SAFETY_COMPONENTS, ObservabilityService

router = APIRouter(prefix="/monitoring", tags=["monitoring"])

_SIGNED_IN = Depends(current_user)
_DIAGNOSTICS = Depends(require_permission(Permission.manage_system_settings))


def _service(request: Request) -> ObservabilityService:
    service = getattr(request.app.state, "observability", None)
    if service is None:  # pragma: no cover - always built by create_app
        return ObservabilityService()
    return service


@router.get(
    "/summary",
    summary="Overall system state, the environment, and whether trading is safe",
    description=(
        "Readable by any signed-in user. Carries no host, no URL, no credential "
        "and no per-component error text -- section 58. `trading_safety` is "
        "derived from the authoritative components and never invented by a "
        "client, and it is observational: nothing in the platform reads it to "
        "decide whether to trade."
    ),
)
async def summary(request: Request, _: User = _SIGNED_IN) -> dict[str, Any]:
    return _service(request).summary(request.app.state.settings)


@router.get(
    "/components",
    summary="Every component, grouped by layer",
    description=(
        "Detailed diagnostics: requires `manage_system_settings`. Five states, "
        "not two -- NOT_CONFIGURED and UNKNOWN are different operational facts "
        "from UNHEALTHY, and collapsing them is how an operator spends an "
        "afternoon looking for a fault that does not exist."
    ),
)
async def components(request: Request, _: User = _DIAGNOSTICS) -> dict[str, Any]:
    return _service(request).components()


@router.get(
    "/components/{name}",
    summary="One component: state, latency, errors and what was observed",
    description="Section 61. Never a URL, a host or a credential.",
)
async def component(request: Request, name: str, _: User = _DIAGNOSTICS) -> dict[str, Any]:
    found = _service(request).component(name)
    if found is None:
        raise NotFound(
            f"no component named {name!r} in the last collection, or no collection has run"
        )
    return found


@router.get(
    "/events",
    summary="Recent incidents, newest first",
    description=(
        "Read from `system_events`, which survives a restart. A state change is "
        "only recorded after it has been observed consistently -- a service that "
        "fails for 100ms produces no row at all."
    ),
)
async def events(
    request: Request,
    component: str | None = Query(None, max_length=32),
    level: str | None = Query(None, max_length=8, description="info | warning | error | critical"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = _DIAGNOSTICS,
) -> dict[str, Any]:
    service = _service(request)
    return {
        "events": await service.events(db, component=component, level=level, limit=limit),
        "retention": (
            "none is applied automatically. `system_events` records state TRANSITIONS "
            "rather than heartbeats, so it grows with incidents rather than with time; "
            "a retention period is an operator decision that has not been made."
        ),
    }


@router.get(
    "/metrics",
    summary="Counters, gauges and histograms",
    description=(
        "In-process and bounded. Identifier labels are refused outright and a "
        "metric that reaches the series cap records into an overflow series "
        "rather than growing -- an unbounded label set is a memory leak that "
        "looks like observability."
    ),
)
async def metrics(request: Request, _: User = _DIAGNOSTICS) -> dict[str, Any]:
    service = _service(request)
    return {
        "metrics": service.metrics.snapshot(),
        "cardinality": {
            "max_series_per_metric": MAX_SERIES,
            "forbidden_labels": sorted(FORBIDDEN_LABELS),
            "note": (
                "identifiers belong in logs and traces. A label must come from a small "
                "closed set -- a component name, a status word, a channel."
            ),
        },
    }


@router.get(
    "/thresholds",
    summary="Every number the monitor compares against",
    description=(
        "Section 44: thresholds live in one place rather than scattered through "
        "collectors. None of them is a trading limit -- they decide when the "
        "platform says something, and the RiskEngine decides what may be traded."
    ),
)
async def thresholds(request: Request, _: User = _DIAGNOSTICS) -> dict[str, Any]:
    return _service(request).thresholds.as_dict()


@router.get(
    "/uptime",
    summary="Uptime, or an honest statement that there is not enough history",
    description=(
        "Section 64. This platform stores state transitions rather than a "
        "continuous sample series, so a percentage would describe the process "
        "rather than the platform. It says so instead of computing one."
    ),
)
async def uptime(request: Request, _: User = _DIAGNOSTICS) -> dict[str, Any]:
    return _service(request).uptime()


@router.get(
    "/contract",
    summary="What is monitored, how it is aggregated, and what monitoring cannot do",
    description=(
        "Served rather than documented elsewhere, so a client can check what it "
        "is rendering and a reviewer can check what it claims."
    ),
)
async def contract(request: Request, _: User = _SIGNED_IN) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "component_states": [str(s) for s in ComponentState],
        "state_meanings": {
            str(ComponentState.healthy): "observed working",
            str(ComponentState.degraded): "observed working, but not properly",
            str(ComponentState.unhealthy): "observed not working",
            str(ComponentState.unknown): (
                "not observed. A probe failed, or the thing it reads does not exist "
                "yet. Never treated as healthy."
            ),
            str(ComponentState.not_configured): (
                "deliberately absent. A platform with no SMTP server is correctly "
                "configured and does not send email."
            ),
        },
        "layers": [str(layer) for layer in Layer],
        "criticality": [str(c) for c in Criticality],
        "incident_types": [str(i) for i in IncidentType],
        "trading_safety_derived_from": list(SAFETY_COMPONENTS),
        "aggregation": (
            "weighted, never averaged. A critical component unhealthy makes the "
            "platform unhealthy; anything else critical not healthy makes it degraded; "
            "an important component degraded makes it degraded; an optional component "
            "never moves the overall state, because an unconfigured integration must "
            "not make a working platform report unhealthy."
        ),
        "environment": {
            "trading_mode": settings.trading_mode.value,
            "live_trading": settings.live_trading,
            "live_execution_allowed": settings.live_execution_allowed,
        },
        "alerting": (
            "an incident publishes one SYSTEM_ALERT onto the L07 bus. L34's "
            "notification service grades it, applies each recipient's preferences and "
            "routes it; L35 delivers it to Discord. This layer imports no notification "
            "service, no email provider and no Discord adapter."
        ),
        "does_not": [
            "place, modify or cancel an order",
            "open, close or modify a position",
            "change a risk limit or release a kill switch",
            "start, stop, pause or restart anything",
            "reconnect a broker or a provider",
            "enable live trading",
            "recompute drift, validation or any model metric (L29 owns those)",
            "claim an uptime percentage it has no history for",
        ],
        "recovery": (
            "L38's. This layer detects and reports RECONCILIATION_REQUIRED and "
            "RECOVERY_REQUIRED; it performs neither. A monitor that restarts the thing "
            "it watches cannot tell you it failed to restart it."
        ),
    }


@router.post(
    "/collect",
    summary="Run one collection pass now",
    description=(
        "The same read-only pass the worker runs on its interval. It probes, "
        "records any state transition and returns the snapshot. It changes no "
        "trading state, restarts nothing and reconnects nothing."
    ),
)
async def collect(
    request: Request, db: AsyncSession = Depends(get_db), _: User = _DIAGNOSTICS
) -> dict[str, Any]:
    service = _service(request)
    snapshot = await service.collect(db, request.app)
    await db.commit()
    return {
        **service.summary(request.app.state.settings),
        "incidents": [i.as_dict() for i in snapshot.incidents],
    }


__all__ = ["router"]
