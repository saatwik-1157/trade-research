"""Password reset.

The token mechanics are complete and tested; **delivery is not built**, and
that is stated rather than faked. Sending the token to a user needs an email
or messaging channel, which is Level 34. Until then `request_reset` creates a
valid token and hands it to a `ResetDelivery` port whose only implementation
refuses, so the flow is exercised end to end without a token ever reaching
anyone who should not have it.

Security properties, each covered by a test:

  * The token is 256 bits of `secrets` entropy. Only its SHA-256 is stored,
    so a database read yields nothing replayable.
  * It expires, and it is single-use: the row records `used_at` and a second
    attempt is refused.
  * Requesting a reset for an unknown address succeeds identically to a known
    one. Anything else is a user-enumeration oracle.
  * **The token is never returned in an API response and never logged.**
    Returning it would be an authentication bypass wearing a helpful face.
  * Completing a reset revokes every session the user holds, so a stolen
    session does not survive the password change that was meant to stop it.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession, PasswordResetToken, User, utcnow
from app.auth.passwords import hash_password
from app.auth.service import normalise_email

log = logging.getLogger("app.auth.reset")

DEFAULT_TTL = timedelta(hours=1)


class ResetError(Exception):
    status_code = 400
    detail = "reset error"


class InvalidResetToken(ResetError):
    status_code = 400
    detail = "the reset link is invalid or has expired"


class DeliveryUnavailable(ResetError):
    """No channel is configured to deliver the token."""

    status_code = 503
    detail = "password reset delivery is not configured"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ResetDelivery(Protocol):
    """Sends a reset token to its owner. Level 34 provides a real one."""

    name: str

    async def send(self, email: str, token: str) -> None: ...


@dataclass
class UnconfiguredDelivery:
    """The only implementation today. It refuses rather than pretending.

    It does not log the token, print it, or return it. A reset therefore
    cannot currently be completed by a user, which is the honest state of a
    system with no messaging channel.
    """

    name: str = "unconfigured"

    async def send(self, email: str, token: str) -> None:
        raise DeliveryUnavailable(
            "no delivery channel is configured; password reset needs the "
            "notification engine (level 34)"
        )


async def request_reset(
    db: AsyncSession,
    email: str,
    delivery: ResetDelivery,
    ttl: timedelta = DEFAULT_TTL,
    ip: str | None = None,
) -> bool:
    """Create and dispatch a token. Returns whether delivery succeeded.

    The caller answers the same way regardless: whether an account exists is
    not something an unauthenticated request may learn.
    """
    user = await db.scalar(select(User).where(User.email == normalise_email(email)))
    if user is None or not user.is_active:
        return False

    raw = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=utcnow() + ttl,
            requested_ip=ip,
        )
    )
    await db.commit()

    try:
        await delivery.send(user.email, raw)
    except DeliveryUnavailable:
        # The token exists and is valid; nobody received it. Logged without
        # the token, because a logged token is a token in a log file.
        log.warning(
            "password reset token created but not delivered",
            extra={"event": "reset_undelivered", "delivery": delivery.name},
        )
        return False
    return True


async def complete_reset(db: AsyncSession, raw_token: str, new_password: str) -> User:
    """Consume a token, set the password, and revoke every session."""
    row = await db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(raw_token))
    )
    if row is None or not row.is_valid:
        raise InvalidResetToken(InvalidResetToken.detail)

    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise InvalidResetToken(InvalidResetToken.detail)

    user.password_hash = hash_password(new_password)
    row.used_at = utcnow()

    # A password change that leaves old sessions alive does not lock anyone
    # out, which is usually the entire point of resetting it.
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await db.commit()
    await db.refresh(user)
    return user
