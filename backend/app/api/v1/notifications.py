"""The notification surface: what you were told, and what you want to be told.

Sections 33, 46 and 47.

**Every read is scoped in the query, not filtered after it.** `WHERE user_id =
:me` is on the statement, so a caller cannot page past their own rows and a
notification belonging to somebody else cannot be counted, listed or marked
read. That is the rule `app/realtime/channels.py`, the portfolio router and the
reviews router already follow, and the reason is the same: authorization
checked after a fetch is authorization that can be forgotten.

**A notification for another user answers 404, not 403.** Distinguishing them
would say whether the id exists, which is a membership oracle. The same
sentence appears in `channels.py` and in `reviews.py`.

**No route here can create a notification.** There is no POST that takes a
title and a body. Section 58: production must never display a fabricated
trading alert, and the cheapest way to guarantee that is to give the API no way
to write one -- notifications exist only as a consequence of a domain event
that the platform itself published.

**Nothing here is a preference an operator can use to disable a safety
system.** Section 17. `PATCH /preferences` writes rows in
`notification_preferences` and nothing else reads that table except the
notification service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, SortSpec, page_params, paginate
from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.auth.ratelimit import RateLimit
from app.core import audit
from app.core.errors import NotFound, ValidationFailed, request_id_of
from app.models.ops import Notification, NotificationDelivery
from app.notifications import catalogue as notif_catalogue
from app.notifications import preferences as prefs
from app.notifications.contract import (
    ENVIRONMENTS,
    UNSUPPRESSIBLE,
    Category,
    Channel,
    DeliveryStatus,
    Severity,
)
from app.notifications.service import (
    BACKOFF,
    MAX_ATTEMPTS,
    MAX_RECIPIENTS,
    NotificationService,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])

_SIGNED_IN = Depends(current_user)
#: L35 section 33 and section 50. Configuring an integration and testing one
#: are administrative acts; reading its status is not. `manage_system_settings`
#: is the permission this platform already gates deployment configuration on.
_SYSTEM_SETTINGS = Depends(require_permission(Permission.manage_system_settings))

#: Section 47. Generous enough that a notification centre polling every few
#: seconds is unaffected, tight enough that the two endpoints which write are
#: not a free way to generate database work.
WRITE_LIMIT = RateLimit(limit=60, window_seconds=60)
BULK_LIMIT = RateLimit(limit=10, window_seconds=60)

SORTS = SortSpec(
    columns={"created_at": Notification.created_at, "severity": Notification.severity},
    default="created_at",
)


def _service(request: Request) -> NotificationService:
    """The process's one service, or a bare one if the app did not build it.

    A fallback rather than a failure: a test client constructed without the
    notification wiring still gets working read endpoints, and the only thing
    the bare service lacks is channel adapters -- which no read route uses.
    """
    service = getattr(request.app.state, "notifications", None)
    if service is None:
        return NotificationService()
    return service


async def _limit(request: Request, bucket: str, limit: RateLimit, user: User) -> None:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:  # pragma: no cover - always built by create_app
        return
    decision = await limiter.hit(bucket, f"user:{user.id}", limit)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            decision.detail,
            headers={
                "Retry-After": str(decision.retry_after),
                "X-Request-ID": request_id_of(request),
            },
        )


# =================================================================== contract


@router.get(
    "/contract",
    summary="Which events become notifications, and what governs delivery",
    description=(
        "The routing table, the severity vocabulary, the channel states and the "
        "retry policy. `producing_now` is false for an event type whose emitter "
        "has not been built, so a category that is quiet can be told apart from "
        "one that cannot fire yet -- the same distinction "
        "`/v1/realtime/catalogue` draws for events."
    ),
)
async def contract(request: Request, _: User = _SIGNED_IN) -> dict[str, Any]:
    service = _service(request)
    return {
        "severities": [str(s) for s in Severity],
        "categories": [str(c) for c in Category],
        "channels": [str(c) for c in Channel],
        "delivery_statuses": [str(s) for s in DeliveryStatus],
        "environments": list(ENVIRONMENTS),
        "always_delivered_in_app": sorted(str(s) for s in UNSUPPRESSIBLE),
        "events": notif_catalogue.as_dict(),
        "not_notified": {str(t): why for t, why in notif_catalogue.NOT_NOTIFIED.items()},
        "channel_status": service.channels.describe(),
        "retries": {
            "max_attempts": MAX_ATTEMPTS,
            "backoff_seconds": list(BACKOFF),
            "note": (
                "bounded. A permanent error -- a refused authentication, an unknown "
                "recipient -- is not retried at all: retrying a configuration mistake "
                "forever is how a queue stops draining."
            ),
        },
        "fanout_cap": MAX_RECIPIENTS,
        "does_not": [
            "place, modify or cancel an order",
            "change a risk limit, a kill switch or a position",
            "start, pause or stop a bot",
            "enable live trading",
            "calculate a risk, exposure or drawdown figure of its own",
            "create a notification from an API call rather than a domain event",
        ],
        "environment_note": (
            "a trading notification always carries an environment. Where the event did "
            "not state one it is stored as 'unknown' rather than defaulting to paper: a "
            "live trade shown as paper is the mistake that costs money."
        ),
    }


# ====================================================================== reads


@router.get(
    "",
    response_model=Page[dict[str, Any]],
    summary="Your notifications, newest first",
    description=(
        "Scoped to the signed-in user in the query. Filterable by category, "
        "severity, environment and read state, and paginated with the same hard "
        "ceiling every collection in this API uses."
    ),
)
async def list_notifications(
    category: str | None = Query(None, max_length=16),
    severity: str | None = Query(None, max_length=8),
    environment: str | None = Query(None, max_length=8),
    unread: bool | None = Query(None, description="true for unread only."),
    event_type: str | None = Query(None, max_length=32),
    from_time: datetime | None = Query(None, description="created_at lower bound, UTC."),
    to_time: datetime | None = Query(None, description="created_at upper bound, UTC."),
    order: str | None = Query(None, description="asc | desc"),
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    user: User = _SIGNED_IN,
) -> Page[dict[str, Any]]:
    if from_time and to_time and from_time > to_time:
        raise ValidationFailed("from_time is after to_time")
    stmt = select(Notification).where(Notification.user_id == user.id)
    if category:
        stmt = stmt.where(Notification.category == _enum(Category, category, "category"))
    if severity:
        stmt = stmt.where(Notification.severity == _enum(Severity, severity, "severity"))
    if environment:
        if environment.lower() not in ENVIRONMENTS:
            raise ValidationFailed(f"environment must be one of {', '.join(ENVIRONMENTS)}")
        stmt = stmt.where(Notification.environment == environment.lower())
    if unread is True:
        stmt = stmt.where(Notification.read_at.is_(None))
    elif unread is False:
        stmt = stmt.where(Notification.read_at.is_not(None))
    if event_type:
        stmt = stmt.where(Notification.event_type == event_type)
    if from_time:
        stmt = stmt.where(Notification.created_at >= from_time)
    if to_time:
        stmt = stmt.where(Notification.created_at <= to_time)
    stmt = SORTS.apply(stmt, None, order)
    rows, page = await paginate(db, stmt, params)
    return Page(items=[row.as_dict() for row in rows], page=page)


@router.get(
    "/unread-count",
    summary="How many unread notifications you have",
    description=(
        "One indexed count over `(user_id, read_at)`, never a fetch-and-count. "
        "Also broken down by severity, so a badge can be coloured by the worst "
        "thing waiting rather than only sized by how much is."
    ),
)
async def unread_count(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _SIGNED_IN,
) -> dict[str, Any]:
    service = _service(request)
    return {
        "unread": await service.unread_count(db, user.id),
        "by_severity": await service.counts_by_severity(db, user.id),
    }


@router.get(
    "/deliveries",
    response_model=Page[dict[str, Any]],
    summary="How your notifications were delivered, and what failed",
    description=(
        "Joined to your own notifications only. A failure reason is the "
        "provider's status line; no credential can appear in it, because no "
        "adapter is given one to put there."
    ),
)
async def list_deliveries(
    channel: str | None = Query(None, max_length=8),
    delivery_status: str | None = Query(None, alias="status", max_length=12),
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    user: User = _SIGNED_IN,
) -> Page[dict[str, Any]]:
    stmt = (
        select(NotificationDelivery)
        .join(Notification, Notification.id == NotificationDelivery.notification_id)
        .where(Notification.user_id == user.id)
    )
    if channel:
        stmt = stmt.where(NotificationDelivery.channel == _enum(Channel, channel, "channel"))
    if delivery_status:
        stmt = stmt.where(
            NotificationDelivery.status == _enum(DeliveryStatus, delivery_status, "status")
        )
    stmt = stmt.order_by(NotificationDelivery.created_at.desc())
    rows, page = await paginate(db, stmt, params)
    return Page(items=[row.as_dict() for row in rows], page=page)


@router.get(
    "/channels",
    summary="Which delivery channels this deployment has",
    description=(
        "Status only. A configured channel reports that it is configured and "
        "never how: no host, no address, no URL, no token. Section 33 of L35 "
        "and section 57 of L36 both ask for exactly this shape."
    ),
)
async def channels(request: Request, _: User = _SIGNED_IN) -> dict[str, Any]:
    return {"channels": _service(request).channels.describe()}


# ==================================================================== discord
#
# Two routes, both L35. Section 40 asks for only the endpoints that fit the
# existing architecture: status is the shape `/channels` already has, and a
# test send is the one thing that cannot be expressed as a preference.


@router.get(
    "/discord",
    summary="Whether Discord delivery is configured, and how it behaves",
    description=(
        "Status only. The webhook URL is never returned, not even redacted -- a "
        "redacted secret is still a statement about the secret's shape. "
        "`scope: system` says the deployment has one destination, which is why "
        "the Discord channel defaults to off for every category."
    ),
)
async def discord_status(request: Request, _: User = _SIGNED_IN) -> dict[str, Any]:
    described = _service(request).channels.get(Channel.discord).describe()
    return {
        **described,
        "how_to_enable": (
            "an administrator sets DISCORD_ENABLED and DISCORD_WEBHOOK_URL on the "
            "server. Neither is settable from the browser, and neither is ever "
            "returned by this API."
        ),
        "rotation": (
            "a leaked webhook cannot be rotated in place. Delete it in Discord "
            "(Server Settings -> Integrations -> Webhooks) and configure a new one; "
            "the old URL stops working the moment it is deleted."
        ),
    }


@router.post(
    "/discord/test",
    summary="Send a test message to the configured Discord channel",
    description=(
        "Sends a message that says it is a test. It creates no notification, "
        "publishes no event, and names no trade, symbol, price or account -- "
        "sections 38, 39 and 54: a synthetic TRADE_CLOSED to prove the wiring "
        "works would put a trade in the channel that never happened."
    ),
)
async def discord_test(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _SYSTEM_SETTINGS,
) -> dict[str, Any]:
    await _limit(request, "notifications:discord-test", BULK_LIMIT, user)
    adapter = _service(request).channels.get(Channel.discord)
    send = getattr(adapter, "send_test", None)
    if send is None:
        raise ValidationFailed(
            "Discord is not configured on this deployment, so there is nothing to test"
        )
    result = await send()
    # An administrative action against an external service. Recorded with who
    # did it and what came back -- and `_scrub` means no URL could reach the
    # row even if the detail carried one.
    await audit.record(
        db,
        audit.AuditAction.admin_action,
        "notification_channel",
        actor_user_id=user.id,
        resource_id=str(Channel.discord),
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        details={"action": "discord_test", "status": str(result.status)},
    )
    await db.commit()
    return {
        "status": str(result.status),
        "delivered": result.delivered,
        "detail": result.detail,
        "retryable": result.retryable,
        "note": (
            "a test message only. No trade, order, alert or notification was "
            "created, and no domain event was published."
        ),
    }


# ===================================================================== writes


@router.patch(
    "/{notification_id}/read",
    summary="Mark one notification read",
    description=(
        "Idempotent: marking an already-read notification read again leaves its "
        "timestamp alone, so 'when did I read this' stays true."
    ),
)
async def mark_read(
    request: Request,
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _SIGNED_IN,
) -> dict[str, Any]:
    await _limit(request, "notifications:read", WRITE_LIMIT, user)
    service = _service(request)
    row = await service.mark_read(db, user.id, notification_id)
    if row is None:
        # Deliberately the same answer as a notification that does not exist.
        raise NotFound("no such notification for this user")
    await db.commit()
    return {
        "notification": row.as_dict(),
        "unread": await service.unread_count(db, user.id),
    }


@router.post(
    "/read-all",
    summary="Mark every unread notification read",
    description=(
        "One UPDATE scoped to your own rows. Returns how many were actually "
        "unread, which is not the same as how many you have."
    ),
)
async def read_all(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _SIGNED_IN,
) -> dict[str, Any]:
    await _limit(request, "notifications:read-all", BULK_LIMIT, user)
    service = _service(request)
    changed = await service.mark_all_read(db, user.id)
    await db.commit()
    return {"marked_read": changed, "unread": await service.unread_count(db, user.id)}


# ================================================================ preferences


class PreferenceUpdate(BaseModel):
    """One (category, channel) pair. Everything is validated as an enum."""

    category: Category
    channel: Channel
    enabled: bool = True
    min_severity: Severity = Severity.info


class PreferencesBody(BaseModel):
    updates: list[PreferenceUpdate] = Field(..., min_length=1, max_length=60)


@router.get(
    "/preferences",
    summary="What you have asked to be told, and what the defaults are",
    description=(
        "The full grid, not only the rows you have saved. `source` says whether "
        "a value is yours or the platform default, and `locked` marks the in-app "
        "channel, which cannot be switched off -- errors and critical alerts "
        "always reach the notification centre."
    ),
)
async def get_preferences(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _SIGNED_IN,
) -> dict[str, Any]:
    service = _service(request)
    return {
        "preferences": await service.preferences_for(db, user.id),
        "locked": {
            "channel": str(Channel.in_app),
            "severities": sorted(str(s) for s in UNSUPPRESSIBLE),
            "why": (
                "a preference decides whether you are told. Switching off the record "
                "of a risk breach is how an operator learns about one from the broker "
                "instead. The floor may be raised to ERROR and no further."
            ),
        },
        "safety_note": (
            "notification preferences never affect risk enforcement, kill switches, "
            "order execution, position exits or broker reconciliation. Disabling risk "
            "email disables the email."
        ),
    }


@router.patch(
    "/preferences",
    summary="Change what you are told, per category and channel",
    description=(
        "Only your own. A setting that would silence an unsuppressible severity "
        "in-app is refused with the reason rather than quietly clamped -- a "
        "clamped setting is one you believe you made."
    ),
)
async def set_preferences(
    request: Request,
    body: PreferencesBody,
    db: AsyncSession = Depends(get_db),
    user: User = _SIGNED_IN,
) -> dict[str, Any]:
    await _limit(request, "notifications:preferences", WRITE_LIMIT, user)
    service = _service(request)
    try:
        for update in body.updates:
            await service.set_preference(
                db,
                user.id,
                category=update.category,
                channel=update.channel,
                enabled=update.enabled,
                min_severity=update.min_severity,
            )
    except prefs.PreferenceError as exc:
        await db.rollback()
        raise ValidationFailed(str(exc)) from exc
    await db.commit()
    return {"preferences": await service.preferences_for(db, user.id)}


def _enum(kind: Any, value: str, what: str) -> str:
    """A query parameter into an enum value, or a 422 naming the alternatives.

    The value a caller sends is a key into an enum, never interpolated. The
    same rule `app/api/pagination.py` applies to sort fields.
    """
    try:
        return str(kind(value.upper()))
    except ValueError as exc:
        allowed = ", ".join(str(m) for m in kind)
        raise ValidationFailed(f"{what} must be one of {allowed}") from exc


__all__ = ["BULK_LIMIT", "WRITE_LIMIT", "router"]
