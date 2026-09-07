"""/admin: user and role management. ADMIN only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.deps import current_user, get_db, require_role
from app.auth.models import Role, User
from app.auth.schemas import RoleUpdate, UserOut
from app.core import audit
from app.core.errors import request_id_of

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_role(Role.admin))]
)


@router.get("/users", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[User]:
    return await service.list_users(db)


@router.patch("/users/{user_id}/role", response_model=UserOut)
async def set_role(
    user_id: str,
    body: RoleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(current_user),
) -> User:
    before = await db.get(User, user_id)
    previous = before.role if before else None
    try:
        user = await service.set_role(db, user_id, body.role)
    except service.AuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    # A role change is a privilege change; it is always recorded, with who
    # made it and what it was before.
    await audit.record(
        db,
        audit.AuditAction.role_changed,
        "user",
        actor_user_id=actor.id,
        resource_id=user.id,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        details={"from": previous, "to": user.role, "subject_email": user.email},
    )
    await db.commit()
    return user
