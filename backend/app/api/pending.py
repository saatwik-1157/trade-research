"""Route groups whose implementations arrive in later levels.

Why these exist as routes rather than as nothing:

  * **Authorization is enforced and tested before the route can do anything.**
    A 501 behind a 401/403 gate is honest. A 200 with invented data is not,
    and a missing route teaches a caller nothing about what will guard it.
  * The path, the method and the gate are the parts a caller writes against.
    They are settled here so the level that builds the body does not also get
    to move the door.

Each entry names the level that builds it, and the answer says so. Nothing in
this module returns data, and nothing in it can be made to return data by
configuration — the handler's only behaviour is to raise.

`app.api.protected` keeps four of these mounted at their pre-v1 paths for one
release. See MIGRATION_STATUS.md L06 for the migration note.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, params

from app.auth.deps import current_user, require_permission, require_role
from app.auth.models import Role
from app.auth.permissions import Permission
from app.core.errors import NotImplementedYet


@dataclass(frozen=True)
class PendingGroup:
    """One unbuilt route group: where it will live, who may reach it, and when."""

    path: str
    tag: str
    level: int
    guard: params.Depends
    what: str
    methods: Sequence[str] = field(default=("GET",))


def _perm(permission: Permission) -> params.Depends:
    return Depends(require_permission(permission))


# Kept though nothing uses it today: it is the gate the notifications group
# held until L34 built it, and the next group to be declared signed-in-only
# should reuse this name rather than re-deriving it.
_SIGNED_IN = Depends(current_user)

# The full target surface. Read this table as the API contract for every
# level from 07 onward: the path and the gate are fixed, the body is not
# built. L34 removed its own entry -- `/notifications` is a real router now,
# and a 501 beside a working route would be a second answer for one path.
PENDING: tuple[PendingGroup, ...] = (
    PendingGroup(
        "/signals",
        "signals",
        16,
        _perm(Permission.view_markets),
        "signals emitted by strategies and webhooks",
    ),
    PendingGroup(
        "/journal/entries",
        "journal",
        31,
        _perm(Permission.view_journal),
        "notes, tags and review against a trade",
    ),
    PendingGroup(
        "/models/registry",
        "models",
        28,
        _perm(Permission.manage_ai_models),
        "registration, promotion, rollback",
    ),
)


def _handler(level: int, what: str):  # noqa: ANN202 - closure factory
    async def pending() -> None:
        raise NotImplementedYet(level, what)

    return pending


def build_router(groups: Sequence[PendingGroup] = PENDING, **route_kwargs: object) -> APIRouter:
    """One router carrying every unbuilt group in `groups`."""
    router = APIRouter()
    for group in groups:
        router.add_api_route(
            group.path,
            _handler(group.level, group.what),
            methods=list(group.methods),
            dependencies=[group.guard],
            name=f"pending_{group.tag}_{group.path.strip('/').replace('/', '_')}",
            tags=[group.tag],
            summary=f"{group.what} (built at L{group.level:02d})",
            status_code=501,
            **route_kwargs,  # type: ignore[arg-type]
        )
    return router


# Resources still gated coarsely by role rather than by a named permission,
# kept so `RESOURCE_MIN_ROLE` remains the single coarse table L04 built.
def role_guard(resource: str) -> params.Depends:
    from app.auth.permissions import min_role_for

    return Depends(require_role(min_role_for(resource)))


__all__ = ["PENDING", "PendingGroup", "build_router", "role_guard", "Role"]
