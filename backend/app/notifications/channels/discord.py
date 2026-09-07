"""The Discord channel (L35). A webhook, an embed, and nothing else.

L34 held this seat with an adapter that reported NOT_CONFIGURED. L35 fills it,
and the shape of everything around it is unchanged: no new preference column,
no new delivery status, no migration, no second event bus. `Channel.DISCORD`
has been in the enum, the preference grid, the delivery table and the channel
registry since L34, waiting for exactly this file.

**A webhook, not a bot.** Section 4 asks for a bot only when the project
genuinely needs commands, interactive responses or guild management. It needs
none of those: the requirement is outbound notification, a webhook does that
with no gateway connection, no token with guild-wide reach and no long-lived
process. Section 35 says do not implement commands unless required, so there
are none. If a later level needs `/status`, that is a bot and a separate
security design; a webhook cannot be escalated into one by accident.

**Nothing here is a trading path.** This module posts a message. It imports no
order manager, no risk engine and no broker adapter -- the same parse test that
covered `app/notifications` at L34 covers this file. Discord going down affects
Discord delivery and nothing else, which is section 25: the notification's own
delivery row records FAILED or RETRYING and the trade, the journal and the risk
decision are untouched.

**The URL never leaves this object.** It is read from settings in the
constructor and held on the instance. `describe()` reports `configured: true`
and the host is not in it. The delivery row records a status code, never a URL.
`_redact` scrubs it out of any provider message before that message is stored
or logged, because a Discord error body can echo the request.

**Why `urllib.request` in a thread rather than an HTTP client library.** The
same argument `email.py` makes for `smtplib`: the backend's runtime
dependencies are short and each is argued for in `requirements.txt`, and one
POST per notification, dispatched off the request path with a timeout, does not
justify adding one. If a later level needs a real HTTP client for something
else, this becomes three lines of that client.

**Rate limits are respected, not discovered.** Section 24. A webhook allows
roughly five requests per two seconds; the adapter holds a minimum interval
between sends and, on a 429, reads Discord's own `retry_after` and hands it to
the delivery worker rather than guessing at a backoff. A 429 is retryable; a
404 (the webhook was deleted) and a 401 are not -- retrying a webhook that does
not exist is how a queue stops draining.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from app.notifications.channels.base import ChannelState, UnavailableChannel
from app.notifications.contract import (
    Channel,
    DeliveryResult,
    DeliveryStatus,
    NotificationEnvelope,
    Severity,
)

log = logging.getLogger("app.notifications.discord")

#: Discord's documented shape for a webhook URL. Checked so a misconfiguration
#: is reported at status time rather than as a 404 per notification -- section
#: 41 asks for configuration validation that does not fail startup.
WEBHOOK_PATTERN = re.compile(
    r"^https://(?:\w+\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+$"
)

DEFAULT_TIMEOUT = 10.0

#: Section 24. Discord allows about five requests per two seconds per webhook.
#: 0.5s between sends keeps a burst of notifications comfortably under it
#: without making a single alert wait.
MIN_INTERVAL_SECONDS = 0.5

#: Embed colours, by severity. Decoration with a job: an operator scanning a
#: channel reads the stripe before the text.
COLOURS: dict[Severity, int] = {
    Severity.info: 0x5865F2,
    Severity.success: 0x2ECC71,
    Severity.warning: 0xE67E22,
    Severity.error: 0xE74C3C,
    Severity.critical: 0x992D22,
}

#: Section 12. A prefix an operator can filter on, and that reads correctly in
#: a notification popup where only the first line is visible.
MARKERS: dict[Severity, str] = {
    Severity.info: "",
    Severity.success: "✅ ",
    Severity.warning: "⚠️ ",
    Severity.error: "❗ ",
    Severity.critical: "🚨 CRITICAL — ",
}

#: Embed fields, in the order they are shown. Each is `(payload key, label)`,
#: and a key the notification did not carry produces no field -- the same rule
#: `templates.py` follows for the sentence. Section 14: only include fields
#: that exist.
FIELDS: tuple[tuple[str, str], ...] = (
    ("symbol", "Symbol"),
    ("side", "Direction"),
    ("strategy", "Strategy"),
    ("quantity", "Quantity"),
    ("net_profit", "P&L"),
    ("r_multiple", "R multiple"),
    ("model_key", "Model"),
    ("model_version", "Version"),
    ("bot_name", "Bot"),
    ("check", "Check"),
    ("reason", "Reason"),
)

#: Discord's own limits. Truncating here is better than a 400 from the API,
#: and the notification centre holds the untruncated text either way.
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024


class DiscordError(Exception):
    """A send that did not happen, with whether trying again could help."""

    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def looks_like_a_webhook(url: str) -> bool:
    return bool(WEBHOOK_PATTERN.match(url.strip()))


def _redact(text: str, secret: str) -> str:
    """Take the webhook out of anything that is about to be stored or logged.

    Section 36 and section 43. Discord's error bodies can echo the request, and
    a failure reason is written to a delivery row a user can read. Belt and
    braces: the token segment is removed as well as the whole URL, so a partial
    echo does not leak the part that authenticates.
    """
    if not secret:
        return text
    out = text.replace(secret, "[redacted webhook]")
    token = secret.rstrip("/").rsplit("/", 1)[-1]
    if len(token) > 8:
        out = out.replace(token, "[redacted]")
    return out


def build_embed(envelope: NotificationEnvelope, *, base_url: str = "") -> dict[str, Any]:
    """One embed from one notification. Sections 8, 9, 14, 28 and 30.

    Every value comes off the envelope, which came off a recorded event. There
    is nothing in this function that can invent a P&L, a symbol or a strategy:
    a field whose key is absent produces no field at all, which is the same
    refusal `templates.py` makes for the sentence.
    """
    context = envelope.context
    fields: list[dict[str, Any]] = []

    # Section 9: the environment is mandatory and first, on every trading
    # message. `templates.stamp` has already put it in the title; it is
    # repeated as a field because a title is truncated in a mobile popup and
    # the environment is the one thing that must not be.
    if envelope.environment:
        fields.append(
            {"name": "Environment", "value": envelope.environment.upper(), "inline": True}
        )

    for key, label in FIELDS:
        value = context.get(key)
        if value in (None, ""):
            continue
        fields.append({"name": label, "value": str(value)[:MAX_FIELD_VALUE], "inline": True})

    # Section 28. The identifiers that let a message in a channel be traced
    # back to the event and the entity. Not a secret: an id is not a
    # capability anywhere in this platform -- every read is authorized against
    # the database, which is why `channels.py` refuses to trust one.
    if envelope.entity_type and envelope.entity_id:
        fields.append(
            {
                "name": envelope.entity_type.replace("_", " ").title(),
                "value": f"`{envelope.entity_id}`",
                "inline": False,
            }
        )

    embed: dict[str, Any] = {
        "title": (MARKERS.get(envelope.severity, "") + envelope.title)[:MAX_TITLE],
        "description": (envelope.body or "")[:MAX_DESCRIPTION],
        "color": COLOURS.get(envelope.severity, COLOURS[Severity.info]),
        "fields": fields,
        "footer": {"text": f"{envelope.category} · {envelope.severity} · {envelope.event_type}"},
    }
    if envelope.created_at is not None:
        embed["timestamp"] = envelope.created_at.isoformat()
    # Section 31. Only when a base URL is configured, and never carrying a
    # token: the link opens the notification centre, which authenticates the
    # reader the way every other page does.
    link = context.get("link")
    if isinstance(link, str) and link:
        embed["url"] = link
    elif base_url and envelope.entity_type:
        embed["url"] = f"{base_url}/alerts?notification={envelope.notification_id}"
    return embed


def verification_embed() -> dict[str, Any]:
    """Sections 38 and 39. A configuration test that is visibly a test.

    It creates no notification row, publishes no event and names no trade,
    symbol, price or account. A test that fabricated a TRADE_CLOSED to prove
    the wiring works would put a trade in the channel that never happened --
    section 54, and the reason section 39 asks for a separate path rather than
    a synthetic domain event.
    """
    return {
        "title": "🔧 Discord configuration test",
        "description": (
            "This is a test of the notification webhook. **No trading action was "
            "performed** and nothing was recorded: no trade, no order, no alert."
        ),
        "color": 0x95A5A6,
        "footer": {"text": "trade-research · configuration test"},
    }


@dataclass
class DiscordWebhookChannel:
    """One webhook. Section 7.

    It receives a notification, formats it, sends it and reports what happened.
    It cannot create a trade, modify an order, read a broker credential, change
    a risk limit or start a bot, and it is given no object through which it
    could: a `NotificationEnvelope` carries recorded fields and nothing else.
    """

    webhook_url: str
    enabled: bool = True
    username: str = "trade-research"
    timeout: float = DEFAULT_TIMEOUT
    base_url: str = ""
    min_interval: float = MIN_INTERVAL_SECONDS
    channel: Channel = Channel.discord
    #: Wall-clock of the last send, for the client-side rate gate. Per-process:
    #: two API processes would each hold their own, which is why the 429 path
    #: exists as well rather than instead.
    _last_send: float = field(default=0.0, repr=False)
    #: Counters for L37 to read. Counts only, never a payload.
    sent: int = field(default=0, repr=False)
    failed: int = field(default=0, repr=False)
    rate_limited: int = field(default=0, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    @property
    def valid(self) -> bool:
        return self.configured and looks_like_a_webhook(self.webhook_url)

    @property
    def available(self) -> bool:
        return self.enabled and self.valid

    # ---------------------------------------------------------------- sending

    async def send(self, envelope: NotificationEnvelope) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(
                status=DeliveryStatus.skipped,
                detail="Discord delivery is disabled on this deployment",
                retryable=False,
            )
        if not self.configured:
            return DeliveryResult(
                status=DeliveryStatus.skipped,
                detail="no Discord webhook is configured",
                retryable=False,
            )
        if not self.valid:
            # Configuration, not weather. Section 30: retrying this forever
            # would never succeed and would hide the real problem.
            return DeliveryResult(
                status=DeliveryStatus.failed,
                detail=(
                    "the configured Discord webhook is not a webhook URL. "
                    "Expected https://discord.com/api/webhooks/<id>/<token>"
                ),
                retryable=False,
            )
        return await self.post(
            {"username": self.username, "embeds": [build_embed(envelope, base_url=self.base_url)]}
        )

    async def send_test(self) -> DeliveryResult:
        """Sections 38 and 40. Used by the admin route; sends no trading data."""
        if not self.available:
            return DeliveryResult(
                status=DeliveryStatus.skipped,
                detail=self.describe()["detail"],
                retryable=False,
            )
        return await self.post({"username": self.username, "embeds": [verification_embed()]})

    async def post(self, body: dict[str, Any]) -> DeliveryResult:
        """One HTTP POST, classified. Never raises into the worker."""
        import asyncio

        await self._wait_for_the_rate_gate()
        started = time.monotonic()
        try:
            status_code = await asyncio.to_thread(self._post, body)
        except DiscordError as exc:
            if exc.retry_after is not None:
                self.rate_limited += 1
            else:
                self.failed += 1
            return DeliveryResult(
                status=(DeliveryStatus.retrying if exc.retryable else DeliveryStatus.failed),
                detail=_redact(str(exc), self.webhook_url)[:500],
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after,
            )
        self.sent += 1
        log.info(
            "discord notification sent",
            extra={
                "event": "discord_sent",
                "status": status_code,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
        # A webhook returns 204 with no body unless `?wait=true` is used, which
        # this does not: waiting for the message object doubles the latency to
        # learn an id nothing needs. None is the honest answer -- an invented
        # id would be traced to a message that does not carry it.
        return DeliveryResult(status=DeliveryStatus.delivered, provider_message_id=None)

    async def _wait_for_the_rate_gate(self) -> None:
        import asyncio

        elapsed = time.monotonic() - self._last_send
        if self._last_send and elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        self._last_send = time.monotonic()

    def _post(self, body: dict[str, Any]) -> int:
        request = urllib.request.Request(  # noqa: S310 - the URL is operator configuration
            self.webhook_url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "trade-research"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return int(response.status)
        except urllib.error.HTTPError as exc:
            raise self._classify(exc) from exc
        except urllib.error.URLError as exc:
            # DNS, TLS, connection refused. Weather, not configuration.
            raise DiscordError(f"network error: {exc.reason}", retryable=True) from exc
        except TimeoutError as exc:
            raise DiscordError("the request timed out", retryable=True) from exc

    def _classify(self, exc: urllib.error.HTTPError) -> DiscordError:
        """Section 47. Which failures are worth trying again, and which are not."""
        code = int(exc.code)
        if code == 429:
            return DiscordError(
                "rate limited by Discord", retryable=True, retry_after=self._retry_after(exc)
            )
        if code in (401, 403):
            return DiscordError(
                f"Discord refused the webhook ({code}). The webhook may have been "
                "revoked; it needs to be rotated.",
                retryable=False,
            )
        if code == 404:
            return DiscordError(
                "the webhook does not exist (404). It was deleted, or the URL is wrong.",
                retryable=False,
            )
        if code == 400:
            # Our message, not their server. Retrying an identical bad payload
            # produces an identical 400 forever.
            return DiscordError("Discord rejected the message (400)", retryable=False)
        if 500 <= code < 600:
            return DiscordError(f"Discord server error ({code})", retryable=True)
        return DiscordError(f"unexpected response ({code})", retryable=code >= 500)

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> float | None:
        """Discord's own instruction, read from the header or the JSON body.

        Section 24: respect what the provider asked for rather than guessing.
        Capped, because a hostile or broken value must not park a delivery for
        a day.
        """
        header = exc.headers.get("Retry-After") if exc.headers else None
        value: float | None = None
        if header:
            try:
                value = float(header)
            except ValueError:
                value = None
        if value is None:
            try:
                body = json.loads(exc.read().decode("utf-8", "replace") or "{}")
                raw = body.get("retry_after")
                value = float(raw) if raw is not None else None
            except (ValueError, AttributeError, OSError):
                value = None
        if value is None:
            return None
        return max(0.0, min(value, 300.0))

    # -------------------------------------------------------------- reporting

    def describe(self) -> dict[str, Any]:
        """Sections 33, 40 and 42. Status only. Never the URL, never the token.

        This is what the API returns and what the admin panel renders. There is
        no branch of it that includes `self.webhook_url`, not even redacted: a
        redacted secret is still a statement about the secret's shape.
        """
        if not self.enabled:
            state, detail = ChannelState.disabled, "Discord delivery is switched off"
        elif not self.configured:
            state, detail = (
                ChannelState.not_configured,
                "set DISCORD_WEBHOOK_URL to enable Discord delivery",
            )
        elif not self.valid:
            state, detail = (
                ChannelState.not_configured,
                "the configured value is not a Discord webhook URL",
            )
        else:
            state, detail = ChannelState.configured, "delivering through a webhook"
        return {
            "channel": str(self.channel),
            "state": state,
            "available": self.available,
            "detail": detail,
            "transport": "webhook",
            # Section 34. Stated rather than left to be inferred: one webhook
            # for the deployment, so every user who enables Discord shares the
            # destination. That is why the default is off for every category.
            "scope": "system",
            "commands": False,
            "sent": self.sent,
            "failed": self.failed,
            "rate_limited": self.rate_limited,
        }


def discord_channel_from(settings: Any) -> Any:
    """The Discord adapter for this deployment. Section 41.

    Never raises and never fails startup: Discord is optional, and a
    misconfiguration is reported as a state rather than as a crash. A
    deployment with `DISCORD_ENABLED=false` gets the L34 seat back, which
    records SKIPPED rather than FAILED -- "switched off" is not "broken".
    """
    if not bool(getattr(settings, "discord_enabled", False)):
        return UnavailableChannel(
            Channel.discord,
            "Discord delivery is switched off (DISCORD_ENABLED=false)",
            state=ChannelState.disabled,
        )
    return DiscordWebhookChannel(
        webhook_url=(getattr(settings, "discord_webhook_url", "") or "").strip(),
        enabled=True,
        username=getattr(settings, "discord_username", "") or "trade-research",
        timeout=float(getattr(settings, "discord_timeout_seconds", DEFAULT_TIMEOUT)),
        base_url=(getattr(settings, "app_base_url", "") or "").rstrip("/"),
    )


__all__ = [
    "COLOURS",
    "DEFAULT_TIMEOUT",
    "FIELDS",
    "MARKERS",
    "MIN_INTERVAL_SECONDS",
    "WEBHOOK_PATTERN",
    "DiscordError",
    "DiscordWebhookChannel",
    "build_embed",
    "discord_channel_from",
    "looks_like_a_webhook",
    "verification_embed",
]
