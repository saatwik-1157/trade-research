"""Where step-up is actually required. One helper, called from four routes.

A re-authentication endpoint nobody checks is the security theatre the brief
names in as many words, so this module exists to make the check a single call
that a route cannot half-implement. It is deliberately not a FastAPI dependency:
the subject a grant is bound to is a path parameter or a body field, and a
dependency that could not see it would have to fall back to a scope-wide grant
-- which is the "one prompt unlocks everything" design `stepup.py` refuses.

**Four call sites, and the list is the definition of "dangerous" here:**

    admin: deactivate / activate a user      removes or restores access
    admin: revoke a user's sessions          signs somebody else out
    recovery: exit safe mode                 resumes trading after a latch
    paper: release the kill switch           lifts a deliberate trading halt

Each either removes somebody's access or lifts a control that stands between a
signal and an order. Read-only surfaces are deliberately absent: a confirmation
that fires on ordinary work is one people learn to click through, and a control
everybody clicks through protects nothing.

**Turning it off is possible and is reported.** `STEP_UP_REQUIRED=false` exists
for tests and for a first-run deployment with a single operator. It does not
fail silently: the posture endpoint lists `step_up_reauth` as a control that is
not in force, and every skipped check writes a log line saying so. A control
that can be disabled invisibly is one that is disabled.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status

from app.security.announce import SecurityAnnouncer
from app.security.events import (
    SecurityEvent,
    SecuritySeverity,
    fingerprint,
    record,
)
from app.security.stepup import StepUp, StepUpError, StepUpScope


def announcer(request: Request) -> SecurityAnnouncer:
    """The process's announcer, or a log-only one.

    A fallback rather than a failure: a test client built without the realtime
    wiring still gets working routes, and the only thing it loses is the
    published event -- the log line and the audit row are written either way.
    """
    held = getattr(request.app.state, "security_announcer", None)
    return held if held is not None else SecurityAnnouncer()


async def announce(request: Request, rec: Any) -> None:
    """Publish a security record. Never raises into the caller's request."""
    await announcer(request).announce(rec)


def session_key(request: Request, cookie_name: str, user_id: str) -> str:
    """A hashed handle for the caller's session. Never the token itself.

    Falls back to the user id when there is no cookie -- a token-authenticated
    caller still gets a grant bound to *something*, and binding it to the
    account is weaker than binding it to the session but is not nothing.
    """
    raw = request.cookies.get(cookie_name) or ""
    return fingerprint(raw) if raw else f"user:{user_id}"


async def require_step_up(
    request: Request,
    *,
    scope: StepUpScope,
    subject: str,
    actor_id: str,
    settings: Any,
) -> None:
    """Spend the caller's grant for this exact action, or refuse with 403.

    Raises `HTTPException(403)` rather than returning a boolean, because a
    caller that has to remember to check a return value is a caller that will
    eventually forget.
    """
    if not getattr(settings, "step_up_required", True):
        await announce(
            request,
            record(
                SecurityEvent.step_up_missing,
                f"{scope.value} proceeded without re-authentication because "
                "STEP_UP_REQUIRED is false",
                user_id=actor_id,
                scope=scope.value,
                outcome="skipped",
                severity=SecuritySeverity.warning,
            ),
        )
        return

    holder = getattr(request.app.state, "step_up", None)
    if holder is None:  # pragma: no cover - always built by create_app
        holder = StepUp()

    try:
        holder.consume(
            session=session_key(request, settings.session_cookie_name, actor_id),
            scope=scope,
            subject=subject,
            user_id=actor_id,
        )
    except StepUpError as exc:
        await announce(
            request,
            record(
                SecurityEvent.step_up_missing,
                f"{scope.value} was refused: no live re-authentication",
                user_id=actor_id,
                scope=scope.value,
                outcome="refused",
            ),
        )
        # 403 and not 401. The session is valid and stays valid; a 401 invites
        # the client to throw it away and sign in again, which is the wrong
        # remedy and loses whatever the operator had typed.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            str(exc),
            headers={"X-Step-Up-Scope": scope.value},
        ) from exc


__all__ = ["announce", "announcer", "require_step_up", "session_key"]
