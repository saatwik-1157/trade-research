"""Administration: visibility, user access, and the audit trail. Level 36.

**Every route here is gated on a named permission, server-side, and answers 403
without it.** Section 3 and section 37: the frontend's navigation mirror hides
links and authorizes nothing. A test signs in as a plain user and as a trader
and asserts each route refuses.

**No route here trades, and there is no door to one.** Section 1's CRITICAL
rule, section 10 and section 49. Pausing a bot is `POST /v1/bots/{id}/disable`
(BotManager, L22); promoting a model is `POST /v1/ai/.../promote` (the registry,
L28); reconciling a broker is `GET /v1/brokers/{id}/reconcile` (the adapter,
L10); a risk limit is the risk surface (L17). Those already exist, already
enforce their own permissions, and are already authoritative. A second door in
front of them would be a second authorization surface to keep in step, and the
one that drifts is the one nobody is watching. The dashboard reports; the
existing surfaces act. A test asserts this module imports none of them.

**Two routes write, and both are dangerous, so both cost something.** Section
30: a reason of real length and a confirmation phrase that is the subject's own
email -- not a yes/no prompt, because a confirmation you can click without
reading is not a confirmation. Both are audited with before and after state.

**Nothing here can enable live trading.** Section 9. `LIVE_GATES` is code, not
configuration; it is reported and there is no route that sets one.

**Nothing here deletes an audit row.** Section 32. There is no DELETE and no
UPDATE against `audit_logs` anywhere in this application, so an administrator
cannot erase their own trail through the API they administer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import service as admin_service
from app.api.pagination import Page, PageParams, SortSpec, page_params, paginate
from app.api.v1.schemas import AuditLogOut
from app.auth.deps import get_db, require_permission
from app.auth.models import Role, User
from app.auth.permissions import Permission
from app.auth.ratelimit import RateLimit
from app.core import audit
from app.core.errors import NotFound, ValidationFailed, request_id_of
from app.core.settings import Settings, get_settings
from app.models.ops import AuditLog
from app.security.enforce import announce, require_step_up
from app.security.events import SecurityEvent
from app.security.events import record as security_record
from app.security.stepup import StepUpScope

router = APIRouter(prefix="/admin", tags=["admin"])

_ADMIN = Depends(require_permission(Permission.access_administration))
#: Changing somebody's access is held above reading the panel. Section 5: an
#: administrator who needs to look at the dashboard does not need to be able to
#: lock somebody out.
_MANAGE_USERS = Depends(require_permission(Permission.manage_users))

#: Section 45. Privileged endpoints, especially the two that write. Tight
#: enough that scripted role churn is not free, generous enough that an
#: administrator working through a list is never stopped.
ADMIN_WRITE_LIMIT = RateLimit(limit=20, window_seconds=60)

AUDIT_SORTS = SortSpec(columns={"occurred_at": AuditLog.occurred_at}, default="occurred_at")
USER_SORTS = SortSpec(columns=admin_service.USER_SORTS, default="created_at")


async def _limit(request: Request, bucket: str, user: User) -> None:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:  # pragma: no cover - always built by create_app
        return
    from fastapi import HTTPException, status

    decision = await limiter.hit(bucket, f"user:{user.id}", ADMIN_WRITE_LIMIT)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            decision.detail,
            headers={"Retry-After": str(decision.retry_after)},
        )


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ================================================================= dashboard


@router.get(
    "/dashboard",
    summary="Operational visibility: how much of each thing, and in what state",
    description=(
        "Counts and states, every one read from the system that owns it. Not the "
        "L37 monitoring dashboard: no latency, no queue depth, no dependency "
        "probe. An empty platform reports zeros, which is the honest answer -- "
        "nothing here is seeded, sampled or estimated."
    ),
)
async def dashboard(
    request: Request, db: AsyncSession = Depends(get_db), _: User = _ADMIN
) -> dict[str, Any]:
    return await admin_service.dashboard(db, request.app.state.settings)


@router.get(
    "/permissions",
    summary="The role and permission tables, as the server holds them",
    description=(
        "Read from `app/auth/permissions.py` rather than restated, so a "
        "permission that moves between roles moves here too. A UI that "
        "hard-coded this would show a grant the server had already withdrawn."
    ),
)
async def permissions(_: User = _ADMIN) -> dict[str, Any]:
    return admin_service.rbac()


@router.get(
    "/integrations",
    summary="Which integrations are configured, and in what state",
    description=(
        "Status only. There is no field in this response that could hold a URL, "
        "a host, a password or a token, and `never_returned` lists what is "
        "deliberately absent."
    ),
)
async def integrations(request: Request, _: User = _ADMIN) -> dict[str, Any]:
    return admin_service.integrations(request.app, request.app.state.settings)


@router.get(
    "/configuration",
    summary="What is configuration, and which of it a browser may change",
    description=(
        "The answer for every row is: none of it. Configuration is environment "
        "variables read at startup and a code-level gate table. A settings page "
        "that accepted a connection string would be a remote configuration "
        "channel into the process."
    ),
)
async def configuration(request: Request, _: User = _ADMIN) -> dict[str, Any]:
    return admin_service.configuration(request.app.state.settings)


# ===================================================================== users


class UserListItem(BaseModel):
    id: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


@router.get(
    "/users/search",
    response_model=Page[UserListItem],
    summary="Users, searchable and paginated",
    description=(
        "Server-side search on email or id. Paginated with the same hard ceiling "
        "every collection in this API uses -- section 43: an admin table must "
        "not load a user list into a browser. Never returns a password or a hash."
    ),
)
async def search_users(
    search: str | None = Query(None, max_length=320, description="Email fragment or exact id."),
    role: str | None = Query(None, max_length=16),
    active: bool | None = Query(None),
    sort: str | None = Query(None, description="created_at | email | last_login_at"),
    order: str | None = Query(None, description="asc | desc"),
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    _: User = _ADMIN,
) -> Page[UserListItem]:
    stmt = admin_service.users_query(search=search, role=role, active=active)
    stmt = USER_SORTS.apply(stmt, sort, order)
    rows, page = await paginate(db, stmt, params)
    return Page(
        items=[
            UserListItem(
                id=u.id,
                email=u.email,
                role=u.role,
                is_active=u.is_active,
                created_at=u.created_at,
                last_login_at=u.last_login_at,
            )
            for u in rows
        ],
        page=page,
    )


@router.get(
    "/users/{user_id}",
    summary="One user, with the accounts and bots they own",
    description=(
        "Section 11. Identity, role, status, activity and associations. No "
        "password, no hash, no session token, no reset token -- at any role."
    ),
)
async def user_detail(
    user_id: str, db: AsyncSession = Depends(get_db), _: User = _ADMIN
) -> dict[str, Any]:
    try:
        return await admin_service.user_detail(db, user_id)
    except admin_service.AdminError as exc:
        raise NotFound(str(exc)) from exc


@router.get(
    "/users/{user_id}/sessions",
    summary="This user's sessions, without their tokens",
    description=(
        "The browser holds the token; the row holds its SHA-256. Neither is "
        "returned. What is useful is when a session opened, when it expires and "
        "what user agent opened it."
    ),
)
async def user_sessions(
    user_id: str, db: AsyncSession = Depends(get_db), _: User = _ADMIN
) -> dict[str, Any]:
    if await db.get(User, user_id) is None:
        raise NotFound("no such user")
    return {"sessions": await admin_service.sessions_for(db, user_id)}


class DangerousAction(BaseModel):
    """Section 30. A reason worth reading, and a phrase you had to look up."""

    reason: str = Field(
        ...,
        min_length=admin_service.MIN_REASON,
        max_length=admin_service.MAX_REASON,
        description="Recorded in the audit trail and read by whoever asks why.",
    )
    confirm: str = Field(
        ...,
        max_length=320,
        description="The subject's email, exactly. Not a yes/no prompt.",
    )


@router.post(
    "/users/{user_id}/deactivate",
    summary="Remove a user's access. Keeps every historical record.",
    description=(
        "Sets `is_active = false` and revokes their sessions. It does NOT delete "
        "trades, journal entries, analytics or strategies, and it does NOT close "
        "a position or stop a bot -- if they hold either, the response says so "
        "loudly and nothing is liquidated. Closing a position is a trading "
        "decision and this surface does not make one."
    ),
)
async def deactivate_user(
    request: Request,
    user_id: str,
    body: DangerousAction,
    db: AsyncSession = Depends(get_db),
    actor: User = _MANAGE_USERS,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return await _set_active(request, user_id, body, db, actor, active=False, settings=settings)


@router.post(
    "/users/{user_id}/activate",
    summary="Restore a user's access",
    description="Sets `is_active = true`. Existing sessions stay revoked; they sign in again.",
)
async def activate_user(
    request: Request,
    user_id: str,
    body: DangerousAction,
    db: AsyncSession = Depends(get_db),
    actor: User = _MANAGE_USERS,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return await _set_active(request, user_id, body, db, actor, active=True, settings=settings)


async def _set_active(
    request: Request,
    user_id: str,
    body: DangerousAction,
    db: AsyncSession,
    actor: User,
    *,
    active: bool,
    settings: Settings,
) -> dict[str, Any]:
    await _limit(request, "admin:users", actor)
    subject = await db.get(User, user_id)
    if subject is None:
        raise NotFound("no such user")
    reason = admin_service.Confirmation(body.reason, body.confirm).check(
        expected=subject.email, what="user's email address"
    )
    # L39. A third gate, after the permission and the confirmation phrase, and
    # after every deterministic refusal -- because the grant is spent by a
    # single use. Running it earlier would burn one on an action that could
    # never have succeeded (deactivating yourself, or the last administrator),
    # and would show "confirm your password" for something that is forbidden
    # whoever you are. `check_set_active` raises exactly what `set_active`
    # would, from the same code, so nothing is duplicated and nothing is
    # skipped.
    await admin_service.check_set_active(db, user_id=user_id, active=active, actor=actor)
    await require_step_up(
        request,
        scope=StepUpScope.admin_user_access,
        subject=user_id,
        actor_id=actor.id,
        settings=settings,
    )
    outcome = await admin_service.set_active(db, user_id=user_id, active=active, actor=actor)
    # Announced on the SYSTEM channel, so it carries the action and not the
    # person: "a user's access was removed" is what operators need to see on a
    # dashboard, and who it was is in the audit trail behind `audit:read`.
    await announce(
        request,
        security_record(
            SecurityEvent.dangerous_action,
            "a user's access was " + ("restored" if active else "removed"),
            user_id=actor.id,
            action="set_active",
            resource="user",
        ),
    )
    await audit.record_admin(
        db,
        audit.AuditAction.user_activated if active else audit.AuditAction.user_deactivated,
        "user",
        actor_user_id=actor.id,
        resource_id=subject.id,
        reason=reason,
        ip=_ip(request),
        request_id=request_id_of(request),
        before=outcome.before,
        after=outcome.after,
        extra={
            "subject_email": subject.email,
            "sessions_revoked": outcome.sessions_revoked,
            "open_positions": outcome.open_positions,
            "enabled_bots": outcome.enabled_bots,
        },
    )
    await db.commit()
    return outcome.as_dict()


@router.post(
    "/users/{user_id}/revoke-sessions",
    summary="Force this user to sign in again",
    description=(
        "Revokes every live session. Their account stays active and every "
        "record they own is untouched."
    ),
)
async def revoke_user_sessions(
    request: Request,
    user_id: str,
    body: DangerousAction,
    db: AsyncSession = Depends(get_db),
    actor: User = _MANAGE_USERS,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    await _limit(request, "admin:sessions", actor)
    subject = await db.get(User, user_id)
    if subject is None:
        raise NotFound("no such user")
    reason = admin_service.Confirmation(body.reason, body.confirm).check(
        expected=subject.email, what="user's email address"
    )
    await require_step_up(
        request,
        scope=StepUpScope.admin_session_revoke,
        subject=user_id,
        actor_id=actor.id,
        settings=settings,
    )
    revoked = await admin_service.revoke_sessions(db, user_id)
    # Two events. The operators are told an action was taken; the person it
    # happened TO is told on their own channel, because "you were signed out"
    # is only useful to somebody who did not do it.
    await announce(
        request,
        security_record(
            SecurityEvent.dangerous_action,
            "an account's sessions were revoked",
            user_id=actor.id,
            action="revoke_sessions",
            resource="user",
        ),
    )
    await announce(
        request,
        security_record(
            SecurityEvent.sessions_revoked,
            "every session on this account was signed out by an administrator",
            user_id=user_id,
        ),
    )
    await audit.record_admin(
        db,
        audit.AuditAction.sessions_revoked,
        "user",
        actor_user_id=actor.id,
        resource_id=subject.id,
        reason=reason,
        ip=_ip(request),
        request_id=request_id_of(request),
        extra={"subject_email": subject.email, "sessions_revoked": revoked},
    )
    await db.commit()
    return {
        "user_id": user_id,
        "sessions_revoked": revoked,
        "note": "the account is unchanged; they sign in again.",
    }


# ================================================================ audit trail


@router.get(
    "/audit-logs",
    response_model=Page[AuditLogOut],
    summary="The security and administration audit trail",
    description=(
        "Newest first, filterable by actor, action, resource, environment and "
        "date. `details` is scrubbed on write, not on read: passwords, hashes, "
        "session and reset tokens, API keys and broker credentials never enter "
        "the table at any depth, so nothing here needs filtering on the way out "
        "-- including the before/after snapshots L36 writes. There is no route "
        "that deletes or edits a row."
    ),
)
async def list_audit_logs(
    params: PageParams = Depends(page_params),
    action: str | None = Query(None, max_length=64),
    resource_type: str | None = Query(None, max_length=32),
    resource_id: str | None = Query(None, max_length=64),
    actor_user_id: str | None = Query(None, max_length=36),
    environment: str | None = Query(None, max_length=8),
    from_time: datetime | None = Query(None, description="occurred_at lower bound, UTC."),
    to_time: datetime | None = Query(None, description="occurred_at upper bound, UTC."),
    order: str | None = Query(None, description="asc | desc"),
    db: AsyncSession = Depends(get_db),
    _: User = _ADMIN,
) -> Page[AuditLogOut]:
    if from_time and to_time and from_time > to_time:
        raise ValidationFailed("from_time is after to_time")
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    if actor_user_id:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if environment:
        stmt = stmt.where(AuditLog.environment == environment)
    if from_time:
        stmt = stmt.where(AuditLog.occurred_at >= from_time)
    if to_time:
        stmt = stmt.where(AuditLog.occurred_at <= to_time)
    stmt = AUDIT_SORTS.apply(stmt, None, order)
    rows, page = await paginate(db, stmt, params)
    items = [AuditLogOut.model_validate(r, from_attributes=True) for r in rows]
    return Page[AuditLogOut](items=items, page=page)


@router.get(
    "/audit-logs/actions",
    summary="Which actions appear in the trail, and how often",
    description=(
        "For populating a filter without loading the table. Counts only; no "
        "detail, no actor, no resource."
    ),
)
async def audit_actions(db: AsyncSession = Depends(get_db), _: User = _ADMIN) -> dict[str, Any]:
    from sqlalchemy import func

    rows = (
        await db.execute(
            select(AuditLog.action, func.count(AuditLog.id))
            .group_by(AuditLog.action)
            .order_by(func.count(AuditLog.id).desc())
        )
    ).all()
    return {
        "actions": [{"action": str(a), "count": int(c)} for a, c in rows],
        "retention": (
            "none is applied automatically. This trail is append-only and nothing "
            "in the platform deletes from it on a timer. Security and "
            "administrative records are the ones a retention policy should keep "
            "longest, and choosing the period is an operator decision that has not "
            "been made."
        ),
        "immutability": (
            "there is no route that updates or deletes an audit row, so an "
            "administrator cannot erase their own trail through this API."
        ),
    }


@router.get(
    "/contract",
    summary="What this surface may do, and what it deliberately cannot",
    description=(
        "Served rather than documented elsewhere, so a client can check what it "
        "is looking at. The `delegates_to` table is the answer to 'where is the "
        "button to stop a bot' -- on the surface that owns bots."
    ),
)
async def contract(request: Request, _: User = _ADMIN) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "environment": {
            "trading_mode": settings.trading_mode.value,
            "live_trading": settings.live_trading,
            "live_execution_allowed": settings.live_execution_allowed,
        },
        "delegates_to": {
            "bots": "/v1/bots — start, pause, disable, limits, preflight (BotManager, L22)",
            "strategies": "/v1/strategies and /v1/strategy-builder (L12, L13)",
            "models": "/v1/ai/models/... — register, deploy, promote, rollback, retire (L28)",
            "brokers": "/v1/brokers — status, reconcile (BrokerAdapter, L10)",
            "risk": "/v1/risk — limits and the kill switch (RiskService, L17)",
            "notifications": "/v1/notifications — channels, preferences, Discord (L34, L35)",
        },
        "delegation_note": (
            "each of those enforces its own permission and is the authoritative "
            "path. A second door in front of one would be a second authorization "
            "surface to keep in step with the first."
        ),
        "writes_here": [
            "activate a user",
            "deactivate a user (access only; every record is preserved)",
            "revoke a user's sessions",
            "change a user's role (POST /admin/users/{id}/role, L04)",
        ],
        "dangerous_actions_require": ["a reason", "the subject's email as a confirmation phrase"],
        "cannot": [
            "place, modify or cancel an order",
            "open, close or modify a position",
            "change a risk limit or release a kill switch",
            "start, pause or stop a bot",
            "promote, roll back or deploy a model",
            "connect to or command a broker terminal",
            "enable live trading",
            "read or set any credential",
            "delete a trade, a journal entry or an audit row",
            "edit an environment variable",
        ],
        "roles": [str(r) for r in Role],
        "mfa": (
            "NOT IMPLEMENTED. There is no multi-factor authentication and no step-up "
            "re-authentication for administrative actions. Recorded as a gap rather "
            "than implied."
        ),
    }


__all__ = ["ADMIN_WRITE_LIMIT", "DangerousAction", "router"]
