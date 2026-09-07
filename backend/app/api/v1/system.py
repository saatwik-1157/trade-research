"""System: version, dependency health, and the trading-safety statement.

`/health`, `/health/live` and `/health/ready` stay mounted at the application
root. They are an infrastructure contract -- the Compose healthcheck, nginx
and any orchestrator probe them there -- and moving them under a version
prefix would break that for no gain. These routes read the *same* check
functions, so there is one implementation and two doors, not two answers.

The safety route is the one worth reading. It does not report a flag; it
reports every gate that is not built, so "live execution is not allowed" comes
with its reasons attached and cannot be turned into "allowed" by editing a
setting.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.v1.schemas import ApiVersionOut, TradingSafetyOut
from app.auth.deps import current_user, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.health import HealthStatus, overall_status, run_checks
from app.core.settings import LIVE_GATES, Settings

router = APIRouter(prefix="/system", tags=["system"])

API_VERSION = "v1"

_SIGNED_IN = Depends(current_user)
_SYSTEM_SETTINGS = Depends(require_permission(Permission.manage_system_settings))


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get(
    "/version",
    response_model=ApiVersionOut,
    summary="Service and API version",
    description="Public. Carries no URL, no credential and no dependency detail.",
)
async def version(request: Request) -> ApiVersionOut:
    settings = _settings(request)
    return ApiVersionOut(
        service=settings.app_name,
        version=settings.app_version,
        api_version=API_VERSION,
        environment=settings.environment.value,
        documented_at="/docs",
    )


@router.get(
    "/health",
    summary="Dependency health, with three states",
    description=(
        "healthy | degraded | unavailable per dependency, and an overall status "
        "derived from them. The system is never reported healthy while a "
        "critical dependency is down. Identical to /health/ready, which stays "
        "at the root for orchestrator probes."
    ),
)
async def health(request: Request, response: Response) -> dict[str, object]:
    results = await run_checks(request.app.state.checks)
    overall = overall_status(results)
    if overall is HealthStatus.unavailable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": str(overall),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "critical_unavailable": sorted(
            r.name for r in results.values() if r.critical and r.status is HealthStatus.unavailable
        ),
        "checks": {name: r.as_dict() for name, r in results.items()},
    }


@router.get(
    "/safety",
    response_model=TradingSafetyOut,
    summary="What the platform may execute, and why it may not execute more",
    description=(
        "`live_execution_blockers` lists every unbuilt gate by name. Live "
        "execution is unreachable by configuration alone: a setting can widen "
        "what the platform wants, only tested code widens what it can."
    ),
)
async def safety(request: Request, _: User = _SIGNED_IN) -> TradingSafetyOut:
    settings = _settings(request)
    return TradingSafetyOut(
        trading_mode=settings.trading_mode.value,
        live_trading=settings.live_trading,
        live_execution_allowed=settings.live_execution_allowed,
        live_execution_blockers=settings.live_execution_blockers(),
        gates=dict(LIVE_GATES),
    )


@router.get(
    "/events",
    summary="Event-bus transport in use by this process",
    description=(
        "Reports which bus is carrying events and never guesses. The in-process "
        "bus is labelled as such so a local delivery is not mistaken for a "
        "distributed one. The event catalogue and the WebSocket fan-out are L07."
    ),
)
async def events(request: Request, _: User = _SYSTEM_SETTINGS) -> dict[str, object]:
    bus = request.app.state.event_bus
    registry = request.app.state.workers
    return {
        "bus": bus.kind,
        "workers_registered": sorted(getattr(registry, "workers", {})),
        "stale_workers": sorted(registry.stale()) if getattr(registry, "workers", {}) else [],
    }
