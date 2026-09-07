"""The Discord notification channel (L35).

The ones that matter most:

  * `test_the_webhook_never_appears_in_anything_a_caller_can_read` — §36, §50.
  * `test_a_provider_error_that_echoes_the_url_is_redacted` — §36, §43.
  * `test_a_404_is_not_retried_and_a_429_is` — §23, §47.
  * `test_discord_honours_the_retry_after_it_was_given` — §24.
  * `test_a_trade_event_reaches_discord_through_the_notification_service` — §48.
  * `test_discord_being_down_changes_nothing_about_the_trade` — §25, §49.
  * `test_one_event_delivered_twice_is_one_discord_message` — §22, §51.
  * `test_every_environment_is_distinguishable` — §9, §52.
  * `test_the_test_message_creates_no_trade_and_publishes_no_event` — §38, §39, §54.
  * `test_only_an_administrator_may_send_a_test` — §50.
"""

from __future__ import annotations

import ast
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import Role, User
from app.core.events import Event
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.models.accounts import PaperAccount
from app.models.ops import Notification, NotificationDelivery
from app.notifications.channels import build_registry
from app.notifications.channels.base import ChannelState
from app.notifications.channels.discord import (
    COLOURS,
    MARKERS,
    DiscordWebhookChannel,
    build_embed,
    discord_channel_from,
    looks_like_a_webhook,
    verification_embed,
)
from app.notifications.channels.inapp import InAppChannel
from app.notifications.contract import (
    Category,
    Channel,
    DeliveryStatus,
    NotificationEnvelope,
    Severity,
)
from app.notifications.service import NotificationService
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

MODULE = Path(__file__).resolve().parents[1] / "app" / "notifications" / "channels" / "discord.py"

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/aBcDeFgHiJkLmNoPqRsTuVwXyZ-0123"
TOKEN = WEBHOOK.rsplit("/", 1)[-1]

ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
BOB = {"email": "bob@tr-platform.io", "password": "another long passphrase"}
NOW = datetime(2026, 9, 5, 12, 0, 0)


class _Configured:
    """A deployment with Discord switched on."""

    discord_enabled = True
    discord_webhook_url = WEBHOOK
    discord_username = "trade-research"
    discord_timeout_seconds = 5.0
    app_base_url = "https://trade.example.com"
    smtp_host = ""
    email_notifications_enabled = True


class _Off(_Configured):
    discord_enabled = False


# ==================================================================== helpers


class _Response:
    def __init__(self, status: int = 204) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _http_error(code: int, headers: dict[str, str] | None = None, body: str = "") -> Exception:
    # `HTTPError` types its 4th parameter as `email.message.Message`, and a
    # plain dict is what every caller of this helper actually passes -- and what
    # `urllib` accepts at run time. Building a real `Message` here would make
    # the helper harder to read for no behavioural difference.
    return urllib.error.HTTPError(
        WEBHOOK,
        code,
        "error",
        headers or {},  # type: ignore[arg-type]
        BytesIO(body.encode("utf-8")),
    )


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every POST body instead of sending it."""
    sent: list[dict[str, Any]] = []

    def fake(request: Any, timeout: float | None = None) -> _Response:  # noqa: ANN401
        sent.append(json.loads(request.data.decode("utf-8")))
        return _Response(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return sent


def _raise(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fake(request: Any, timeout: float | None = None) -> _Response:  # noqa: ANN401
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def envelope(**over: Any) -> NotificationEnvelope:
    base: dict[str, Any] = {
        "notification_id": "n1",
        "event_id": "e1",
        "event_type": "TRADE_RECORDED",
        "category": Category.trading,
        "severity": Severity.info,
        "environment": "paper",
        "title": "[PAPER] Trade recorded",
        "body": "BUY EURUSD was journalled. Result 42.30 (1.4R).",
        "user_id": "u1",
        "entity_type": "trade",
        "entity_id": "t-abcdef12",
        "created_at": NOW,
        "context": {
            "symbol": "EURUSD",
            "side": "buy",
            "net_profit": "42.30",
            "r_multiple": "1.4",
            "trade_id": "t-abcdef12",
        },
    }
    base.update(over)
    return NotificationEnvelope(**base)


def channel(**over: Any) -> DiscordWebhookChannel:
    kwargs: dict[str, Any] = {
        "webhook_url": WEBHOOK,
        "min_interval": 0.0,  # the rate gate is tested on its own
        "base_url": "https://trade.example.com",
    }
    kwargs.update(over)
    return DiscordWebhookChannel(**kwargs)


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(User(id="u1", email="a@b.io", password_hash="x", role="trader"))
        db.add(
            PaperAccount(
                id="p1",
                user_id="u1",
                name="paper",
                currency="USD",
                starting_balance=Decimal("100000"),
                balance=Decimal("100000"),
                equity=Decimal("100000"),
            )
        )
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
async def db(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:
        yield session


def trade_event() -> Event:
    return Event(
        type="TRADE_RECORDED",
        payload={
            "trade_id": "t-abcdef12",
            "environment": "paper",
            "symbol": "EURUSD",
            "side": "buy",
            "net_profit": "42.30",
            "r_multiple": "1.4",
        },
        source="trade_journal",
        channel="account:p1",
    )


async def _enable_discord(service: NotificationService, db: AsyncSession) -> None:
    await service.set_preference(
        db,
        "u1",
        category=Category.trading,
        channel=Channel.discord,
        enabled=True,
        min_severity=Severity.info,
    )
    await db.commit()


# ============================================== 1. it is a channel, not a path


FORBIDDEN_IMPORTS = ("app.risk", "app.oms", "app.brokers", "app.sizing", "app.positions")


def test_the_discord_adapter_cannot_reach_anything_that_trades() -> None:
    """Sections 1, 7 and 63. Discord is a destination, never a trading path."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    assert not [m for m in imported if m.startswith(FORBIDDEN_IMPORTS)]


def test_no_trading_service_reaches_discord_directly() -> None:
    """Section 59. The only import of the adapter is the channel registry."""
    root = Path(__file__).resolve().parents[1] / "app"
    importers = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "channels.discord" in p.read_text(encoding="utf-8")
        or "channels import discord" in p.read_text(encoding="utf-8")
    )
    assert importers == ["notifications/channels/__init__.py"]


# ================================================= 2. configuration (§5, §41)


def test_a_webhook_url_is_recognised_and_a_wrong_one_is_not() -> None:
    assert looks_like_a_webhook(WEBHOOK)
    assert looks_like_a_webhook("https://discordapp.com/api/v10/webhooks/1234/abcd-EFGH_1234")
    assert not looks_like_a_webhook("https://example.com/hook")
    assert not looks_like_a_webhook("https://discord.com/api/webhooks/")
    assert not looks_like_a_webhook("")


def test_discord_off_is_a_state_not_a_failure() -> None:
    """Section 57. No repeated errors, no trading impact, no worker failure."""
    adapter = discord_channel_from(_Off())
    described = adapter.describe()
    assert described["state"] == ChannelState.disabled
    assert described["available"] is False


async def test_a_misconfigured_webhook_fails_rather_than_retrying_forever() -> None:
    """Section 41 and section 30: configuration is not weather."""
    adapter = channel(webhook_url="https://example.com/not-a-webhook")
    result = await adapter.send(envelope())
    assert result.status is DeliveryStatus.failed
    assert result.retryable is False
    assert "not a webhook URL" in result.detail


async def test_an_unconfigured_deployment_skips_rather_than_fails() -> None:
    result = await channel(webhook_url="").send(envelope())
    assert result.status is DeliveryStatus.skipped
    assert result.retryable is False


def test_the_application_starts_with_discord_misconfigured() -> None:
    """Section 41. An optional integration must not fail startup."""

    class _Bad:
        discord_enabled = True
        discord_webhook_url = "nonsense"

    adapter = discord_channel_from(_Bad())
    assert adapter.available is False
    assert adapter.describe()["state"] == ChannelState.not_configured


# ===================================================== 3. secrets (§36, §43)


async def test_the_webhook_never_appears_in_anything_a_caller_can_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sections 36, 37 and 50."""
    adapter = channel()
    described = json.dumps(adapter.describe())
    assert WEBHOOK not in described
    assert TOKEN not in described
    assert "discord.com" not in described

    registry = json.dumps(build_registry(_Configured()).describe())
    assert WEBHOOK not in registry and TOKEN not in registry


async def test_a_provider_error_that_echoes_the_url_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Discord error body can echo the request. The stored reason must not."""
    _raise(monkeypatch, urllib.error.URLError(f"cannot reach {WEBHOOK}"))
    result = await channel().send(envelope())
    assert WEBHOOK not in result.detail
    assert TOKEN not in result.detail
    assert "[redacted" in result.detail


async def test_no_secret_reaches_a_delivery_row(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raise(monkeypatch, urllib.error.URLError(f"boom {WEBHOOK}"))
    service = NotificationService(build_registry(_Configured()))
    await _enable_discord(service, db)
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    rows = list((await db.scalars(select(NotificationDelivery))).all())
    stored = json.dumps([r.as_dict() for r in rows])
    assert WEBHOOK not in stored and TOKEN not in stored


def test_the_source_contains_no_webhook() -> None:
    """Section 5 and section 36: never hard-coded."""
    source = MODULE.read_text(encoding="utf-8")
    assert "discord.com/api/webhooks/1" not in source


# ================================================ 4. the message (§8, 14, 30)


def test_the_embed_carries_only_recorded_fields() -> None:
    """Section 8 and section 14. Never fabricate a value."""
    embed = build_embed(envelope())
    names = {f["name"]: f["value"] for f in embed["fields"]}
    assert names["Environment"] == "PAPER"
    assert names["Symbol"] == "EURUSD"
    assert names["P&L"] == "42.30"
    assert names["R multiple"] == "1.4"
    # Nothing the event did not carry.
    assert "Strategy" not in names
    assert "Model" not in names


def test_a_missing_field_produces_no_field_rather_than_a_placeholder() -> None:
    embed = build_embed(envelope(context={"trade_id": "t1"}))
    names = {f["name"] for f in embed["fields"]}
    assert "P&L" not in names and "Symbol" not in names
    assert not any("None" in str(f["value"]) for f in embed["fields"])


def test_every_environment_is_distinguishable() -> None:
    """Sections 9 and 52. Paper must never read as live."""
    seen = {}
    for environment in ("backtest", "paper", "demo", "live", "unknown"):
        embed = build_embed(envelope(environment=environment))
        value = [f for f in embed["fields"] if f["name"] == "Environment"][0]["value"]
        seen[environment] = value
    assert seen == {
        "backtest": "BACKTEST",
        "paper": "PAPER",
        "demo": "DEMO",
        "live": "LIVE",
        "unknown": "UNKNOWN",
    }
    assert len(set(seen.values())) == 5


def test_the_environment_is_a_field_as_well_as_a_title() -> None:
    """A title is truncated in a popup; the environment must not be."""
    embed = build_embed(envelope())
    assert embed["fields"][0]["name"] == "Environment"


def test_severity_colours_and_markers_are_distinct() -> None:
    assert len(set(COLOURS.values())) == len(Severity)
    critical = build_embed(envelope(severity=Severity.critical, title="Risk limit breached"))
    assert critical["color"] == COLOURS[Severity.critical]
    assert critical["title"].startswith(MARKERS[Severity.critical])


def test_an_unknown_order_state_is_not_reported_as_a_failure() -> None:
    """Section 13. Discord repeats the notification; it does not reinterpret it."""
    embed = build_embed(
        envelope(
            event_type="ORDER_UNKNOWN",
            severity=Severity.error,
            title="[PAPER] Order state requires reconciliation",
            body=(
                "The broker's state for o-1 could not be established. It is being "
                "reconciled; no conclusion has been drawn."
            ),
            entity_type="order",
            entity_id="o-1",
            context={"order_id": "o-1"},
        )
    )
    text = json.dumps(embed).lower()
    assert "reconcil" in text
    assert "order failed" not in text


def test_the_embed_carries_the_ids_that_trace_it_back() -> None:
    """Section 28. An id is not a capability anywhere in this platform."""
    embed = build_embed(envelope())
    assert any("t-abcdef12" in str(f["value"]) for f in embed["fields"])
    assert embed["footer"]["text"].startswith("TRADING")


def test_a_link_is_included_only_when_a_base_url_is_configured() -> None:
    """Section 31. Never a hard-coded localhost, never a token in a URL."""
    with_url = build_embed(envelope(), base_url="https://trade.example.com")
    assert with_url["url"] == "https://trade.example.com/alerts?notification=n1"
    assert "token" not in with_url["url"]
    without = build_embed(envelope(), base_url="")
    assert "url" not in without


def test_long_text_is_truncated_rather_than_rejected_by_discord() -> None:
    embed = build_embed(envelope(body="x" * 9000, title="y" * 500))
    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= 4096


# ============================================ 5. the provider (§23, 24, 47)


async def test_a_successful_send_posts_one_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture(monkeypatch)
    result = await channel().send(envelope())
    assert result.delivered
    assert len(sent) == 1
    assert len(sent[0]["embeds"]) == 1
    # A webhook returns no message object without ?wait=true, so None is the
    # honest answer rather than an invented id.
    assert result.provider_message_id is None


@pytest.mark.parametrize(
    ("code", "retryable"),
    [(400, False), (401, False), (403, False), (404, False), (500, True), (503, True)],
)
async def test_a_404_is_not_retried_and_a_429_is(
    monkeypatch: pytest.MonkeyPatch, code: int, retryable: bool
) -> None:
    """Section 47. Retrying a webhook that does not exist never drains."""
    _raise(monkeypatch, _http_error(code))
    result = await channel().send(envelope())
    assert result.retryable is retryable
    assert result.status is (DeliveryStatus.retrying if retryable else DeliveryStatus.failed)


async def test_a_rate_limit_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _raise(monkeypatch, _http_error(429, {"Retry-After": "2.5"}))
    result = await channel().send(envelope())
    assert result.status is DeliveryStatus.retrying
    assert result.retryable is True


async def test_discord_honours_the_retry_after_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 24. What the provider asked for, not a guess."""
    _raise(monkeypatch, _http_error(429, {"Retry-After": "7"}))
    assert (await channel().send(envelope())).retry_after_seconds == 7.0

    # And from the JSON body when the header is absent, which is what Discord
    # actually sends on a webhook.
    _raise(monkeypatch, _http_error(429, {}, json.dumps({"retry_after": 3.25})))
    assert (await channel().send(envelope())).retry_after_seconds == 3.25


async def test_an_absurd_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken or hostile value must not park a delivery for a day."""
    _raise(monkeypatch, _http_error(429, {"Retry-After": "999999"}))
    assert (await channel().send(envelope())).retry_after_seconds == 300.0


async def test_a_timeout_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _raise(monkeypatch, TimeoutError())
    result = await channel().send(envelope())
    assert result.retryable is True
    assert "timed out" in result.detail


async def test_a_network_failure_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _raise(monkeypatch, urllib.error.URLError("connection refused"))
    result = await channel().send(envelope())
    assert result.retryable is True


async def test_the_rate_gate_spaces_consecutive_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 24. Client-side, so a burst does not reach the 429 path at all."""
    import time

    _capture(monkeypatch)
    adapter = channel(min_interval=0.05)
    started = time.monotonic()
    await adapter.send(envelope())
    await adapter.send(envelope())
    assert time.monotonic() - started >= 0.05


def test_counters_are_counts_and_never_payloads() -> None:
    described = channel().describe()
    assert described["sent"] == 0
    assert set(described) >= {"sent", "failed", "rate_limited", "transport", "scope"}
    assert "webhook_url" not in described


# ============================================= 6. through the service (§48)


async def test_a_trade_event_reaches_discord_through_the_notification_service(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 48 and section 59. Event -> service -> preference -> adapter."""
    sent = _capture(monkeypatch)
    service = NotificationService(build_registry(_Configured()))
    await _enable_discord(service, db)
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()

    row = await db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel == str(Channel.discord))
    )
    assert row is not None and row.status == str(DeliveryStatus.delivered)
    assert len(sent) == 1
    assert sent[0]["embeds"][0]["title"].startswith("[PAPER]")


async def test_discord_is_not_used_when_the_preference_says_no(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 11. The default is off; nothing bypasses the preference."""
    sent = _capture(monkeypatch)
    service = NotificationService(build_registry(_Configured()))
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    channels = {r.channel for r in (await db.scalars(select(NotificationDelivery))).all()}
    assert channels == {str(Channel.in_app)}
    assert sent == []


async def test_one_event_delivered_twice_is_one_discord_message(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sections 22 and 51. L34's idempotency, reused rather than reinvented."""
    sent = _capture(monkeypatch)
    service = NotificationService(build_registry(_Configured()))
    await _enable_discord(service, db)
    event = trade_event()
    await service.on_event(db, event)
    await db.commit()
    await service.on_event(db, event)
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    assert len(sent) == 1
    assert len(list((await db.scalars(select(Notification))).all())) == 1


async def test_a_discord_failure_does_not_stop_the_other_channels(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 26. IN_APP success and DISCORD failure is a normal outcome."""
    _raise(monkeypatch, _http_error(500))
    service = NotificationService(build_registry(_Configured()))
    await _enable_discord(service, db)
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    rows = {r.channel: r.status for r in (await db.scalars(select(NotificationDelivery))).all()}
    assert rows[str(Channel.in_app)] == str(DeliveryStatus.delivered)
    assert rows[str(Channel.discord)] == str(DeliveryStatus.retrying)


async def test_discord_retries_are_bounded(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 23. Bounded, using L34's worker policy rather than a new one."""
    _raise(monkeypatch, _http_error(503))
    service = NotificationService(build_registry(_Configured()))
    await _enable_discord(service, db)
    await service.on_event(db, trade_event())
    await db.commit()
    row = await db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel == str(Channel.discord))
    )
    assert row is not None
    for _ in range(5):
        await service.attempt(db, row, now=datetime(2027, 1, 1))
        await db.commit()
    assert row.status == str(DeliveryStatus.failed)


# ============================================== 7. failure isolation (§25, 49)


async def test_discord_being_down_changes_nothing_about_the_trade(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 25 and section 49, the mandatory one."""
    _raise(monkeypatch, urllib.error.URLError("discord is gone"))
    account = await db.scalar(select(PaperAccount).where(PaperAccount.id == "p1"))
    assert account is not None
    before = account.balance

    service = NotificationService(build_registry(_Configured()))
    await _enable_discord(service, db)
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()

    account_after = await db.scalar(select(PaperAccount).where(PaperAccount.id == "p1"))
    assert account_after is not None
    after = account_after.balance
    assert before == after
    # And the notification itself survived the channel that did not.
    notification = await db.scalar(select(Notification))
    assert notification is not None and notification.read_at is None


async def test_a_discord_failure_is_recorded_rather_than_lost(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 58. Detected, retried safely, recorded, notification preserved."""
    _raise(monkeypatch, _http_error(500))
    service = NotificationService(build_registry(_Configured()))
    await _enable_discord(service, db)
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    row = await db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel == str(Channel.discord))
    )
    assert row is not None
    assert row.attempt_count == 1
    assert row.next_attempt_at is not None  # it will be tried again
    assert row.failure_reason


# ============================================ 8. the test message (§38, 39)


def test_the_verification_message_names_no_trade_or_price() -> None:
    """Sections 38, 39 and 54."""
    embed = verification_embed()
    text = json.dumps(embed).lower()
    assert "test" in text
    for word in ("eurusd", "profit", "p&l", "trade closed", "risk limit"):
        assert word not in text


async def test_the_test_message_creates_no_trade_and_publishes_no_event(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _capture(monkeypatch)
    result = await channel().send_test()
    assert result.delivered
    assert sent[0]["embeds"][0]["title"].startswith("🔧")
    assert list((await db.scalars(select(Notification))).all()) == []


async def test_a_test_send_on_an_unconfigured_deployment_is_skipped() -> None:
    result = await channel(webhook_url="").send_test()
    assert result.status is DeliveryStatus.skipped


# ================================================== 9. the API (§40, 50, 53)


@pytest.fixture
async def app(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    monkeypatch.setenv("DISCORD_ENABLED", "true")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    configured = Settings(_env_file=None)
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(configured, checks={}, engine=engine)
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _promote(app: FastAPI, email: str, role: Role) -> None:
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role.value
        await db.commit()


async def test_the_status_route_never_returns_the_webhook(client: AsyncClient) -> None:
    """Sections 40 and 50."""
    await client.post("/auth/register", json=ALICE)
    r = await client.get("/v1/notifications/discord")
    assert r.status_code == 200
    body = r.text
    assert WEBHOOK not in body and TOKEN not in body
    assert r.json()["state"] == ChannelState.configured
    assert r.json()["scope"] == "system"


async def test_the_channels_route_never_returns_the_webhook(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    body = (await client.get("/v1/notifications/channels")).text
    assert WEBHOOK not in body and TOKEN not in body


async def test_discord_status_needs_a_session(client: AsyncClient) -> None:
    assert (await client.get("/v1/notifications/discord")).status_code == 401


async def test_only_an_administrator_may_send_a_test(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 50. Frontend hiding is not the protection."""
    _capture(monkeypatch)
    await client.post("/auth/register", json=BOB)
    assert (
        await client.post("/v1/notifications/discord/test", headers=_csrf(client))
    ).status_code == 403

    await _promote(app, BOB["email"], Role.admin)
    r = await client.post("/v1/notifications/discord/test", headers=_csrf(client))
    assert r.status_code == 200
    assert r.json()["delivered"] is True
    assert "No trade" in r.json()["note"] or "no trade" in r.json()["note"]


async def test_a_test_send_is_audited_without_the_webhook(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 43 and L36 section 31 in advance."""
    _capture(monkeypatch)
    await client.post("/auth/register", json=BOB)
    await _promote(app, BOB["email"], Role.admin)
    await client.post("/v1/notifications/discord/test", headers=_csrf(client))
    async with app.state.session_factory() as db:
        from app.models.ops import AuditLog

        rows = list((await db.scalars(select(AuditLog))).all())
    entries = [r for r in rows if r.resource_type == "notification_channel"]
    assert entries and entries[0].resource_id == str(Channel.discord)
    assert WEBHOOK not in json.dumps(entries[0].details)


async def test_a_test_send_needs_csrf(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=BOB)
    await _promote(app, BOB["email"], Role.admin)
    r = await client.post("/v1/notifications/discord/test")
    assert r.status_code == 403


async def test_the_preference_grid_offers_discord(client: AsyncClient) -> None:
    """Section 11. Integrated with L34's preferences, not beside them."""
    await client.post("/auth/register", json=ALICE)
    grid = (await client.get("/v1/notifications/preferences")).json()["preferences"]
    discord = [p for p in grid if p["channel"] == str(Channel.discord)]
    assert len(discord) == len(Category)
    assert all(p["enabled"] is False for p in discord)


async def test_a_user_can_turn_discord_on_for_one_category(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.patch(
        "/v1/notifications/preferences",
        headers=_csrf(client),
        json={
            "updates": [
                {
                    "category": "RISK",
                    "channel": "DISCORD",
                    "enabled": True,
                    "min_severity": "WARNING",
                }
            ]
        },
    )
    assert r.status_code == 200
    saved = [
        p for p in r.json()["preferences"] if p["category"] == "RISK" and p["channel"] == "DISCORD"
    ][0]
    assert saved["enabled"] is True and saved["source"] == "user"


# =================================================== 10. nothing else changed


def test_the_in_app_and_email_channels_are_untouched_by_l35() -> None:
    registry = build_registry(_Configured())
    assert isinstance(registry.get(Channel.in_app), InAppChannel)
    assert registry.get(Channel.email).describe()["state"] == ChannelState.not_configured


def test_l35_added_no_delivery_status_and_no_channel() -> None:
    """Section 45. The schema L34 wrote already had room."""
    assert [str(c) for c in Channel] == ["IN_APP", "EMAIL", "DISCORD"]
    assert [str(s) for s in DeliveryStatus] == [
        "PENDING",
        "PROCESSING",
        "DELIVERED",
        "FAILED",
        "RETRYING",
        "SKIPPED",
    ]
