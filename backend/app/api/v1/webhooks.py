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
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.auth.ratelimit import RateLimit
from app.core.errors import request_id_of
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
