"""Authentication and account operations, independent of HTTP."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.models import AuthSession, Role, User, utcnow
from app.auth.passwords import burn_verification, hash_password, verify_password


class AuthError(Exception):
    status_code = 400
    detail = "authentication error"


class RegistrationClosed(AuthError):
    status_code = 403
    detail = "registration is closed"


class EmailTaken(AuthError):
    status_code = 409
    detail = "an account with that email already exists"


class InvalidCredentials(AuthError):
    status_code = 401
    detail = "invalid email or password"


class InactiveUser(AuthError):
    status_code = 403
    detail = "account is disabled"


class LastAdmin(AuthError):
    status_code = 400
    detail = "cannot remove the last admin"


def normalise_email(email: str) -> str:
    return email.strip().lower()


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def register(db: AsyncSession, email: str, password: str, *, allow: bool) -> User:
    if not allow:
        raise RegistrationClosed()
    email = normalise_email(email)
    exists = await db.scalar(select(User.id).where(User.email == email))
    if exists:
        raise EmailTaken()
    user = User(email=email, password_hash=hash_password(password), role=Role.user.value)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    email = normalise_email(email)
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        burn_verification(password)
        raise InvalidCredentials()
    if not verify_password(user.password_hash, password):
        raise InvalidCredentials()
    if not user.is_active:
        raise InactiveUser()
    # Observed on success only. A failed attempt must not move it, or the
    # column stops meaning "last time this account was actually used".
    user.last_login_at = utcnow()
    await db.commit()
    return user


async def create_session(
    db: AsyncSession, user: User, ttl: timedelta, user_agent: str | None = None
) -> tuple[str, AuthSession]:
    raw = secrets.token_urlsafe(32)
    row = AuthSession(
        user_id=user.id,
        token_hash=hash_token(raw),
        expires_at=utcnow() + ttl,
        user_agent=(user_agent or "")[:256] or None,
    )
    db.add(row)
    await db.commit()
    return raw, row


async def user_for_token(db: AsyncSession, raw: str | None) -> User | None:
    if not raw:
        return None
    row = await db.scalar(
        select(AuthSession)
        .options(selectinload(AuthSession.user))
        .where(AuthSession.token_hash == hash_token(raw))
    )
    if row is None or not row.is_valid or not row.user.is_active:
        return None
    return row.user


async def revoke_session(db: AsyncSession, raw: str | None) -> bool:
    if not raw:
        return False
    row = await db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(raw)))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    await db.commit()
    return True


async def list_users(db: AsyncSession) -> list[User]:
    return list((await db.scalars(select(User).order_by(User.created_at))).all())


async def count_admins(db: AsyncSession) -> int:
    n = await db.scalar(
        select(func.count()).select_from(User).where(User.role == Role.admin.value, User.is_active)
    )
    return int(n or 0)


async def set_role(db: AsyncSession, user_id: str, role: Role) -> User | None:
    user = await db.get(User, user_id)
    if user is None:
        return None
    if user.role == Role.admin.value and role is not Role.admin and await count_admins(db) <= 1:
        raise LastAdmin()
    user.role = role.value
    await db.commit()
    await db.refresh(user)
    return user


async def ensure_admin(db: AsyncSession, email: str, password: str) -> tuple[User, str]:
    """Create or promote the bootstrap admin. Returns (user, 'created'|'promoted'|'unchanged')."""
    email = normalise_email(email)
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, password_hash=hash_password(password), role=Role.admin.value)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user, "created"
    if user.role != Role.admin.value:
        user.role = Role.admin.value
        await db.commit()
        return user, "promoted"
    return user, "unchanged"
