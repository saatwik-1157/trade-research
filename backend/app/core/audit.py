"""Audit logging for security-relevant actions.

Writes to the `audit_logs` table created at L05, which nothing wrote to until
now. Every entry carries who, what, when, from where, and the request id, so
an action can be traced back to the log lines it produced.

**What is never recorded**, enforced by `_scrub` and by a test: passwords,
password hashes, session tokens, reset tokens, API keys, broker credentials.
The detail payload is filtered on the way in rather than trusted at the call
site, because the call site is where a mistake happens.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.models.ops import AuditLog

log = logging.getLogger("app.audit")

# Keys whose values never reach the audit table, at any depth.
FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "new_password",
        "old_password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "reset_token",
        "session",
        "session_token",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
    }
)

REDACTED = "[redacted]"


class AuditAction(StrEnum):
    login = "login"
    login_failed = "login_failed"
    logout = "logout"
    register = "register"
    password_changed = "password_changed"
    password_reset_requested = "password_reset_requested"
    password_reset_completed = "password_reset_completed"
    role_changed = "role_changed"
    permission_changed = "permission_changed"
    admin_action = "admin_action"
    rate_limited = "rate_limited"
    csrf_failed = "csrf_failed"
    # --- L36. One name per administrative act, rather than `admin_action`
    # for everything: section 33 asks the trail to say what happened, and
    # "admin_action" with the detail in a JSON blob is "bot changed" wearing a
    # different hat. `admin_action` is kept for acts that have no better name.
    user_activated = "user_activated"
    user_deactivated = "user_deactivated"
    sessions_revoked = "sessions_revoked"
    admin_denied = "admin_denied"


def _scrub(value: Any) -> Any:
    """Remove anything secret, at any depth, before it is stored."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if k.lower() in FORBIDDEN_KEYS else _scrub(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


async def record(
    db: AsyncSession,
    action: AuditAction | str,
    resource_type: str,
    *,
    actor_user_id: str | None = None,
    resource_id: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
    environment: str | None = None,
    details: dict | None = None,
) -> AuditLog:
    """One append-only row. Never updated, never deleted by this application.

    `environment` was added at L36 as a column rather than a detail key so
    section 35's filter is an indexed WHERE rather than a JSON scan. It is
    NULL for an action that is not about a trading environment -- a role
    change is not paper or live, and stamping it would say something untrue.
    """
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action=str(action),
        resource_type=resource_type,
        resource_id=resource_id,
        occurred_at=utcnow(),
        ip=ip,
        request_id=request_id,
        environment=environment,
        details=_scrub(details) if details else None,
    )
    db.add(entry)
    log.info(
        "audit",
        extra={
            "event": "audit",
            "action": str(action),
            "resource_type": resource_type,
            "actor_user_id": actor_user_id,
            "request_id": request_id,
            "environment": environment,
        },
    )
    return entry


async def record_admin(
    db: AsyncSession,
    action: AuditAction | str,
    resource_type: str,
    *,
    actor_user_id: str,
    reason: str,
    resource_id: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
    environment: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    extra: dict | None = None,
) -> AuditLog:
    """An administrative act. Sections 31, 33 and 34 of L36.

    The reason is REQUIRED by the signature rather than by a convention. A
    trail that records *what* changed and not *why* answers the easy half of
    the question somebody is asking months later, and there is no honest way
    to reconstruct the other half afterwards.

    `before` and `after` go through the same `_scrub` as everything else, so a
    state snapshot cannot carry a password, a token or a credential into the
    table even if the caller assembled one carelessly -- section 34 asks for
    redaction, and doing it on the way in rather than at the call site is what
    makes it hold.
    """
    details: dict[str, Any] = {"reason": reason}
    if before is not None:
        details["before"] = before
    if after is not None:
        details["after"] = after
    if extra:
        details.update(extra)
    return await record(
        db,
        action,
        resource_type,
        actor_user_id=actor_user_id,
        resource_id=resource_id,
        ip=ip,
        request_id=request_id,
        environment=environment,
        details=details,
    )
