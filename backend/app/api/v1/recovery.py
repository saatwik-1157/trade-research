"""Recovery: what the startup sequence found, and the safe-mode latch.

Steps 32 and 34.

**Reading is signed in; acting is administrative.** `manage_system_settings`
gates every write, the existing double-submit CSRF covers them, and every one
takes a reason that is recorded -- the same shape L36 settled on for a
dangerous administrative action.

**Nothing here is a shortcut.** Step 32: recovery mechanisms must respect
security controls and must not expose arbitrary command execution. There is no
route that runs a command, restarts a process, resets a database, closes a
position or sends an order. The two writes re-run the read-only sequence and
open or close a latch.

**Reconciling is a read.** `POST /reconcile` calls the reconcilers L10, L19,
L21 and L22 already own; every one of them reports and none of them repairs.
Settling an unknown order is `POST /v1/orders/{id}/reconcile`, which is the
OMS's route and asks the venue -- it is not wrapped here, for the reason L36
gave for every trading control: a second door is a second authorization
surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core import audit
from app.core.errors import ValidationFailed, request_id_of
from app.models.ops import SystemEvent
from app.recovery import reconciliation
from app.recovery.contract import STARTUP_SEQUENCE, SafeModeReason
from app.recovery.manager import RecoveryManager
from app.security.enforce import announce, require_step_up
from app.security.events import SecurityEvent
from app.security.events import record as security_record
from app.security.stepup import StepUpScope

router = APIRouter(prefix="/recovery", tags=["recovery"])

_SIGNED_IN = Depends(current_user)
_OPERATE = Depends(require_permission(Permission.manage_system_settings))

MIN_REASON = 8


def _manager(request: Request) -> RecoveryManager:
    manager = getattr(request.app.state, "recovery", None)
    if manager is None:  # pragma: no cover - always built by create_app
        return RecoveryManager()
    return manager


class RecoveryAction(BaseModel):
    """A reason, recorded. The same bar L36 set for an administrative act."""

    reason: str = Field(
        ...,
        min_length=MIN_REASON,
        max_length=500,
        description="Recorded in the audit trail and in system_events.",
    )


@router.get(
    "/status",
    summary="The recovery state, the safe-mode latch and the last startup report",
    description=(
        "Readable by any signed-in user. Carries no credential and no "
        "connection string -- a recovery report is made of step names, states "
        "and counts."
    ),
)
async def status(request: Request, _: User = _SIGNED_IN) -> dict[str, Any]:
    return _manager(request).status(request.app.state.settings)


@router.get(
    "/sequence",
    summary="The startup sequence, in the order it runs",
    description=(
        "Served rather than documented elsewhere, so the order is checkable. "
        "Reconciliation sits between reconnecting and resuming, and that is the "
        "step the whole level exists to make unskippable."
    ),
)
async def sequence(_: User = _SIGNED_IN) -> dict[str, Any]:
    return {
        "sequence": [{"step": name, "what": what} for name, what in STARTUP_SEQUENCE],
        "rule": (
            "never DISCONNECTED -> EXECUTING. Between the link coming back and new "
            "orders being sent there is always a comparison of what we believe against "
            "what the venue holds."
        ),
        "live_trading": (
            "starting successfully never enables live trading. That is a configuration "
            "and a set of gates, and no route in this application sets either."
        ),
    }


@router.get(
    "/reconciliation",
    summary="What is currently unsettled, without settling it",
    description=(
        "Orders whose venue state was never established, positions the platform "
        "could not settle, and what each adapter says about its link. Nothing "
        "here repairs: an unexpected venue position may be a trade somebody made "
        "by hand, and an order we cannot account for is settled by asking the "
        "venue, never by re-sending."
    ),
)
async def reconciliation_status(
    request: Request, db: AsyncSession = Depends(get_db), _: User = _SIGNED_IN
) -> dict[str, Any]:
    report = await reconciliation.sweep(
        db,
        brokers=getattr(request.app.state, "brokers", None),
        session_factory=getattr(request.app.state, "session_factory", None),
    )
    return report.as_dict()


@router.get(
    "/events",
    summary="Recovery events, newest first",
    description=(
        "Read from `system_events` -- the same table L37 writes incidents to, "
        "with a `recovery.` component prefix, so a restart's findings and the "
        "outage that caused them read as one history."
    ),
)
async def events(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = _OPERATE,
) -> dict[str, Any]:
    rows = (
        await db.scalars(
            select(SystemEvent)
            .where(SystemEvent.component.like("recovery.%"))
            .order_by(SystemEvent.occurred_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "events": [
            {
                "id": row.id,
                "component": row.component,
                "event_type": row.event_type,
                "level": row.level,
                "occurred_at": row.occurred_at.isoformat(),
                "payload": row.payload or {},
            }
            for row in rows
        ]
    }


@router.post(
    "/reconcile",
    summary="Run the reconciliation sweep now",
    description=(
        "The same read-only sweep the startup sequence runs. It compares and "
        "reports; it closes nothing, opens nothing, cancels nothing and "
        "re-sends nothing."
    ),
)
async def reconcile(
    request: Request,
    body: RecoveryAction,
    db: AsyncSession = Depends(get_db),
    actor: User = _OPERATE,
) -> dict[str, Any]:
    manager = _manager(request)
    report = await reconciliation.sweep(
        db,
        brokers=getattr(request.app.state, "brokers", None),
        session_factory=getattr(request.app.state, "session_factory", None),
    )
    manager.last_reconciliation = report
    await manager.announce(getattr(request.app.state, "hub", None), report)
    await audit.record_admin(
        db,
        audit.AuditAction.admin_action,
        "recovery",
        actor_user_id=actor.id,
        resource_id="reconciliation",
        reason=body.reason,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        extra={"clean": report.clean, "needs_attention": len(report.attention)},
    )
    await db.commit()
    return report.as_dict()


@router.post(
    "/safe-mode/enter",
    summary="Engage safe mode deliberately",
    description=(
        "Blocks new orders, automated execution and bot recovery. Everything "
        "observational keeps running -- monitoring, reconciliation, position "
        "visibility, risk evaluation and these routes. The reason is recorded."
    ),
)
async def enter_safe_mode(
    request: Request,
    body: RecoveryAction,
    db: AsyncSession = Depends(get_db),
    actor: User = _OPERATE,
) -> dict[str, Any]:
    manager = _manager(request)
    manager.safe_mode.engage(SafeModeReason.operator, body.reason, actor_user_id=actor.id)
    await audit.record_admin(
        db,
        audit.AuditAction.admin_action,
        "recovery",
        actor_user_id=actor.id,
        resource_id="safe_mode",
        reason=body.reason,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        extra={"action": "enter_safe_mode"},
    )
    await db.commit()
    return manager.status(request.app.state.settings)


@router.post(
    "/safe-mode/exit",
    summary="Release safe mode, after re-running the sequence",
    description=(
        "Deliberately not a flag you can unset. The startup sequence is re-run "
        "first, and only the latches whose condition has actually cleared are "
        "released -- a release that skipped the re-check would let somebody "
        "resume trading into an account that is still unreconciled. The "
        "response says which latches are still holding, and why."
    ),
)
async def exit_safe_mode(
    request: Request,
    body: RecoveryAction,
    db: AsyncSession = Depends(get_db),
    actor: User = _OPERATE,
) -> dict[str, Any]:
    manager = _manager(request)
    if not manager.safe_mode.engaged:
        raise ValidationFailed("safe mode is not engaged")
    # L39. The password again before trading resumes. L38 chose to make
    # releasing safe mode expensive -- it re-runs the whole sequence -- and
    # noted in its own document that the administrative session doing the
    # releasing was still just a cookie. This is that gap. The subject is the
    # latch itself, because a platform has one.
    await require_step_up(
        request,
        scope=StepUpScope.safe_mode_exit,
        subject="safe_mode",
        actor_id=actor.id,
        settings=request.app.state.settings,
    )
    report = await manager.release(db, request.app, actor_user_id=actor.id, why=body.reason)
    await announce(
        request,
        security_record(
            SecurityEvent.dangerous_action,
            "safe mode release was attempted; "
            + ("it is still engaged" if report.safe_mode_engaged else "it is now clear"),
            user_id=actor.id,
            action="exit_safe_mode",
            resource="recovery",
            outcome="still_engaged" if report.safe_mode_engaged else "released",
        ),
    )
    await audit.record_admin(
        db,
        audit.AuditAction.admin_action,
        "recovery",
        actor_user_id=actor.id,
        resource_id="safe_mode",
        reason=body.reason,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        extra={
            "action": "exit_safe_mode",
            "still_engaged": report.safe_mode_engaged,
        },
    )
    await db.commit()
    return {
        **manager.status(request.app.state.settings),
        "report": report.as_dict(),
    }


@router.get(
    "/contract",
    summary="What recovery does, and what it deliberately cannot",
    description="Served so a client can check what it is looking at.",
)
async def contract(request: Request, _: User = _SIGNED_IN) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "reconcilers": {
            "broker": "app/brokers/reconcile.py (L10) — venue positions and orders",
            "oms": "OrderManager.reconcile (L19) — the only exit from `unknown`",
            "positions": "app/positions/reconciler.py (L21)",
            "bots": "app/bots/supervisor.py (L22) — plan_restart, which refuses more than it acts",
        },
        "reconcilers_note": (
            "each was built by the level that owns the state it compares, and each "
            "reports rather than repairs. L38 calls them in an order; it contains no "
            "comparison logic of its own."
        ),
        "safe_mode_reasons": [str(r) for r in SafeModeReason],
        "settling_an_unknown_order": (
            "POST /v1/orders/{id}/reconcile. It asks the venue and records the answer. "
            "It is the OMS's route and is not wrapped here."
        ),
        "environment": {
            "trading_mode": settings.trading_mode.value,
            "live_trading": settings.live_trading,
            "live_execution_allowed": settings.live_execution_allowed,
        },
        "cannot": [
            "submit, cancel or modify an order",
            "open, close or modify a position",
            "release a kill switch or change a risk limit",
            "start a bot, or recover one while safe mode is engaged",
            "enable live trading",
            "reset, drop or migrate a database",
            "delete a trade, a journal entry, an audit row or a system event",
            "run a command",
        ],
        "rules": [
            "an unknown order state is settled by asking the venue, never by retrying",
            "a broker reconnect does not by itself mean trading is safe",
            "position state is reconciled before autonomous execution resumes",
            "a notification failure never blocks a trade",
            "paper remains the default and live trading is never enabled by recovery",
        ],
    }


__all__ = ["router"]
