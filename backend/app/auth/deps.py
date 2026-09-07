"""FastAPI dependencies: database session, current user, role requirement."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Role, User, role_at_least
from app.auth.permissions import Permission, has_permission, min_role_for_permission
from app.auth.service import user_for_token
from app.core.settings import Settings


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    settings = settings_of(request)
    user = await user_for_token(db, request.cookies.get(settings.session_cookie_name))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    return user


def require_role(need: Role) -> Callable[..., Awaitable[User]]:
    async def dep(user: User = Depends(current_user)) -> User:
        if not role_at_least(user.role_enum, need):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires the {need.value} role")
        return user

    return dep


def require_permission(need: Permission) -> Callable[..., Awaitable[User]]:
    """Gate a route on a named permission rather than a role.

    This is the finer of the two gates `app.auth.permissions` describes: a
    route says what it needs done, and the permission table decides which
    roles may do it. Moving `submit_orders` between roles then touches one
    table instead of every order route.

    The refusal names the permission, not the role that happens to hold it
    today, so the message stays true when the table changes.
    """

    async def dep(user: User = Depends(current_user)) -> User:
        if not has_permission(user.role_enum, need):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires the {need.value} permission "
                f"(held by {min_role_for_permission(need).value} and above)",
            )
        return user

    return dep
