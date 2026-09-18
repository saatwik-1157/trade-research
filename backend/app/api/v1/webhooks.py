"""The TradingView webhook route.

Unauthenticated by necessity and secret-checked by design: TradingView posts
from its own servers with no session and cannot set headers, so the shared
secret travels in the body. That makes this the one route on the platform an
anonymous caller can reach and cause a write with, which is why it is the most
heavily gated:

  * a body cap, enforced before the body is read into memory;
  * rate limiting per source address, reusing L04's limiter;
  * a constant-time secret check that refuses everything when unconfigured;
  * an age check, so a replayed alert cannot arrive as a fresh one;
  * an idempotency key, so a retry storm produces one signal.

**A 200 means recorded.** It never means filled, opened or executed, and the
response body says so in as many words. The `status` field is the webhook
layer's own vocabulary -- accepted, duplicate, rejected -- and there is no
value it can take that describes a trade.

CSRF does not apply: the middleware only challenges requests that carry a
session cookie, and this one carries none. Rate limiting is the protection
here, exactly as it is for sign-in.

**One write door and two read doors.** `GET /events` and `GET /events/{id}`
answer the one question the table exists for -- "did our alert arrive, and if
it did not act, why" -- which until they existed was answerable only by
opening the database. Both are gated on `manage_brokers`, like the status
route. Neither touches the gateway, so this module holds no secret and there
is no value it could put back into a payload that was redacted on write; it
also means the read surface still answers when the receiver is unconfigured,
which is exactly when an operator most needs to read the rejections.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, SortSpec, page_params, paginate
from app.api.v1.schemas import WebhookEventDetailOut, WebhookEventOut
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.auth.ratelimit import RateLimit
from app.core.errors import NotFound, ValidationFailed, request_id_of
from app.models.signals import WebhookEvent
from app.webhooks.gateway import Outcome, Unauthorized, WebhookGateway
from app.webhooks.schema import MAX_BODY_BYTES

log = logging.getLogger("app.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_BROKERS = Depends(require_permission(Permission.manage_brokers))

# Generous enough for a busy multi-symbol alert set, tight enough that a
# runaway sender is throttled rather than filling the signal table. A refusal
# here is a 429 with a retry hint; it never silently drops an alert, because a
# dropped alert and an alert that never fired look identical to the sender.
WEBHOOK_LIMIT = RateLimit(limit=120, window_seconds=60)

EVENT_SORTS = SortSpec(
    columns={"received_at": WebhookEvent.received_at, "status": WebhookEvent.status},
    default="received_at",
)
# Mirrors the CHECK constraints on the table (`app/models/signals.py`), named
# here so the fail-closed filter check and its test can both read them.
EVENT_STATUSES = ("accepted", "rejected", "duplicate")
AUTH_STRENGTHS = ("strong", "weak", "none")


def _naive_utc(value: datetime | None) -> datetime | None:
    """`received_at` is stored naive-UTC. A caller may send an offset, and
    comparing an aware datetime to a naive column raises on some drivers, so
    the bound is normalised exactly as the gateway normalises the write."""
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _one_of(name: str, value: str | None, allowed: tuple[str, ...]) -> None:
    """Refuse an unrecognised filter rather than ignoring it.

    A typo'd `?status=accpeted` that returned the whole table would read as
    "these are the accepted ones", which is a false statement about the
    record -- the same argument `app/api/pagination.py` makes about a
    silently truncated page.
    """
    if value is not None and value not in allowed:
        raise ValidationFailed(f"{name} must be one of {', '.join(allowed)}, not {value!r}")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _gateway(request: Request) -> WebhookGateway:
    gateway: WebhookGateway = request.app.state.webhook_gateway
    return gateway


@router.post(
    "/tradingview",
    summary="Receive a TradingView alert",
    description=(
        "Records a signal. **Nothing is executed.** A 200 means the alert was "
        "accepted at the webhook layer and a Signal row exists with status "
        "`new`; between that row and a broker there must be a strategy engine, "
        "an AI layer, a risk engine, position sizing and an OMS, and none of "
        "them is built. Send the shared secret in the alert body as `secret` "
        "-- TradingView cannot set headers. Duplicate alerts return 200 with "
        "status `duplicate` and create no second signal."
    ),
    responses={
        200: {"description": "accepted or duplicate; recorded, not executed"},
        401: {"description": "bad or missing secret"},
        413: {"description": "body larger than the cap"},
        422: {"description": "rejected: unparseable, stale, unmapped or unknown strategy"},
        429: {"description": "rate limited"},
    },
)
async def tradingview(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    correlation_id = request_id_of(request)
    ip = _client_ip(request)

    limiter = request.app.state.rate_limiter
    decision = await limiter.hit("webhook_tv", ip, WEBHOOK_LIMIT)
    if not decision.allowed:
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        response.headers["Retry-After"] = str(decision.retry_after)
        return {
            "status": "rejected",
            "detail": "rate limited",
            "note": "a recorded signal, not a trade; nothing was executed",
        }

    # Checked before reading, so an oversized body is refused rather than
    # buffered. An alert is a few hundred bytes; anything larger is not one.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        response.status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        return {"status": "rejected", "detail": "body too large"}

    raw = (await request.body()).decode("utf-8", errors="replace")
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        response.status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        return {"status": "rejected", "detail": "body too large"}

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # A plain-text alert is legal TradingView usage: Pine's `alert()` can
        # send a bare string. It is wrapped so the secret check can still run,
        # and it will then fail validation for want of a ticker -- which is the
        # correct outcome, stated rather than guessed around.
        payload = {"message": raw}
    if not isinstance(payload, dict):
        payload = {"message": str(payload)[:512]}

    gateway = _gateway(request)
    try:
        result, event = await gateway.receive(
            db, payload, raw, source_ip=ip, correlation_id=correlation_id
        )
    except Unauthorized as exc:
        # Logged with the source so a probing sender is visible; answered
        # without saying which part of the check failed.
        log.warning(
            "webhook unauthorized",
            extra={"event": "webhook_unauthorized", "source_ip": ip, "reason": str(exc)},
        )
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"status": "unauthorized", "detail": "unauthorized"}

    # Committed BEFORE publishing: an event announcing a row that is not yet
    # durable is an event a subscriber can act on before it can read it.
    await db.commit()

    if event is not None:
        try:
            await request.app.state.hub.publish(event)
        except Exception:  # noqa: BLE001 - the signal is already durable
            # The signal is recorded and the API still answers 200, because it
            # is. A lost event costs a live update, not a signal, and the
            # alternative -- reporting failure for a stored alert -- would
            # invite the sender to retry something that already succeeded.
            log.warning(
                "signal recorded but the event was not published",
                extra={
                    "event": "webhook_publish_failed",
                    "signal_id": result.signal_id,
                    "correlation_id": correlation_id,
                },
            )

    response.status_code = result.http_status
    return result.as_dict()


@router.get(
    "/tradingview",
    summary="Receiver status and the alert template to paste into TradingView",
    description=(
        "Never returns the secret, and states plainly whether one is "
        "configured. The template is the JSON body an alert should send."
    ),
)
async def tradingview_status(request: Request, _: User = _BROKERS) -> dict[str, object]:
    gateway = _gateway(request)
    return {
        "endpoint": "/v1/webhooks/tradingview",
        "method": "POST",
        # Whether one exists, never what it is.
        "secret_configured": bool(gateway.secret),
        "mode": gateway.mode,
        "max_alert_age_seconds": gateway.max_age_seconds,
        "future_tolerance_seconds": gateway.future_tolerance_seconds,
        "restrict_to_tradingview_ips": gateway.restrict_to_tradingview_ips,
        "rate_limit": {
            "requests": WEBHOOK_LIMIT.limit,
            "window_seconds": WEBHOOK_LIMIT.window_seconds,
        },
        "max_body_bytes": MAX_BODY_BYTES,
        "outcomes": [str(o) for o in Outcome],
        "alert_template": {
            "secret": "<your secret>",
            "ticker": "{{ticker}}",
            "action": "{{strategy.order.action}}",
            "price": "{{close}}",
            "time": "{{timenow}}",
            "timeframe": "{{interval}}",
        },
        "note": (
            "An accepted alert becomes a Signal with status 'new'. It is not "
            "executed: the strategy engine, risk engine, sizing and OMS are not "
            "built, and no path from this endpoint to a broker exists."
        ),
    }


# ===================================================== the recorded alerts


@router.get(
    "/events",
    response_model=Page[WebhookEventOut],
    summary="Alerts received, accepted and refused",
    description=(
        "Newest first. Every authenticated alert leaves a row, including one "
        "that was refused -- 'we never received it' and 'we received it and "
        "would not act on it' are different answers, and only one of them "
        "means the sender should look at its own config. A duplicate creates "
        "NO row: a replay resolves onto the row it duplicates, so the count "
        "here is the count of distinct alerts, not of deliveries. The payload "
        "is not on a list row; read one event for it. **Nothing here was "
        "executed.**"
    ),
)
async def list_webhook_events(
    params: PageParams = Depends(page_params),
    status_filter: str | None = Query(None, alias="status", description="accepted | rejected"),
    provider: str | None = Query(None, max_length=16),
    auth_strength: str | None = Query(None, description="strong | weak | none"),
    signal_id: str | None = Query(None, max_length=36),
    from_time: datetime | None = Query(None, description="received_at lower bound, UTC."),
    to_time: datetime | None = Query(None, description="received_at upper bound, UTC."),
    sort: str | None = Query(None, description="received_at | status"),
    order: str | None = Query(None, description="asc | desc"),
    db: AsyncSession = Depends(get_db),
    _: User = _BROKERS,
) -> Page[WebhookEventOut]:
    # `status_filter` rather than `status`: this module imports `fastapi.status`
    # and uses it in the POST handler, and a bare parameter would shadow it.
    _one_of("status", status_filter, EVENT_STATUSES)
    _one_of("auth_strength", auth_strength, AUTH_STRENGTHS)
    if from_time and to_time and from_time > to_time:
        raise ValidationFailed("from_time is after to_time")

    stmt = select(WebhookEvent)
    if status_filter:
        stmt = stmt.where(WebhookEvent.status == status_filter)
    if provider:
        stmt = stmt.where(WebhookEvent.provider == provider)
    if auth_strength:
        stmt = stmt.where(WebhookEvent.auth_strength == auth_strength)
    if signal_id:
        stmt = stmt.where(WebhookEvent.signal_id == signal_id)
    lower, upper = _naive_utc(from_time), _naive_utc(to_time)
    if lower:
        stmt = stmt.where(WebhookEvent.received_at >= lower)
    if upper:
        stmt = stmt.where(WebhookEvent.received_at <= upper)

    # Offset paging over an unindexed `received_at`, which `app/api/pagination.py`
    # argues for at these row counts. An index would mean a migration, which
    # would turn a read-only feature into a schema change.
    stmt = EVENT_SORTS.apply(stmt, sort, order)
    rows, page = await paginate(db, stmt, params)
    items = [WebhookEventOut.model_validate(r, from_attributes=True) for r in rows]
    return Page[WebhookEventOut](items=items, page=page)


@router.get(
    "/events/{event_id}",
    response_model=WebhookEventDetailOut,
    summary="One recorded alert, with the payload as it was stored",
    description=(
        "The payload is served exactly as it sits in the row. It was redacted "
        "on write by the only component that holds the secret, and this route "
        "imports neither the gateway nor the settings, so there is no value it "
        "could put back. A 200 describes an alert that was RECORDED; it never "
        "means anything was executed."
    ),
)
async def get_webhook_event(
    event_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _BROKERS,
) -> WebhookEventDetailOut:
    row = await db.get(WebhookEvent, event_id)
    if row is None:
        raise NotFound(f"no webhook event {event_id}")
    return WebhookEventDetailOut.model_validate(row, from_attributes=True)
