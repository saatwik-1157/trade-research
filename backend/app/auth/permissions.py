"""Roles, named permissions, and which role holds which.

Two layers, deliberately:

  * **Roles** are what a user has. USER < TRADER < ADMIN.
  * **Permissions** are what a route needs. A route asks for a permission,
    not a role, so a permission can move between roles without touching
    every route.

The rule that shapes the table: **nothing dangerous is granted by default.**
A new account is USER and can read results. It cannot create a strategy, run
a bot, submit an order or change a risk setting until an admin grants TRADER.
Order submission and risk settings are TRADER-and-above, and user management
is ADMIN alone.

Enforcement is server-side. `RESOURCE_MIN_ROLE` is mirrored in
`frontend/src/lib/nav.ts` for navigation only; hiding a link is not
authorization and the API never trusts it.
"""

from __future__ import annotations

from enum import StrEnum

from app.auth.models import Role


class Permission(StrEnum):
    # Read
    view_markets = "view_markets"
    view_portfolio = "view_portfolio"
    view_analytics = "view_analytics"
    view_journal = "view_journal"
    # Research
    create_strategies = "create_strategies"
    run_backtests = "run_backtests"
    # Execution
    start_paper_bots = "start_paper_bots"
    manage_bots = "manage_bots"
    submit_orders = "submit_orders"
    manage_risk_settings = "manage_risk_settings"
    manage_brokers = "manage_brokers"
    manage_ai_models = "manage_ai_models"
    # L28 §24. Promotion, rollback and retirement change which model a running
    # strategy consults, so they are held ABOVE the permission that trains and
    # validates one. A trader may produce a candidate and register it; deciding
    # that it is the version a scope resolves to is an administrator's.
    promote_ai_models = "promote_ai_models"
    # Administration
    manage_users = "manage_users"
    manage_system_settings = "manage_system_settings"
    access_administration = "access_administration"


# A read-only observer of results. Nothing here can move money or start work.
USER_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.view_markets,
        Permission.view_portfolio,
        Permission.view_analytics,
        Permission.view_journal,
        Permission.run_backtests,
    }
)

# Everything a user has, plus the things that create or execute.
TRADER_PERMISSIONS: frozenset[Permission] = USER_PERMISSIONS | frozenset(
    {
        Permission.create_strategies,
        Permission.start_paper_bots,
        Permission.manage_bots,
        Permission.submit_orders,
        Permission.manage_risk_settings,
        Permission.manage_brokers,
        Permission.manage_ai_models,
    }
)

ADMIN_PERMISSIONS: frozenset[Permission] = TRADER_PERMISSIONS | frozenset(
    {
        Permission.manage_users,
        Permission.manage_system_settings,
        Permission.access_administration,
        Permission.promote_ai_models,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.user: USER_PERMISSIONS,
    Role.trader: TRADER_PERMISSIONS,
    Role.admin: ADMIN_PERMISSIONS,
}

# Permissions no role may hold implicitly and that no default grants. Kept as
# a named set so a test can assert a plain USER never acquires one.
DANGEROUS: frozenset[Permission] = frozenset(
    {
        Permission.submit_orders,
        Permission.manage_bots,
        Permission.manage_risk_settings,
        Permission.manage_brokers,
        Permission.manage_users,
        Permission.manage_system_settings,
        Permission.access_administration,
    }
)


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def min_role_for_permission(permission: Permission) -> Role:
    """The lowest role that holds this permission."""
    for role in (Role.user, Role.trader, Role.admin):
        if permission in ROLE_PERMISSIONS[role]:
            return role
    raise KeyError(f"no role holds {permission}")


# Resource-level table, kept from L04 and still the coarse gate used by the
# protected route stubs. Permissions are the finer one.
RESOURCE_MIN_ROLE: dict[str, Role] = {
    "brokers": Role.trader,
    "strategies": Role.trader,
    "bots": Role.trader,
    "orders": Role.trader,
    "ai_models": Role.trader,
    "admin": Role.admin,
    "journal": Role.user,
    "analytics": Role.user,
    "portfolio": Role.user,
}


def min_role_for(resource: str) -> Role:
    try:
        return RESOURCE_MIN_ROLE[resource]
    except KeyError as exc:
        raise KeyError(f"no permission entry for resource {resource!r}") from exc
