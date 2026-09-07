"""Users and server-side sessions.

Sessions are rows, not signed tokens: a session can be revoked the moment a
password changes or an admin says so, and the browser only ever holds an
opaque random token whose SHA-256 is what the row stores. Timestamps are
naive UTC so SQLite (tests) and PostgreSQL agree byte for byte.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid4())


class Role(StrEnum):
    user = "user"  # read journal, analytics, dashboards
    trader = "trader"  # plus brokers, strategies, bots, orders, AI models
    admin = "admin"  # plus users, roles, platform controls


ROLE_RANK: dict[Role, int] = {Role.user: 0, Role.trader: 1, Role.admin: 2}


def role_at_least(have: Role, need: Role) -> bool:
    return ROLE_RANK[have] >= ROLE_RANK[need]


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Reference to roles.name (L05). The column keeps its L04 shape; only the
    # constraint was added, so no row had to change.
    role: Mapped[str] = mapped_column(
        String(16), ForeignKey("roles.name"), nullable=False, default=Role.user.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    # Observed, never assumed: written on a successful authentication only.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def role_enum(self) -> Role:
        return Role(self.role)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None and self.expires_at > utcnow()


class PasswordResetToken(Base):
    """A single-use password reset token.

    Only the SHA-256 of the token is stored, exactly as for sessions: a
    database read must not yield anything that can be replayed. The row
    records when it was used so a second attempt is refused rather than
    silently succeeding, and a reset invalidates every session the user has.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > utcnow()
