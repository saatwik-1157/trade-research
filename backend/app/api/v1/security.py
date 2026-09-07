"""The security surface: re-authenticate, and read what is actually in force.

Steps 30, 44 and 45.

**Four routes, and none of them changes a security control.** There is no
endpoint that turns CSRF off, widens CORS, disables a header, clears the audit
trail or issues a token. That is not an omission -- a platform whose security
configuration is reachable over the API it protects has a single request
between an attacker and every control at once. The controls are configuration,
they are read at startup, and changing one is a deployment.

    POST /v1/security/step-up   re-authenticate for one dangerous action
    GET  /v1/security/step-up   what is pending for this session, as counts
    GET  /v1/security/posture   which controls are in force, and which are not
    GET  /v1/security/events    the security-relevant slice of the audit trail

**The step-up route is a password oracle if it is not counted**, so it is rate
limited on the session *and* limited by `StepUp`'s own per-scope attempt
counter, and a wrong password returns the same words as a missing grant.

**`/posture` names the gaps.** There is no MFA here, no managed secret store
and no behavioural intrusion detection, and the response says so every time it
is asked. A status page that lists only what exists reads as complete.

**`/events` reads the audit trail; it does not keep a second one.** L36's
`audit_logs` is append-only and already records who did what and why, and a
security event log beside it would be the duplicate system the brief forbids.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.passwords import verify_password
from app.auth.permissions import Permission
from app.auth.ratelimit import RateLimit
from app.core.errors import request_id_of
from app.core.settings import Settings, get_settings
from app.models.ops import AuditLog
from app.security import posture
from app.security.enforce import announce
from app.security.events import (
    SecurityEvent,
    SecurityRecord,
    SecuritySeverity,
    record,
)
from app.security.stepup import StepUp, StepUpError, StepUpScope

router = APIRouter(prefix="/security", tags=["security"])

_SIGNED_IN = Depends(current_user)
_ADMIN = Depends(require_permission(Permission.access_administration))
_AUDIT = Depends(require_permission(Permission.manage_system_settings))

#: Tighter than the login limiter (10/60) because a step-up is rarer than a
#: sign-in and because this route verifies a password for an ALREADY signed-in
#: session -- which is exactly the position an attacker with a stolen cookie is
#: in, trying to guess the password that stands between them and the dangerous
#: action.
STEP_UP_LIMIT = RateLimit(limit=5, window_seconds=300)


def _step_up(request: Request) -> StepUp:
    held = getattr(request.app.state, "step_up", None)
    if held is None:  # pragma: no cover - always built by create_app
        return StepUp()
    return held


def _session_key(request: Request, user: User, settings: Settings) -> str:
    """A stable handle for this session, never the session token itself.

    The cookie value is a bearer credential. Using it as a dict key would put
    it in a data structure that is iterated, logged on error and rendered in a
    traceback, so it is hashed on the way in -- `StepUp` never sees a token.
    """
    from app.security.events import fingerprint

    raw = request.cookies.get(settings.session_cookie_name) or ""
    return fingerprint(raw) if raw else f"user:{user.id}"


async def _limit(request: Request, user: User) -> None:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:  # pragma: no cover - always built by create_app
        return
    decision = await limiter.hit("security:step-up", f"user:{user.id}", STEP_UP_LIMIT)
    if not decision.allowed:
        await announce(
            request,
            record(
                SecurityEvent.rate_limited,
                "step-up confirmations were rate limited",
                user_id=user.id,
                scope="STEP_UP",
            ),
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            decision.detail,
            headers={
                "Retry-After": str(decision.retry_after),
                "X-Request-ID": request_id_of(request),
            },
        )


# ================================================================== step-up


class StepUpRequest(BaseModel):
    """The password again, the action, and what it is being taken against."""

    password: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description=(
            "The signed-in account's own password. Never stored, never logged, "
            "and never compared to anything but the Argon2 hash on the user row."
        ),
    )
    scope: StepUpScope = Field(..., description="Which class of dangerous action this confirms.")
    subject: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "What the action is against -- a user id, or the scope's own name "
            "for a platform-wide action. A grant taken out against one subject "
            "does not authorise the same action against another."
        ),
    )


class StepUpGranted(BaseModel):
    scope: StepUpScope
    subject: str
    expires_in_seconds: int
    single_use: bool = True
    note: str


@router.post(
    "/step-up",
    summary="Re-authenticate for one dangerous action",
    description=(
        "Confirms the signed-in account's password and issues a grant that is "
        "scoped to one action class, bound to one subject, valid for a few "
        "minutes and **spent by a single use**.\n\n"
        "This is not multi-factor authentication. It asks for the same password "
        "again, which raises the bar for a stolen session cookie and does "
        "nothing about a stolen password. `GET /v1/security/posture` says so "
        "in the response."
    ),
    status_code=status.HTTP_201_CREATED,
)
async def step_up(
    request: Request,
    body: StepUpRequest,
    user: User = _SIGNED_IN,
    settings: Settings = Depends(get_settings),
) -> StepUpGranted:
    await _limit(request, user)
    holder = _step_up(request)
    ok = verify_password(user.password_hash, body.password)
    try:
        grant = holder.grant(
            session=_session_key(request, user, settings),
            scope=body.scope,
            subject=body.subject.strip(),
            password_ok=ok,
            user_id=user.id,
        )
    except StepUpError as exc:
        await announce(
            request,
            record(
                SecurityEvent.step_up_failed,
                f"a confirmation for {body.scope.value} did not match",
                user_id=user.id,
                scope=body.scope.value,
                outcome="refused",
            ),
        )
        # 403 rather than 401: the session is valid and stays valid. A 401 would
        # invite a client to discard the session and sign in again, which is the
        # wrong remedy for a mistyped confirmation.
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    await announce(
        request,
        record(
            SecurityEvent.step_up_granted,
            f"re-authentication accepted for {grant.scope.value}",
            user_id=user.id,
            scope=grant.scope.value,
            outcome="granted",
        ),
    )
    return StepUpGranted(
        scope=grant.scope,
        subject=grant.subject,
        expires_in_seconds=int(holder.ttl_seconds),
        note=(
            "Repeat the action now. The grant covers this subject only and is "
            "consumed by one use, so a second action needs a second confirmation."
        ),
    )


@router.get(
    "/step-up",
    summary="What re-authentication is pending on this server",
    description=(
        "Counts only. It does not say which sessions hold grants, for which "
        "subjects, or which scopes are locked out -- that would turn a status "
        "endpoint into a map of who is about to do something dangerous."
    ),
)
async def step_up_status(request: Request, user: User = _SIGNED_IN) -> dict[str, Any]:
    return _step_up(request).stats()


# ================================================================== posture


@router.get(
    "/posture",
    summary="Which security controls are in force, and which are not",
    description=(
        "Read from configuration rather than asserted. A control this "
        "deployment cannot determine reports `null` rather than `true`, and the "
        "known gaps -- no MFA, no managed secret store, no behavioural "
        "detection -- are listed as gaps rather than omitted.\n\n"
        "There is no score. A number invites the question 'how do we get to "
        "ten', and the cheapest answers to that are controls that move a number."
    ),
)
async def security_posture(
    request: Request,
    user: User = _ADMIN,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return posture.scorecard(settings, request.app.state)


@router.get(
    "/headers",
    summary="The response headers this deployment sends, and why",
    description=(
        "Every response already carries them; this route exists so the reason "
        "for each one -- and the reason HSTS is production-only -- is available "
        "without reading the source."
    ),
)
async def security_headers(
    user: User = _ADMIN, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    from app.security.headers import describe

    return describe(settings)


# =================================================================== events


class SecurityEventOut(BaseModel):
    at: str
    action: str
    resource_type: str
    resource_id: str | None
    actor_user_id: str | None
    reason: str | None
    request_id: str | None


@router.get(
    "/events",
    summary="The security-relevant slice of the audit trail",
    description=(
        "Reads `audit_logs`, which has been append-only since L04. There is no "
        "second security log: two records of one action eventually disagree, "
        "and the one somebody reads is not the one that is right.\n\n"
        "Gated on `manage_system_settings`, because an audit trail names who "
        "did what and when."
    ),
)
async def security_events(
    user: User = _AUDIT,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> list[SecurityEventOut]:
    rows = (
        (await db.execute(select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    out: list[SecurityEventOut] = []
    for row in rows:
        # `details` is the scrubbed dict L36 writes; the reason lives in it.
        # Only the reason is lifted out -- `before` and `after` can name a
        # column somebody changed, and this route is a timeline rather than a
        # diff viewer. `GET /v1/admin/audit` is where the full row is read.
        details = row.details if isinstance(row.details, dict) else {}
        reason = details.get("reason")
        out.append(
            SecurityEventOut(
                at=row.occurred_at.isoformat(timespec="seconds") if row.occurred_at else "",
                action=str(row.action),
                resource_type=str(row.resource_type),
                resource_id=row.resource_id,
                actor_user_id=row.actor_user_id,
                reason=str(reason) if isinstance(reason, str) else None,
                request_id=row.request_id,
            )
        )
    return out


__all__ = ["STEP_UP_LIMIT", "SecurityRecord", "SecuritySeverity", "router"]
