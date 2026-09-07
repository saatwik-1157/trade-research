"""What the administration surface reads and does. Level 36.

**It orchestrates; it owns nothing.** Section 1's CRITICAL rule and section 10:
the admin panel must not become a second trading engine. So this module counts
rows, reads state that other systems recorded, and performs the two things that
are genuinely administration -- changing a user's access and revoking their
sessions. Every trading verb belongs to the service that owns it, and there is
no admin door to any of them:

    pause a bot          POST /v1/bots/{id}/disable        (BotManager, L22)
    disable a strategy   the strategy surface              (L12)
    activate a model     POST /v1/ai/models/.../promote    (registry, L28)
    reconcile a broker   GET  /v1/brokers/{id}/reconcile   (adapter, L10)
    change a risk limit  the risk surface                  (RiskService, L17)

Those routes already exist, already enforce their own permissions, and are
already the authoritative path. A second door in front of them would be a
second authorization surface to keep in step with the first, and the one that
drifts is always the one nobody is looking at. The admin dashboard links to
them; it does not wrap them.

**Nothing here deletes a historical record.** Section 13: deactivating a user
does not remove their trades, journal, analytics or strategies. It sets
`is_active = false` and revokes their sessions. If they hold open positions or
enabled bots, that is *reported* -- loudly -- and not acted on, because
liquidating somebody's position is a trading decision and this module does not
make those.

**Every write is audited with a reason.** Section 31 and section 33. Not "bot
changed": who, what, which resource, in which environment, why, and the before
and after state -- through `app.core.audit`, whose `_scrub` means a secret
cannot reach the row even if a caller put one in the reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession, Role, User, utcnow
from app.auth.permissions import (
    DANGEROUS,
    ROLE_PERMISSIONS,
    Permission,
    min_role_for_permission,
)
from app.auth.service import count_admins
from app.core.errors import Conflict, ValidationFailed
from app.models.accounts import BrokerAccount, MT5Connection, PaperAccount
from app.models.ai import ModelVersion
from app.models.ai_registry import ModelDeployment
from app.models.bots import Bot, BotRun
from app.models.execution import Order, Position, Trade
from app.models.ops import Notification, NotificationDelivery
from app.models.strategies import Strategy
from app.notifications.contract import Channel, DeliveryStatus

log = logging.getLogger("app.admin")

#: Section 30. A dangerous action carries a reason, and the reason has to say
#: something. Short enough not to be a chore, long enough that "x" does not
#: satisfy it -- the audit trail is read by somebody trying to understand a
#: decision months later.
MIN_REASON = 8
MAX_REASON = 500


class AdminError(ValidationFailed):
    """A refused administrative action. Never a silent no-op."""


@dataclass(frozen=True)
class Confirmation:
    """Section 30. An explicit action, a reason, and a phrase that is not 'yes'.

    The phrase is the subject's own identifier -- an email, a bot name -- so a
    misdirected request fails rather than succeeding against the wrong row. It
    is the one part of this that cannot be supplied by a client that did not
    look at what it was about to change.
    """

    reason: str
    phrase: str

    def check(self, *, expected: str, what: str) -> str:
        reason = (self.reason or "").strip()
        if len(reason) < MIN_REASON:
            raise AdminError(
                f"a reason of at least {MIN_REASON} characters is required. It is "
                "recorded in the audit trail and read by whoever asks why this "
                "happened."
            )
        if len(reason) > MAX_REASON:
            raise AdminError(f"the reason may be at most {MAX_REASON} characters")
        if (self.phrase or "").strip() != expected:
            raise AdminError(
                f"to confirm, repeat the {what} exactly. This is deliberately not a "
                "yes/no prompt: a confirmation you can click without reading is not a "
                "confirmation."
            )
        return reason


# ================================================================= dashboard


async def _count(db: AsyncSession, stmt: Select) -> int:
    return int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


async def dashboard(db: AsyncSession, settings: Any) -> dict[str, Any]:
    """Section 7. Counts and states, every one read from the owning system.

    Deliberately NOT the L37 monitoring dashboard: no latency, no queue depth,
    no per-dependency probe. This is administrative visibility -- how many of
    each thing exists and what state it is in -- and section 7 says so.

    Nothing here is estimated. A count is a count; a state is whatever the
    owning table recorded. An empty platform reports zeros, which is the honest
    answer and is why there is no seeded sample anywhere in this module.
    """
    users_total = await _count(db, select(User.id))
    users_active = await _count(db, select(User.id).where(User.is_active.is_(True)))
    by_role = {
        str(role): int(count)
        for role, count in (
            await db.execute(select(User.role, func.count(User.id)).group_by(User.role))
        ).all()
    }
    bots_total = await _count(db, select(Bot.id))
    bots_enabled = await _count(db, select(Bot.id).where(Bot.is_enabled.is_(True)))
    runs_live = await _count(
        db, select(BotRun.id).where(BotRun.status.in_(["starting", "running"]))
    )
    positions_open = await _count(
        db, select(Position.id).where(Position.status.in_(["open", "partially_closed"]))
    )
    orders_working = await _count(
        db,
        select(Order.id).where(
            Order.status.in_(["created", "submitted", "acknowledged", "partially_filled"])
        ),
    )
    # Section 17. An order the OMS could not resolve is reported as needing
    # reconciliation, never as failed.
    orders_unknown = await _count(db, select(Order.id).where(Order.status == "unknown"))

    deliveries_failed = await _count(
        db,
        select(NotificationDelivery.id).where(
            NotificationDelivery.status == str(DeliveryStatus.failed)
        ),
    )
    deliveries_queued = await _count(
        db,
        select(NotificationDelivery.id).where(
            NotificationDelivery.status.in_(
                [str(DeliveryStatus.pending), str(DeliveryStatus.retrying)]
            )
        ),
    )

    return {
        # Section 8 and section 41. First, and unmissable.
        "environment": {
            "environment": settings.environment.value,
            "trading_mode": settings.trading_mode.value,
            "live_trading": settings.live_trading,
            "live_execution_allowed": settings.live_execution_allowed,
            "live_execution_blockers": settings.live_execution_blockers(),
            "note": (
                "live execution is derived from gates that are built and verified, "
                "not from a setting. Nothing in this panel can flip one."
            ),
        },
        "users": {"total": users_total, "active": users_active, "by_role": by_role},
        "accounts": {
            "paper": await _count(db, select(PaperAccount.id)),
            "broker": await _count(db, select(BrokerAccount.id)),
            "mt5_connections": await _count(db, select(MT5Connection.id)),
        },
        "bots": {"total": bots_total, "enabled": bots_enabled, "runs_live": runs_live},
        "strategies": {
            "total": await _count(db, select(Strategy.id)),
            "active": await _count(db, select(Strategy.id).where(Strategy.is_active.is_(True))),
        },
        "models": {
            "versions": await _count(db, select(ModelVersion.id)),
            "deployments_active": await _count(
                db, select(ModelDeployment.id).where(ModelDeployment.status == "active")
            ),
        },
        "trading": {
            "trades": await _count(db, select(Trade.id)),
            "positions_open": positions_open,
            "orders_working": orders_working,
            "orders_needing_reconciliation": orders_unknown,
        },
        "notifications": {
            "total": await _count(db, select(Notification.id)),
            "deliveries_queued": deliveries_queued,
            "deliveries_failed": deliveries_failed,
        },
        "authority": (
            "administrative visibility. Every control lives on the surface that owns "
            "it -- bots on /v1/bots, models on /v1/ai, brokers on /v1/brokers, risk "
            "on /v1/risk -- and this panel has no door to any of them."
        ),
    }


# =============================================================== permissions


def rbac() -> dict[str, Any]:
    """Section 4. The role and permission tables, served rather than described.

    Read from `app.auth.permissions` rather than restated, so a permission that
    moves between roles moves here too. A UI that hard-coded this would show a
    grant the server had already withdrawn.
    """
    return {
        "roles": [
            {
                "role": str(role),
                "permissions": sorted(str(p) for p in permissions),
                "count": len(permissions),
            }
            for role, permissions in ROLE_PERMISSIONS.items()
        ],
        "permissions": [
            {
                "permission": str(p),
                "min_role": str(min_role_for_permission(p)),
                "dangerous": p in DANGEROUS,
            }
            for p in sorted(Permission, key=str)
        ],
        "note": (
            "a route asks for a permission, not a role, so a permission can move "
            "between roles without touching every route. Enforcement is server-side; "
            "the navigation mirror in the frontend hides links and authorizes nothing."
        ),
        "mfa": (
            "NOT IMPLEMENTED. This platform has no multi-factor authentication and "
            "no step-up re-authentication. Recorded as a gap rather than implied: "
            "an admin session is a password and a cookie."
        ),
    }


# ==================================================================== users


USER_SORTS = {
    "created_at": User.created_at,
    "email": User.email,
    "last_login_at": User.last_login_at,
}


def users_query(*, search: str | None, role: str | None, active: bool | None) -> Select:
    """Server-side search and filtering. Section 42.

    The search term is a bound parameter in a LIKE, never interpolated -- the
    same rule `app/api/pagination.py` applies to sort fields.
    """
    stmt = select(User)
    if search:
        term = f"%{search.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(User.email).like(term), User.id == search.strip()))
    if role:
        try:
            stmt = stmt.where(User.role == Role(role).value)
        except ValueError as exc:
            raise AdminError(f"role must be one of {', '.join(str(r) for r in Role)}") from exc
    if active is not None:
        stmt = stmt.where(User.is_active.is_(active))
    return stmt


async def user_detail(db: AsyncSession, user_id: str) -> dict[str, Any]:
    """One user, with what they own. Section 11.

    No password, no hash, no session token, no reset token. `UserOut` is built
    from named fields for the same reason -- a model built from the row would
    ship the hash the day somebody added a field.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise AdminError("no such user")
    paper = list(
        (await db.scalars(select(PaperAccount).where(PaperAccount.user_id == user.id))).all()
    )
    broker = list(
        (await db.scalars(select(BrokerAccount).where(BrokerAccount.user_id == user.id))).all()
    )
    bots = list((await db.scalars(select(Bot).where(Bot.user_id == user.id))).all())
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "accounts": {
            "paper": [{"id": a.id, "name": a.name, "status": a.status} for a in paper],
            "broker": [
                {
                    "id": a.id,
                    "name": a.name,
                    "broker": a.broker,
                    # What the broker reports, not what we hope.
                    "environment": a.account_mode,
                    "is_active": a.is_active,
                }
                for a in broker
            ],
        },
        "bots": [
            {"id": b.id, "name": b.name, "environment": b.mode, "enabled": b.is_enabled}
            for b in bots
        ],
        "sessions_active": await _count(
            db,
            select(AuthSession.id).where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > utcnow(),
            ),
        ),
        "note": (
            "no password, hash, session token or reset token is readable through "
            "this API, by anyone, at any role."
        ),
    }


async def sessions_for(db: AsyncSession, user_id: str) -> list[dict[str, Any]]:
    """Section 14. Sessions without tokens.

    `token_hash` is not returned and could not be replayed if it were -- the
    browser holds the token, the row holds its SHA-256. What is useful is when
    it was created, when it expires and what user agent opened it.
    """
    rows = (
        await db.scalars(
            select(AuthSession)
            .where(AuthSession.user_id == user_id)
            .order_by(AuthSession.created_at.desc())
            .limit(50)
        )
    ).all()
    return [
        {
            "id": row.id,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "active": row.is_valid,
            "user_agent": row.user_agent,
        }
        for row in rows
    ]


async def revoke_sessions(db: AsyncSession, user_id: str) -> int:
    """Force logout. Returns how many were actually live."""
    result = await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    # `execute()` is typed as returning `Result`, which has no `rowcount`;
    # an UPDATE always yields a `CursorResult`, which does. The cast documents
    # that rather than reaching past the type.
    return int(cast("CursorResult[Any]", result).rowcount or 0)


@dataclass(frozen=True)
class ActivationOutcome:
    user: User
    before: dict[str, Any]
    after: dict[str, Any]
    sessions_revoked: int
    open_positions: int
    enabled_bots: int

    @property
    def needs_attention(self) -> bool:
        return self.open_positions > 0 or self.enabled_bots > 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "user_id": self.user.id,
            "is_active": self.user.is_active,
            "before": self.before,
            "after": self.after,
            "sessions_revoked": self.sessions_revoked,
            "open_positions": self.open_positions,
            "enabled_bots": self.enabled_bots,
            "preserved": (
                "trades, journal entries, analytics, strategies and every historical "
                "record are untouched. Deactivation removes access, not history."
            ),
        }
        if self.needs_attention:
            # Section 13. Reported, never acted on. Closing somebody's position
            # is a trading decision and this module does not make one.
            out["warning"] = (
                f"this user still has {self.open_positions} open position(s) and "
                f"{self.enabled_bots} enabled bot(s). Deactivating them does NOT close "
                "a position or stop a bot -- nothing here liquidates anything. Use the "
                "bot and position surfaces, which own those decisions."
            )
        return out


async def check_set_active(db: AsyncSession, *, user_id: str, active: bool, actor: User) -> User:
    """Every reason this activation change would be refused, and nothing else.

    Split out of `set_active` at L39 so the router can run these guards BEFORE
    it spends a step-up grant. A grant is single-use, and burning one on an
    action that could never have succeeded -- deactivating yourself, or the
    last administrator -- means the operator re-authenticates, reads the real
    refusal, and has to re-authenticate again to do anything about it. Worse,
    the first thing they saw was "confirm your password", which reads as "this
    would work if you proved who you are" for an action that is forbidden
    whoever you are.

    `set_active` still calls it, so there is one implementation of the rules
    and no path that skips them: a caller who forgot this function gets the
    same refusal from the one that mutates.

    Returns the user, so the caller does not fetch the row twice.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise AdminError("no such user")
    if user.is_active == active:
        raise Conflict(f"this user is already {'active' if active else 'inactive'}")
    if not active:
        if user.id == actor.id:
            raise AdminError(
                "an administrator cannot deactivate their own account. Ask another "
                "administrator, so the action has a second person's name on it."
            )
        if user.role == Role.admin.value and await count_admins(db) <= 1:
            raise AdminError(
                "this is the last active administrator. Deactivating them would leave "
                "the platform with no way to administer it."
            )
    return user


async def set_active(
    db: AsyncSession, *, user_id: str, active: bool, actor: User
) -> ActivationOutcome:
    """Sections 12 and 13. Access changes; history does not.

    An administrator cannot deactivate themselves, and cannot deactivate the
    last active administrator -- the same protection `auth.service.set_role`
    already applies to demotion, for the same reason: a platform with no
    administrator cannot be administered back.
    """
    user = await check_set_active(db, user_id=user_id, active=active, actor=actor)

    before = {"is_active": user.is_active}
    user.is_active = active
    after = {"is_active": active}

    revoked = 0
    open_positions = 0
    enabled_bots = 0
    if not active:
        # A deactivated user whose session is still valid is a deactivated user
        # who is still signed in.
        revoked = await revoke_sessions(db, user.id)
        accounts = [
            *(
                await db.scalars(select(PaperAccount.id).where(PaperAccount.user_id == user.id))
            ).all(),
            *(
                await db.scalars(select(BrokerAccount.id).where(BrokerAccount.user_id == user.id))
            ).all(),
        ]
        if accounts:
            open_positions = await _count(
                db,
                select(Position.id).where(
                    Position.status.in_(["open", "partially_closed"]),
                    or_(
                        Position.paper_account_id.in_(accounts),
                        Position.broker_account_id.in_(accounts),
                    ),
                ),
            )
        enabled_bots = await _count(
            db, select(Bot.id).where(Bot.user_id == user.id, Bot.is_enabled.is_(True))
        )
    await db.flush()
    return ActivationOutcome(
        user=user,
        before=before,
        after=after,
        sessions_revoked=revoked,
        open_positions=open_positions,
        enabled_bots=enabled_bots,
    )


# ============================================================== integrations


def integrations(request_app: Any, settings: Any) -> dict[str, Any]:
    """Section 27, 57 and 58. What is configured, never how.

    Every entry is a state word. There is no field in this response that could
    hold a URL, a host, a password or a token, and a test asserts that a
    configured deployment's response contains none of its own secrets.
    """
    channels: list[dict[str, Any]] = []
    service = getattr(request_app.state, "notifications", None)
    if service is not None:
        channels = service.channels.describe()

    brokers = getattr(request_app.state, "brokers", None)
    order_managers = getattr(request_app.state, "order_managers", None)
    consumer = getattr(request_app.state, "notification_consumer", None)

    return {
        "notifications": {
            "enabled": bool(getattr(settings, "notifications_enabled", True)),
            "channels": channels,
            # Bound once rather than fetched twice: the double `getattr` read
            # the attribute in the guard and again in the call, so mypy could
            # not narrow it -- and in principle the two reads could differ.
            "consumer": (consumer.status() if consumer is not None else None),
        },
        "email": {
            # Whether, not what. No host, no address, no password.
            "configured": bool(getattr(settings, "smtp_host", "")),
            "enabled": bool(getattr(settings, "email_notifications_enabled", True)),
        },
        "discord": next(
            (c for c in channels if c.get("channel") == str(Channel.discord)),
            {"channel": str(Channel.discord), "state": "NOT_CONFIGURED"},
        ),
        "tradingview_webhook": {
            # L09's receiver refuses every alert when the secret is empty. That
            # is the fact worth reporting; the secret itself is not, and how
            # long it is, is nobody's business.
            "secret_configured": bool(getattr(settings, "tv_webhook_secret", "")),
            "restrict_to_tradingview_ips": bool(
                getattr(settings, "tv_webhook_restrict_ips", False)
            ),
        },
        "event_bus": {
            "kind": getattr(getattr(request_app.state, "event_bus", None), "kind", "unknown"),
            "enabled": bool(getattr(settings, "events_enabled", True)),
        },
        "brokers": {
            "adapters_registered": len(getattr(brokers, "adapters", {}) or {})
            if brokers is not None
            else 0,
            "order_managers_registered": len(getattr(order_managers, "managers", {}) or {})
            if order_managers is not None
            else 0,
            "note": (
                "an adapter is registered by an operator action, never as a side "
                "effect of the process booting. Zero is the honest state of a "
                "deployment that has not been pointed at a venue."
            ),
        },
        "never_returned": [
            "database URL or password",
            "Redis URL",
            "broker or MT5 credentials",
            "SMTP password",
            "Discord webhook URL or bot token",
            "TradingView webhook secret",
            "session or reset tokens",
        ],
    }


def configuration(settings: Any) -> dict[str, Any]:
    """Section 28. What is configuration, and which of it a browser may change.

    The answer for every row is the same: none of it. This platform's
    configuration is environment variables read at startup and a code-level
    gate table, and neither is editable through an API. That is deliberate --
    section 28 forbids arbitrary environment editing from a browser, and a
    field that accepted `DATABASE_URL` would be a remote code execution wearing
    a settings page.
    """
    from app.core.settings import LIVE_GATES

    return {
        "read_only": settings.public_summary(),
        "live_gates": {
            "gates": dict(LIVE_GATES),
            "all_built": all(LIVE_GATES.values()),
            "note": (
                "code, not configuration. Each entry flips only in the level that "
                "builds the mechanism and proves it, in a reviewed pass. There is no "
                "API that can set one, and this panel has no button that would."
            ),
        },
        "runtime_editable": [],
        "runtime_editable_note": (
            "nothing. Configuration is environment variables read at startup. A "
            "settings page that accepted a connection string or a credential would "
            "be a remote configuration channel into the process, and section 28 "
            "forbids exactly that."
        ),
        "feature_flags": {
            "implemented": False,
            "why_not": (
                "this platform has no feature-flag table and L36 did not add one. "
                "Its flags are LIVE_GATES, which are deliberately code rather than "
                "configuration, and settings, which are deliberately environment "
                "rather than database. A database-backed flag is a way to change "
                "behaviour without a deployment or a review, which is the property "
                "the gate table exists to prevent."
            ),
        },
    }


__all__ = [
    "MAX_REASON",
    "MIN_REASON",
    "USER_SORTS",
    "ActivationOutcome",
    "AdminError",
    "Confirmation",
    "configuration",
    "dashboard",
    "integrations",
    "rbac",
    "revoke_sessions",
    "sessions_for",
    "set_active",
    "user_detail",
    "users_query",
]
