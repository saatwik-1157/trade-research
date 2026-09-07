"""The automated execution engine's surface: status, start, stop.

**This is a control plane, not the execution path.** The pipeline runs in a
supervised background worker; these routes start it, stop it and report on it.
Closing a browser, dropping a connection or restarting the API's request
handlers stops nothing that is already running, which is the property brief
§19 asks for and the reason the worker exists at all.

**Nothing here executes a signal.** There is no route that takes a payload and
trades it. Signals arrive through the L09 webhook gateway or the strategy
engine, become rows, and the worker drains them — so the only way into the
execution path is a recorded signal, and there is exactly one consumer of it.

**Starting is an operator action.** The worker is registered at startup and
NOT started, for the same reason no broker adapter is registered at startup:
beginning to consume signals is a decision somebody makes, not a side effect
of the process booting.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.auth.deps import current_user, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import Conflict
from app.execution.worker import ExecutionWorker

router = APIRouter(prefix="/execution", tags=["execution"])

_VIEW = Depends(current_user)
#: Starting and stopping automated execution is a trading action, not a
#: settings change, so it sits behind the same permission order submission does.
_CONTROL = Depends(require_permission(Permission.submit_orders))


def _worker(request: Request) -> ExecutionWorker:
    worker: ExecutionWorker | None = getattr(request.app.state, "execution", None)
    if worker is None:  # pragma: no cover - registered in main.py at startup
        raise Conflict("the execution worker is not registered on this deployment")
    return worker


@router.get(
    "/status",
    summary="Pipeline counters, recent passes, and what the engine may not do",
)
async def status(request: Request, _: User = _VIEW) -> dict[str, Any]:
    worker = _worker(request)
    return {
        **worker.report(),
        # The heartbeat, so "running" is observed rather than assumed. A
        # worker that is running and stale is a worse state than a stopped
        # one, and both are visible here.
        "supervisor": worker.status.as_dict(),
    }


@router.post(
    "/start",
    summary="Begin draining recorded signals through the pipeline",
    description=(
        "The worker runs in the background from here on. It survives a closed "
        "browser and a dropped connection, because it is a supervised loop "
        "rather than anything attached to a request. Every signal it picks up "
        "still passes the AI seat, the Risk Engine, position sizing and the "
        "OMS — starting the worker grants no permission it did not have."
    ),
)
async def start(request: Request, user: User = _CONTROL) -> dict[str, Any]:
    worker = _worker(request)
    if worker.status.running:
        raise Conflict("the execution worker is already running")
    request.app.state.workers.start(worker)
    return {
        "running": True,
        "worker": worker.name,
        "note": (
            "Signals are drained in the background. Nothing about the gates changed: "
            "an order still requires a risk Approval, and no venue is reachable until "
            "an operator registers an adapter."
        ),
    }


@router.post(
    "/stop",
    summary="Stop draining signals. Orders already at a venue are unaffected",
    description=(
        "Stops the loop. It does NOT cancel, reconcile or retract anything "
        "already sent: an order at a venue is the venue's, and stopping a "
        "worker is not a way to unsend one. Use the OMS cancel and reconcile "
        "routes for that."
    ),
)
async def stop(request: Request, user: User = _CONTROL) -> dict[str, Any]:
    worker = _worker(request)
    worker.stop()
    return {
        "running": False,
        "worker": worker.name,
        "note": (
            "Orders already submitted are untouched. Stopping the consumer does not "
            "unsend anything; unresolved orders still need reconciling."
        ),
    }
