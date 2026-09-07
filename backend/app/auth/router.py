"""/auth: register, login, logout, me, password reset.

Layers on every state-changing request here, in order:

  rate limit  -> CSRF (middleware, sessions only) -> handler -> audit

The session token travels only in an HttpOnly cookie; a readable CSRF token
travels beside it and must be echoed in a header. Sign-in and registration
carry no session, so they are protected by rate limiting rather than by a
token the caller cannot yet have.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import reset as reset_service
from app.auth import service
from app.auth.csrf import clear_csrf_cookie, new_token, set_csrf_cookie
from app.auth.deps import current_user, get_db, settings_of
from app.auth.models import User
from app.auth.ratelimit import LOGIN_LIMIT, REGISTER_LIMIT, RESET_LIMIT, RateLimit
from app.auth.schemas import (
    Credentials,
    LoginIn,
    PasswordResetRequest,
    PasswordResetSubmit,
    UserOut,
)
from app.core import audit
from app.core.errors import request_id_of
from app.core.settings import Settings

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _enforce_rate_limit(
    request: Request, bucket: str, identifier: str, limit: RateLimit, db: AsyncSession
) -> None:
    """Two counters: one per address, one per identifier.

    An address spraying many accounts trips the first; many addresses
    targeting one account trip the second.
    """
    limiter = request.app.state.rate_limiter
    ip = _client_ip(request)
    for key in (f"ip:{ip}", f"id:{identifier.strip().lower()}"):
        decision = await limiter.hit(bucket, key, limit)
        if not decision.allowed:
            await audit.record(
                db,
                audit.AuditAction.rate_limited,
                "auth",
                ip=ip,
                request_id=request_id_of(request),
                details={"bucket": bucket, "retry_after": decision.retry_after},
            )
            await db.commit()
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                decision.detail,
                headers={"Retry-After": str(decision.retry_after)},
            )


def _issue_session_cookies(response: Response, settings: Settings, token: str) -> None:
    max_age = int(timedelta(hours=settings.session_ttl_hours).total_seconds())
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure_effective,
        path="/",
    )
    # Readable by design: the page echoes it in X-CSRF-Token.
    set_csrf_cookie(response, new_token(), settings.cookie_secure_effective, max_age)


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    clear_csrf_cookie(response)


def _raise(exc: Exception) -> None:
    raise HTTPException(getattr(exc, "status_code", 400), getattr(exc, "detail", str(exc)))


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: Credentials,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> User:
    settings = settings_of(request)
    await _enforce_rate_limit(request, "register", body.email, REGISTER_LIMIT, db)
    try:
        user = await service.register(
            db, body.email, body.password, allow=settings.allow_registration
        )
    except service.AuthError as exc:
        _raise(exc)
    token, _ = await service.create_session(
        db, user, timedelta(hours=settings.session_ttl_hours), request.headers.get("user-agent")
    )
    _issue_session_cookies(response, settings, token)
    await audit.record(
        db,
        audit.AuditAction.register,
        "user",
        actor_user_id=user.id,
        resource_id=user.id,
        ip=_client_ip(request),
        request_id=request_id_of(request),
        details={"email": user.email, "role": user.role},
    )
    await db.commit()
    return user


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> User:
    settings = settings_of(request)
    await _enforce_rate_limit(request, "login", body.email, LOGIN_LIMIT, db)
    try:
        user = await service.authenticate(db, body.email, body.password)
    except service.AuthError as exc:
        # Recorded without the password and without saying whether the account
        # exists; the response is identical either way.
        await audit.record(
            db,
            audit.AuditAction.login_failed,
            "user",
            ip=_client_ip(request),
            request_id=request_id_of(request),
            details={"email": body.email, "reason": type(exc).__name__},
        )
        await db.commit()
        _raise(exc)
    token, _ = await service.create_session(
        db, user, timedelta(hours=settings.session_ttl_hours), request.headers.get("user-agent")
    )
    _issue_session_cookies(response, settings, token)
    await audit.record(
        db,
        audit.AuditAction.login,
        "user",
        actor_user_id=user.id,
        resource_id=user.id,
        ip=_client_ip(request),
        request_id=request_id_of(request),
    )
    await db.commit()
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    settings = settings_of(request)
    raw = request.cookies.get(settings.session_cookie_name)
    user = await service.user_for_token(db, raw)
    await service.revoke_session(db, raw)
    _clear_session_cookies(response, settings)
    if user is not None:
        await audit.record(
            db,
            audit.AuditAction.logout,
            "user",
            actor_user_id=user.id,
            resource_id=user.id,
            ip=_client_ip(request),
            request_id=request_id_of(request),
        )
        await db.commit()


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> User:
    return user


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Always 202, whether or not the account exists.

    Answering differently would turn this endpoint into a user-enumeration
    oracle. The response never contains the token.
    """
    await _enforce_rate_limit(request, "password_reset", body.email, RESET_LIMIT, db)
    delivered = await reset_service.request_reset(
        db,
        body.email,
        request.app.state.reset_delivery,
        ip=_client_ip(request),
    )
    await audit.record(
        db,
        audit.AuditAction.password_reset_requested,
        "user",
        ip=_client_ip(request),
        request_id=request_id_of(request),
        details={"email": body.email, "delivered": delivered},
    )
    await db.commit()
    return {
        "status": "accepted",
        "detail": (
            "If an account exists for that address, a reset has been created. "
            "Delivery is not configured yet (level 34), so no message was sent."
            if not delivered
            else "If an account exists for that address, a reset link has been sent."
        ),
    }


@router.post("/password-reset/complete", response_model=UserOut)
async def complete_password_reset(
    body: PasswordResetSubmit,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> User:
    settings = settings_of(request)
    try:
        user = await reset_service.complete_reset(db, body.token, body.password)
    except reset_service.ResetError as exc:
        _raise(exc)
    # Every session was revoked by the reset; clear this client's cookies too.
    _clear_session_cookies(response, settings)
    await audit.record(
        db,
        audit.AuditAction.password_reset_completed,
        "user",
        actor_user_id=user.id,
        resource_id=user.id,
        ip=_client_ip(request),
        request_id=request_id_of(request),
    )
    await db.commit()
    return user
