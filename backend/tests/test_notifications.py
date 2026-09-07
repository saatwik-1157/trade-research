"""The notification engine (L34).

The ones that matter most:

  * `test_the_notification_package_cannot_reach_anything_that_trades` — §24, §56.
  * `test_a_redelivered_event_creates_one_notification` — §20, §31.
  * `test_a_severity_change_is_never_suppressed_by_the_cooldown` — §21.
  * `test_a_figure_the_event_did_not_carry_never_appears` — §22.
  * `test_an_unknown_order_state_is_not_reported_as_a_failure` — §44, §45.
  * `test_a_paper_trade_is_never_shown_without_its_label` — §23.
  * `test_critical_in_app_cannot_be_switched_off` — §16, §17.
  * `test_a_channel_that_raises_does_not_affect_the_others` — §26 of L35, §56.
  * `test_a_permanent_failure_is_not_retried_and_a_temporary_one_is` — §30.
  * `test_one_user_cannot_read_or_change_another_users_notifications` — §46, §55.
  * `test_no_route_can_create_a_notification` — §58.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import User
from app.core.events import Event, InMemoryEventBus
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.bots import Bot
from app.models.ops import Notification, NotificationDelivery, NotificationPreference
from app.notifications import catalogue as notif_catalogue
from app.notifications import dedup, templates
from app.notifications import preferences as prefs
from app.notifications.channels import build_registry
from app.notifications.channels.base import ChannelRegistry, UnavailableChannel
from app.notifications.channels.email import (
    EmailChannel,
    EmailError,
    NotificationResetDelivery,
    UnconfiguredEmailProvider,
    body_for,
)
from app.notifications.channels.inapp import InAppChannel
from app.notifications.contract import (
    Category,
    Channel,
    DeliveryResult,
    DeliveryStatus,
    Severity,
)
from app.notifications.service import MAX_ATTEMPTS, NotificationService, backoff_seconds
from app.notifications.worker import NotificationConsumer, NotificationDeliveryWorker
from app.realtime.catalogue import PRODUCED_NOW, EventType
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PACKAGE = Path(__file__).resolve().parents[1] / "app" / "notifications"
ROUTER = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "notifications.py"

NOW = datetime(2026, 9, 5, 12, 0, 0)
ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
BOB = {"email": "bob@tr-platform.io", "password": "another long passphrase"}
SECRET = "smtp-password-do-not-leak"


class _Settings:
    """The subset `build_registry` reads. No SMTP host: email NOT_CONFIGURED."""

    smtp_host = ""
    smtp_port = 587
    smtp_username = ""
    smtp_password = ""
    smtp_use_tls = True
    smtp_from = "notifications@localhost"
    email_notifications_enabled = True


# ================================================================== fixtures


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
        db.add(User(id="u2", email="c@d.io", password_hash="x", role="user"))
        db.add(User(id="u3", email="e@f.io", password_hash="x", role="admin"))
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
        db.add(
            BrokerAccount(id="b1", user_id="u1", name="demo", broker="fake", account_mode="demo")
        )
        db.add(Bot(id="bot1", user_id="u1", name="runner", mode="paper"))
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
async def db(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:
        yield session


@pytest.fixture
def service() -> NotificationService:
    return NotificationService(build_registry(_Settings()))


def trade_event(**payload: Any) -> Event:
    body = {
        "trade_id": "t-abcdef12",
        "environment": "paper",
        "symbol": "EURUSD",
        "side": "buy",
        "net_profit": "42.30",
        "r_multiple": "1.4",
    }
    body.update(payload)
    return Event(type="TRADE_RECORDED", payload=body, source="trade_journal", channel="account:p1")


# ============================================ 1. the package cannot trade (§24)


def _modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py"))


FORBIDDEN_IMPORTS = ("app.risk", "app.oms", "app.brokers", "app.sizing", "app.positions")
FORBIDDEN_CALLS = (
    "submit_order",
    "place_order",
    "cancel_order",
    "modify_position",
    "close_position",
    "set_risk_limit",
    "start_bot",
    "enable_live",
)


def test_the_notification_package_cannot_reach_anything_that_trades() -> None:
    """Sections 24 and 65. A property of the imports, not a promise about them."""
    offences: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(FORBIDDEN_IMPORTS):
                    offences.append(f"{path.name} imports {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(FORBIDDEN_IMPORTS):
                        offences.append(f"{path.name} imports {alias.name}")
    assert offences == []


def test_the_notification_package_names_no_trading_verb() -> None:
    called: set[str] = set()
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if isinstance(name, str):
                    called.add(name)
    assert called.isdisjoint(FORBIDDEN_CALLS)


def test_no_route_can_create_a_notification() -> None:
    """Section 58. There is no POST that takes a title and a body.

    A production UI cannot be made to show a fabricated trading alert, because
    the API has no way to write one: a notification exists only as a
    consequence of a domain event the platform itself published.
    """
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    # `isinstance(d.func, ast.Attribute)` rather than `getattr(..., "attr")`:
    # the getattr form reads the attribute twice and tells the type checker
    # nothing, so `d.func.attr` was an unchecked access on an `expr`. Matching
    # the node type narrows it and is what the comprehension actually means.
    posts = [
        d.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for d in node.decorator_list
        if isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr in ("post", "put")
    ]
    # Two write-shaped POSTs, and neither creates a notification: `read-all`
    # marks existing rows read, and `discord/test` (L35) sends a message that
    # says it is a test and stores nothing.
    assert posts == ["post", "post"]
    source = ROUTER.read_text(encoding="utf-8")
    assert "Notification(" not in source


# ================================================ 2. events to notifications


async def test_a_trade_event_becomes_one_notification_for_its_owner(
    db: AsyncSession, service: NotificationService
) -> None:
    outcome = await service.on_event(db, trade_event())
    await db.commit()
    assert outcome.handled and len(outcome.created) == 1
    row = await db.scalar(select(Notification))
    assert row is not None
    assert row.user_id == "u1"
    assert row.category == str(Category.trading)
    assert row.severity == str(Severity.info)
    assert row.entity_type == "trade" and row.entity_id == "t-abcdef12"
    assert row.event_id == outcome.event_id


async def test_an_event_addressed_to_nobody_creates_nothing(
    db: AsyncSession, service: NotificationService
) -> None:
    event = Event(type="TRADE_RECORDED", payload={"trade_id": "x"}, channel="account:missing")
    outcome = await service.on_event(db, event)
    assert outcome.created == [] and outcome.recipients == 0
    assert "resolves to nobody" in outcome.reason


async def test_a_bot_event_reaches_the_bots_owner(
    db: AsyncSession, service: NotificationService
) -> None:
    event = Event(
        type="BOT_ERROR",
        payload={"bot_id": "bot1", "bot_name": "runner", "reason": "the strategy raised"},
        channel="bot:bot1",
    )
    outcome = await service.on_event(db, event)
    await db.commit()
    assert len(outcome.created) == 1
    row = await db.scalar(select(Notification))
    assert row is not None and row.user_id == "u1"
    assert row.severity == str(Severity.error)
    assert row.category == str(Category.bots)


async def test_a_model_event_reaches_operators_not_plain_users(
    db: AsyncSession, service: NotificationService
) -> None:
    """Model lifecycle has no owner; it goes to whoever may manage models."""
    event = Event(
        type="MODEL_ACTIVATED",
        payload={"model_key": "edge", "model_version": "3"},
        channel="model:edge",
    )
    await service.on_event(db, event)
    await db.commit()
    users = {r.user_id for r in (await db.scalars(select(Notification))).all()}
    assert users == {"u1", "u3"}  # trader and admin; the plain user is not told


async def test_a_deliberately_silent_event_creates_nothing(
    db: AsyncSession, service: NotificationService
) -> None:
    """Section 43. Not every catalogued type is worth telling somebody about."""
    event = Event(type="PORTFOLIO_UPDATED", payload={"equity": "1"}, channel="account:p1")
    outcome = await service.on_event(db, event)
    assert outcome.created == []
    assert "produces no notification" in outcome.reason


async def test_an_uncatalogued_type_is_reported_not_guessed_at(
    db: AsyncSession, service: NotificationService
) -> None:
    event = Event(type="MADE_UP_EVENT", payload={}, channel="account:p1")
    outcome = await service.on_event(db, event)
    assert outcome.created == [] and outcome.error is None


def test_every_routing_rule_names_a_catalogued_event() -> None:
    """Section 6 and section 65: L34 invents no event."""
    assert set(notif_catalogue.RULES).issubset(set(EventType))
    assert set(notif_catalogue.NOT_NOTIFIED).issubset(set(EventType))
    # Between them the two tables account for every type in the catalogue, so a
    # type added later is a test failure rather than a silent omission.
    covered = set(notif_catalogue.RULES) | set(notif_catalogue.NOT_NOTIFIED)
    assert covered == set(EventType)


def test_the_contract_reports_which_events_can_actually_fire() -> None:
    """`producing_now` is read from the platform's own list, not a second one."""
    live = notif_catalogue.live_now()
    assert live == set(notif_catalogue.RULES) & PRODUCED_NOW
    assert EventType.TRADE_RECORDED in live
    # Nothing emits BOT_ERROR yet; the rule is a contract L22's emitter fills.
    assert EventType.BOT_ERROR not in live


# ==================================================== 3. idempotency (§20, 31)


async def test_a_redelivered_event_creates_one_notification(
    db: AsyncSession, service: NotificationService
) -> None:
    event = trade_event()
    first = await service.on_event(db, event)
    await db.commit()
    second = await service.on_event(db, event)
    await db.commit()
    assert len(first.created) == 1
    assert second.created == [] and second.duplicates == 1
    assert len(list((await db.scalars(select(Notification))).all())) == 1


async def test_the_unique_constraint_is_what_actually_holds(
    sessions: async_sessionmaker[AsyncSession], service: NotificationService
) -> None:
    """Two sessions racing on one replayed event. The database decides."""
    event = trade_event()
    async with sessions() as one:
        await service.on_event(one, event)
        await one.commit()
    async with sessions() as two:
        outcome = await service.on_event(two, event)
        await two.commit()
    assert outcome.duplicates == 1
    async with sessions() as db:
        assert len(list((await db.scalars(select(Notification))).all())) == 1


async def test_two_different_events_about_one_trade_are_two_notifications(
    db: AsyncSession, service: NotificationService
) -> None:
    """Deduplication is per EVENT, not per entity. A correction is news."""
    await service.on_event(db, trade_event())
    await service.on_event(db, Event(**{**trade_event().__dict__, "type": "TRADE_UPDATED"}))
    await db.commit()
    assert len(list((await db.scalars(select(Notification))).all())) == 2


# ====================================================== 4. cooldowns (§20, 21)


async def test_a_repeated_condition_is_suppressed_within_the_cooldown(
    db: AsyncSession, service: NotificationService
) -> None:
    """Section 20's fifty workers noticing one disconnected terminal."""
    created = 0
    for _ in range(5):
        event = Event(
            type="BROKER_DISCONNECTED",
            payload={"account_id": "b1", "reason": "terminal not responding"},
            channel="account:b1",
        )
        outcome = await service.on_event(db, event)
        await db.commit()
        created += len(outcome.created)
    assert created == 1


async def test_a_severity_change_is_never_suppressed_by_the_cooldown() -> None:
    """Section 21. The transition an operator most needs to see."""
    warning = dedup.condition_key(
        "PORTFOLIO_HEALTH_CHANGED",
        user_id="u1",
        entity_type="account",
        entity_id="p1",
        severity=Severity.warning,
    )
    error = dedup.condition_key(
        "PORTFOLIO_HEALTH_CHANGED",
        user_id="u1",
        entity_type="account",
        entity_id="p1",
        severity=Severity.error,
    )
    assert warning != error


def test_a_discrete_fact_has_no_cooldown_at_all() -> None:
    """A trade closes once; debouncing it would drop the second of two."""
    assert notif_catalogue.RULES[EventType.TRADE_RECORDED].cooldown_seconds is None
    muted, why = dedup.suppressed(last_seen=NOW, now=NOW, cooldown_seconds=None)
    assert muted is False and "discrete fact" in why


def test_a_condition_outside_the_cooldown_is_said_again() -> None:
    muted, why = dedup.suppressed(
        last_seen=NOW - timedelta(seconds=700), now=NOW, cooldown_seconds=600
    )
    assert muted is False and "said again" in why


# ================================================== 5. templates (§22, 23, 45)


def test_a_figure_the_event_did_not_carry_never_appears() -> None:
    """Section 22. The line is left out; it is not filled with a zero."""
    rule = notif_catalogue.RULES[EventType.TRADE_RECORDED]
    _, body, _ = templates.render(
        "TRADE_RECORDED",
        {"trade_id": "t1", "symbol": "EURUSD", "side": "buy"},
        rule=rule,
        environment="paper",
        severity=Severity.info,
    )
    assert "Result" not in body
    assert "0.00" not in body and "None" not in body


def test_a_recorded_figure_is_reported_exactly_as_recorded() -> None:
    rule = notif_catalogue.RULES[EventType.TRADE_RECORDED]
    _, body, _ = templates.render(
        "TRADE_RECORDED",
        {"trade_id": "t1", "symbol": "EURUSD", "net_profit": "42.30", "r_multiple": "1.4"},
        rule=rule,
        environment="paper",
        severity=Severity.info,
    )
    assert "42.30" in body and "1.4R" in body


def test_a_paper_trade_is_never_shown_without_its_label() -> None:
    """Section 23. Both directions: paper says paper, live says live."""
    rule = notif_catalogue.RULES[EventType.TRADE_RECORDED]
    for environment, label in (("paper", "[PAPER]"), ("live", "[LIVE]"), ("demo", "[DEMO]")):
        title, _, _ = templates.render(
            "TRADE_RECORDED",
            {"trade_id": "t1"},
            rule=rule,
            environment=environment,
            severity=Severity.info,
        )
        assert title.startswith(label)


def test_an_unresolvable_environment_says_unknown_rather_than_paper() -> None:
    """A live trade shown as paper is the mistake that costs money."""
    rule = notif_catalogue.RULES[EventType.TRADE_RECORDED]
    title, _, _ = templates.render(
        "TRADE_RECORDED", {"trade_id": "t1"}, rule=rule, environment="", severity=Severity.info
    )
    assert title.startswith("[UNKNOWN]")


def test_a_non_trading_notification_is_not_stamped_with_an_environment() -> None:
    """A model registration is not paper or live. Stamping it would be a lie."""
    rule = notif_catalogue.RULES[EventType.MODEL_ACTIVATED]
    title, _, _ = templates.render(
        "MODEL_ACTIVATED",
        {"model_key": "edge"},
        rule=rule,
        environment="",
        severity=Severity.success,
    )
    assert "[" not in title


def test_an_unknown_order_state_is_not_reported_as_a_failure() -> None:
    """Sections 44 and 45. The whole point of the type being separate."""
    rule = notif_catalogue.RULES[EventType.ORDER_UNKNOWN]
    title, body, _ = templates.render(
        "ORDER_UNKNOWN",
        {"order_id": "o-1", "symbol": "EURUSD"},
        rule=rule,
        environment="paper",
        severity=Severity.error,
    )
    assert "reconcil" in (title + body).lower()
    assert "failed" not in body.lower()
    assert "filled" not in body.lower()


def test_a_risk_alert_does_not_claim_trading_was_blocked_unless_it_was() -> None:
    """Section 41. Only the RiskEngine can make a claim about enforcement."""
    rule = notif_catalogue.RULES[EventType.RISK_ALERT]
    _, quiet, _ = templates.render(
        "RISK_ALERT",
        {"account_id": "p1", "reason": "daily loss within 10% of the limit"},
        rule=rule,
        environment="paper",
        severity=Severity.warning,
    )
    assert "blocked" not in quiet.lower()
    _, loud, _ = templates.render(
        "RISK_ALERT",
        {"account_id": "p1", "kill_switch": True, "reason": "daily loss limit reached"},
        rule=rule,
        environment="paper",
        severity=Severity.error,
    )
    assert "blocked" in loud.lower()


def test_a_template_reads_only_the_fields_it_is_allowed_to() -> None:
    """An event that grows a field cannot start emailing it."""
    context = templates.context_of(
        {"trade_id": "t1", "broker_password": SECRET, "session_token": "abc"}
    )
    assert context == {"trade_id": "t1"}


def test_a_review_notification_is_a_pointer_not_the_review() -> None:
    """Section 40. The narrative stays behind the platform's access control."""
    rule = notif_catalogue.RULES[EventType.TRADE_REVIEW_COMPLETED]
    _, body, _ = templates.render(
        "TRADE_REVIEW_COMPLETED",
        {"review_id": "r1", "trade_id": "t-abcdef12", "outcome": "LOSS", "confidence": "0.6"},
        rule=rule,
        environment="paper",
        severity=Severity.info,
    )
    assert "Open it in the platform" in body
    assert len(body) < 400


# ================================================== 6. severity mapping (§8)


def test_severity_is_read_from_the_event_where_the_event_carries_one() -> None:
    rule = notif_catalogue.RULES[EventType.MODEL_ALERT_CREATED]
    assert rule.severity_for({"severity": "critical"}) is Severity.critical
    assert rule.severity_for({"severity": "serious"}) is Severity.error
    assert rule.severity_for({"severity": "warning"}) is Severity.warning


def test_a_kill_switch_is_critical() -> None:
    rule = notif_catalogue.RULES[EventType.RISK_ALERT]
    assert rule.severity_for({"kill_switch": True}) is Severity.critical
    assert rule.severity_for({"outcome": "breached"}) is Severity.error
    assert rule.severity_for({"outcome": "approaching"}) is Severity.warning


def test_a_critical_notification_says_so_in_its_title() -> None:
    rule = notif_catalogue.RULES[EventType.RISK_ALERT]
    title, _, _ = templates.render(
        "RISK_ALERT",
        {"account_id": "p1", "kill_switch": True},
        rule=rule,
        environment="live",
        severity=Severity.critical,
    )
    assert title.startswith("[LIVE] Critical:")


# ================================================= 7. preferences (§15, 16, 17)


def test_critical_in_app_cannot_be_switched_off() -> None:
    """Section 16. Refused with a reason, never quietly clamped."""
    with pytest.raises(prefs.PreferenceError):
        prefs.validate(Category.risk, Channel.in_app, enabled=False, min_severity=Severity.info)


def test_an_error_reaches_the_notification_centre_whatever_is_stored() -> None:
    """Even a row written straight into the database cannot hide one."""
    stored = {(Category.risk, Channel.in_app): prefs.Setting(False, Severity.critical)}
    assert Channel.in_app in prefs.resolve(Category.risk, Severity.error, stored)


def test_a_user_may_quieten_informational_in_app_alerts() -> None:
    stored = {(Category.trading, Channel.in_app): prefs.Setting(True, Severity.warning)}
    assert prefs.resolve(Category.trading, Severity.info, stored) == []
    assert Channel.in_app in prefs.resolve(Category.trading, Severity.warning, stored)


def test_the_defaults_do_not_email_every_trade_and_do_email_a_breach() -> None:
    """Section 16: quiet by default, never silent about something critical."""
    assert prefs.resolve(Category.trading, Severity.info, {}) == [Channel.in_app]
    assert Channel.email in prefs.resolve(Category.risk, Severity.error, {})
    assert Channel.email in prefs.resolve(Category.trading, Severity.critical, {})


def test_discord_is_off_by_default_at_this_level() -> None:
    """Its adapter arrives at L35; defaulting it on would queue what cannot send."""
    for category in Category:
        assert Channel.discord not in prefs.resolve(category, Severity.critical, {})


def test_the_preference_grid_shows_defaults_not_an_empty_page() -> None:
    rows = prefs.as_rows({})
    assert len(rows) == len(Category) * len(Channel)
    assert all(r["source"] == "default" for r in rows)
    assert all(r["locked"] is (r["channel"] == str(Channel.in_app)) for r in rows)


async def test_a_stored_preference_changes_what_is_queued(
    db: AsyncSession, service: NotificationService
) -> None:
    await service.set_preference(
        db,
        "u1",
        category=Category.trading,
        channel=Channel.in_app,
        enabled=True,
        min_severity=Severity.error,
    )
    await db.commit()
    outcome = await service.on_event(db, trade_event())
    await db.commit()
    # The notification is still recorded -- it is the platform's own record --
    # but nothing is queued for delivery.
    assert len(outcome.created) == 1
    assert outcome.deliveries == 0


# ===================================================== 8. read state (§12, 14)


async def test_the_unread_count_tracks_reading(
    db: AsyncSession, service: NotificationService
) -> None:
    await service.on_event(db, trade_event())
    await service.on_event(db, trade_event(trade_id="t-2"))
    await db.commit()
    assert await service.unread_count(db, "u1") == 2
    rows = list((await db.scalars(select(Notification))).all())
    await service.mark_read(db, "u1", rows[0].id)
    await db.commit()
    assert await service.unread_count(db, "u1") == 1
    assert await service.mark_all_read(db, "u1") == 1
    await db.commit()
    assert await service.unread_count(db, "u1") == 0


async def test_marking_read_twice_does_not_move_the_timestamp(
    db: AsyncSession, service: NotificationService
) -> None:
    await service.on_event(db, trade_event())
    await db.commit()
    row = await db.scalar(select(Notification))
    assert row is not None
    marked = await service.mark_read(db, "u1", row.id)
    assert marked is not None
    first = marked.read_at
    await db.commit()
    marked_again = await service.mark_read(db, "u1", row.id)
    assert marked_again is not None
    again = marked_again.read_at
    assert first == again


async def test_read_at_and_status_never_disagree(
    db: AsyncSession, service: NotificationService
) -> None:
    await service.on_event(db, trade_event())
    await db.commit()
    row = await db.scalar(select(Notification))
    assert row is not None and row.status == "sent" and row.read_at is None
    await service.mark_read(db, "u1", row.id)
    await db.commit()
    await db.refresh(row)
    assert row.status == "read" and row.read_at is not None


# ================================================== 9. delivery (§26, 30, 31)


class _Recording:
    """A channel that records what it was given and answers as told."""

    def __init__(self, channel: Channel, result: DeliveryResult | Exception) -> None:
        self.channel = channel
        self.result = result
        self.seen: list[Any] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, envelope: Any) -> DeliveryResult:
        self.seen.append(envelope)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def describe(self) -> dict[str, Any]:
        return {"channel": str(self.channel), "state": "CONFIGURED", "available": True}


def _registry(*adapters: Any) -> ChannelRegistry:
    registry = ChannelRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


async def test_in_app_delivery_succeeds_without_a_realtime_bus(
    db: AsyncSession, service: NotificationService
) -> None:
    """Section 13. The record is the delivery; the frame is a nudge on top."""
    await service.on_event(db, trade_event())
    await db.commit()
    results = await service.deliver_pending(db)
    await db.commit()
    assert [r["status"] for r in results] == [str(DeliveryStatus.delivered)]
    row = await db.scalar(select(NotificationDelivery))
    assert row is not None and row.delivered_at is not None


async def test_a_channel_that_raises_does_not_affect_the_others(
    db: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Section 26 of L35, section 56 of L34. Two channels, one broken."""
    boom = _Recording(Channel.email, RuntimeError("provider exploded"))
    service = NotificationService(_registry(InAppChannel(None), boom))
    await service.set_preference(
        db,
        "u1",
        category=Category.trading,
        channel=Channel.email,
        enabled=True,
        min_severity=Severity.info,
    )
    await db.commit()
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    rows = {r.channel: r for r in (await db.scalars(select(NotificationDelivery))).all()}
    assert rows[str(Channel.in_app)].status == str(DeliveryStatus.delivered)
    assert rows[str(Channel.email)].status == str(DeliveryStatus.retrying)
    # And the notification itself is untouched by the channel that broke.
    row = await db.scalar(select(Notification))
    assert row is not None and row.title.startswith("[PAPER]")


async def test_a_permanent_failure_is_not_retried_and_a_temporary_one_is(
    db: AsyncSession,
) -> None:
    """Section 30. A refused password is not weather."""
    permanent = _Recording(
        Channel.email,
        DeliveryResult(DeliveryStatus.failed, "SMTP authentication refused", retryable=False),
    )
    service = NotificationService(_registry(InAppChannel(None), permanent))
    await service.set_preference(
        db,
        "u1",
        category=Category.trading,
        channel=Channel.email,
        enabled=True,
        min_severity=Severity.info,
    )
    await db.commit()
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    row = await db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel == str(Channel.email))
    )
    assert row is not None
    assert row.status == str(DeliveryStatus.failed)
    assert row.attempt_count == 1  # not retried at all
    assert row.next_attempt_at is None
    assert "not retried" in (row.failure_reason or "")


async def test_retries_are_bounded(db: AsyncSession) -> None:
    """Section 30. Three tries, then FAILED, never forever."""
    flaky = _Recording(
        Channel.email, DeliveryResult(DeliveryStatus.retrying, "timeout", retryable=True)
    )
    service = NotificationService(_registry(InAppChannel(None), flaky))
    await service.set_preference(
        db,
        "u1",
        category=Category.trading,
        channel=Channel.email,
        enabled=True,
        min_severity=Severity.info,
    )
    await db.commit()
    await service.on_event(db, trade_event())
    await db.commit()
    row = await db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel == str(Channel.email))
    )
    assert row is not None
    for _ in range(MAX_ATTEMPTS + 2):
        await service.attempt(db, row, now=datetime(2027, 1, 1))
        await db.commit()
    assert row.attempt_count == MAX_ATTEMPTS + 2
    assert row.status == str(DeliveryStatus.failed)


def test_the_backoff_grows_and_then_stops_growing() -> None:
    waits = [backoff_seconds(i) for i in range(6)]
    assert waits[0] < waits[1] < waits[2]
    assert waits[2] == waits[3] == waits[5]


async def test_one_notification_cannot_be_delivered_twice_on_one_channel(
    db: AsyncSession, service: NotificationService
) -> None:
    """Section 31, as a database guarantee rather than a worker's memory."""
    await service.on_event(db, trade_event())
    await db.commit()
    row = await db.scalar(select(Notification))
    assert row is not None
    db.add(
        NotificationDelivery(notification_id=row.id, channel=str(Channel.in_app), status="PENDING")
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_an_unconfigured_channel_is_skipped_not_failed(db: AsyncSession) -> None:
    """ "Nobody set this up" is a different fact from "the provider broke"."""
    service = NotificationService(
        _registry(
            InAppChannel(None),
            UnavailableChannel(Channel.email, "no SMTP host is configured"),
        )
    )
    await service.set_preference(
        db,
        "u1",
        category=Category.trading,
        channel=Channel.email,
        enabled=True,
        min_severity=Severity.info,
    )
    await db.commit()
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    row = await db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.channel == str(Channel.email))
    )
    assert row is not None and row.status == str(DeliveryStatus.skipped)


async def test_critical_deliveries_are_attempted_before_informational_ones(
    db: AsyncSession, service: NotificationService
) -> None:
    """Section 29, as an ORDER BY rather than a second queue system."""
    await service.on_event(db, trade_event())
    await service.on_event(
        db,
        Event(
            type="RISK_ALERT",
            payload={"account_id": "p1", "kill_switch": True},
            channel="account:p1",
        ),
    )
    await db.commit()
    order = [r["delivery_id"] for r in await service.deliver_pending(db)]
    rows = {r.id: r for r in (await db.scalars(select(NotificationDelivery))).all()}
    priorities = [rows[i].priority for i in order]
    assert priorities == sorted(priorities, reverse=True)


# ======================================================= 10. email (§25, 26)


async def test_an_unconfigured_email_provider_refuses_rather_than_pretending() -> None:
    provider = UnconfiguredEmailProvider()
    assert provider.configured is False
    with pytest.raises(EmailError):
        await provider.send(to="a@b.io", subject="x", text="y")


async def test_email_without_an_address_is_skipped_not_retried() -> None:
    channel = EmailChannel(UnconfiguredEmailProvider())
    envelope = _envelope(context={})
    result = await channel.send(envelope)
    assert result.status is DeliveryStatus.skipped
    assert result.retryable is False


def test_no_secret_reaches_the_channel_description() -> None:
    """Section 43 of L35, section 57 of L36: status only, never the value."""

    class _Configured:
        smtp_host = "mail.example.com"
        smtp_port = 587
        smtp_username = "notifier"
        smtp_password = SECRET
        smtp_use_tls = True
        smtp_from = "n@example.com"
        email_notifications_enabled = True

    described = str(build_registry(_Configured()).describe())
    assert SECRET not in described
    assert "authenticated" in described


def test_the_email_body_carries_no_link_when_none_is_configured() -> None:
    body = body_for(_envelope(context={}))
    assert "http" not in body


def test_the_email_body_carries_the_configured_link_and_no_token() -> None:
    body = body_for(_envelope(context={"link": "https://app.example.com/alerts?notification=n1"}))
    assert "https://app.example.com/alerts?notification=n1" in body
    assert "token" not in body.lower()


async def test_the_password_reset_port_is_finally_implemented() -> None:
    """L04 wrote the port and said L34 would fill it. It does."""
    from app.auth.reset import DeliveryUnavailable

    delivery = NotificationResetDelivery(UnconfiguredEmailProvider())
    with pytest.raises(DeliveryUnavailable):
        await delivery.send("a@b.io", "tok")


def _envelope(context: dict[str, Any]) -> Any:
    from app.notifications.contract import NotificationEnvelope

    return NotificationEnvelope(
        notification_id="n1",
        event_id="e1",
        event_type="TRADE_RECORDED",
        category=Category.trading,
        severity=Severity.info,
        environment="paper",
        title="[PAPER] Trade recorded",
        body="EURUSD was journalled.",
        user_id="u1",
        context=context,
    )


# ================================================ 11. the consumer and worker


async def test_the_consumer_turns_a_published_event_into_a_notification(
    sessions: async_sessionmaker[AsyncSession], service: NotificationService
) -> None:
    bus = InMemoryEventBus()
    consumer = NotificationConsumer(bus, sessions, service)
    await consumer.handle(trade_event())
    async with sessions() as db:
        assert await service.unread_count(db, "u1") == 1
    assert consumer.handled == 1 and consumer.created == 1


async def test_the_consumer_ignores_an_id_it_has_already_seen(
    sessions: async_sessionmaker[AsyncSession], service: NotificationService
) -> None:
    bus = InMemoryEventBus()
    consumer = NotificationConsumer(bus, sessions, service)
    event = trade_event()
    await consumer.handle(event)
    await consumer.handle(event)
    async with sessions() as db:
        assert await service.unread_count(db, "u1") == 1


async def test_a_consumer_failure_does_not_end_the_subscription(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class _Exploding(NotificationService):
        async def on_event(self, db: AsyncSession, event: Event) -> Any:
            raise RuntimeError("boom")

    consumer = NotificationConsumer(InMemoryEventBus(), sessions, _Exploding())
    await consumer.handle(trade_event())  # must not raise
    assert consumer.failures == 1
    # And the next event is still handled by a working service.
    ok = NotificationConsumer(InMemoryEventBus(), sessions, NotificationService())
    await ok.handle(trade_event())
    assert ok.handled == 1


async def test_the_delivery_worker_drains_the_queue(
    sessions: async_sessionmaker[AsyncSession], service: NotificationService
) -> None:
    async with sessions() as db:
        await service.on_event(db, trade_event())
        await db.commit()
    worker = NotificationDeliveryWorker(sessions, service, interval_seconds=0.01)
    await worker.tick()
    async with sessions() as db:
        row = await db.scalar(select(NotificationDelivery))
        assert row is not None and row.status == str(DeliveryStatus.delivered)
    assert worker.delivered == 1


# ============================================== 12. trading safety (§17, 56)


async def test_a_notification_failure_leaves_the_trade_record_untouched(
    db: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Section 56, the mandatory one.

    The journal row, the account and the bot are read back after a delivery
    that raised. Nothing in the notification path can reach them: the service
    holds no order manager, no risk engine and no adapter.
    """
    service = NotificationService(_registry(_Recording(Channel.in_app, RuntimeError("boom"))))
    account = await db.scalar(select(PaperAccount).where(PaperAccount.id == "p1"))
    assert account is not None
    before = account.balance
    await service.on_event(db, trade_event())
    await db.commit()
    await service.deliver_pending(db)
    await db.commit()
    account_after = await db.scalar(select(PaperAccount).where(PaperAccount.id == "p1"))
    assert account_after is not None
    after = account_after.balance
    assert before == after
    bot = await db.scalar(select(Bot).where(Bot.id == "bot1"))
    assert bot is not None and bot.is_enabled is False


async def test_a_preference_is_not_a_switch_on_any_safety_system(
    db: AsyncSession, service: NotificationService
) -> None:
    """Section 17, stated as what the table is read by.

    `notification_preferences` is queried in exactly one place in the whole
    application, and it is the notification service.
    """
    hits = []
    for path in (Path(__file__).resolve().parents[1] / "app").rglob("*.py"):
        if "NotificationPreference" in path.read_text(encoding="utf-8"):
            hits.append(path.relative_to(Path(__file__).resolve().parents[1]).as_posix())
    assert sorted(hits) == [
        "app/models/__init__.py",  # the table is registered
        "app/models/ops.py",  # the table is defined
        "app/notifications/service.py",  # the table is read, in one place
    ]
    await service.set_preference(
        db,
        "u1",
        category=Category.risk,
        channel=Channel.email,
        enabled=False,
        min_severity=Severity.critical,
    )
    await db.commit()
    stored = await db.scalar(select(NotificationPreference))
    assert stored is not None and stored.enabled is False


# ================================================= 13. the API (§33, 46, 55)


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _notify(app: FastAPI, email: str) -> str:
    """Give this user one real notification, through the real service."""
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        db.add(
            PaperAccount(
                id=f"acct-{user.id[:8]}",
                user_id=user.id,
                name="paper",
                currency="USD",
                starting_balance=Decimal("1000"),
                balance=Decimal("1000"),
                equity=Decimal("1000"),
            )
        )
        await db.commit()
        event = Event(
            type="TRADE_RECORDED",
            payload={"trade_id": "t1", "environment": "paper", "symbol": "EURUSD"},
            channel=f"account:acct-{user.id[:8]}",
        )
        outcome = await app.state.notifications.on_event(db, event)
        await db.commit()
        return outcome.created[0]


async def test_notifications_need_a_session(client: AsyncClient) -> None:
    assert (await client.get("/v1/notifications")).status_code == 401
    assert (await client.get("/v1/notifications/unread-count")).status_code == 401
    assert (await client.get("/v1/notifications/preferences")).status_code == 401


async def test_the_notifications_route_is_no_longer_a_501(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.get("/v1/notifications")
    assert r.status_code == 200
    assert r.json()["page"]["total"] == 0  # an empty list, not fabricated rows


async def test_a_user_sees_their_own_notification_and_the_unread_count(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _notify(app, ALICE["email"])
    listing = (await client.get("/v1/notifications")).json()
    assert listing["page"]["total"] == 1
    assert listing["items"][0]["title"].startswith("[PAPER]")
    assert (await client.get("/v1/notifications/unread-count")).json()["unread"] == 1


async def test_one_user_cannot_read_or_change_another_users_notifications(
    app: FastAPI, client: AsyncClient
) -> None:
    """Section 46 and section 55. A 404, not a 403: an id is not confirmed."""
    await client.post("/auth/register", json=ALICE)
    alice_notification = await _notify(app, ALICE["email"])
    await client.post("/auth/logout", headers=_csrf(client))
    client.cookies.clear()

    await client.post("/auth/register", json=BOB)
    assert (await client.get("/v1/notifications")).json()["page"]["total"] == 0
    assert (await client.get("/v1/notifications/unread-count")).json()["unread"] == 0
    r = await client.patch(f"/v1/notifications/{alice_notification}/read", headers=_csrf(client))
    assert r.status_code == 404
    async with app.state.session_factory() as db:
        row = await db.get(Notification, alice_notification)
        assert row is not None and row.read_at is None


async def test_mark_read_and_read_all_move_the_count(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    notification = await _notify(app, ALICE["email"])
    r = await client.patch(f"/v1/notifications/{notification}/read", headers=_csrf(client))
    assert r.status_code == 200 and r.json()["unread"] == 0
    r = await client.post("/v1/notifications/read-all", headers=_csrf(client))
    assert r.json()["marked_read"] == 0


async def test_preferences_round_trip_and_refuse_an_unsafe_setting(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    grid = (await client.get("/v1/notifications/preferences")).json()
    assert len(grid["preferences"]) == len(Category) * len(Channel)

    ok = await client.patch(
        "/v1/notifications/preferences",
        headers=_csrf(client),
        json={
            "updates": [
                {
                    "category": "TRADING",
                    "channel": "EMAIL",
                    "enabled": True,
                    "min_severity": "WARNING",
                }
            ]
        },
    )
    assert ok.status_code == 200
    saved = [
        p
        for p in ok.json()["preferences"]
        if p["category"] == "TRADING" and p["channel"] == "EMAIL"
    ][0]
    assert saved["source"] == "user" and saved["min_severity"] == "WARNING"

    refused = await client.patch(
        "/v1/notifications/preferences",
        headers=_csrf(client),
        json={
            "updates": [
                {
                    "category": "RISK",
                    "channel": "IN_APP",
                    "enabled": False,
                    "min_severity": "INFO",
                }
            ]
        },
    )
    assert refused.status_code == 422
    assert "cannot be switched off" in refused.json()["error"]["detail"]


async def test_an_invalid_filter_names_the_alternatives(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.get("/v1/notifications?category=NONSENSE")
    assert r.status_code == 422
    assert "TRADING" in r.json()["error"]["detail"]


async def test_the_contract_reports_channels_without_reporting_secrets(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    body = (await client.get("/v1/notifications/contract")).json()
    assert body["always_delivered_in_app"] == ["CRITICAL", "ERROR"]
    assert any(e["event_type"] == "TRADE_RECORDED" for e in body["events"])
    text = str(body)
    assert "password" not in text.lower()
    assert "DISCORD" in str(body["channel_status"])


async def test_channel_status_says_configured_without_saying_how(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    body = (await client.get("/v1/notifications/channels")).json()
    states = {c["channel"]: c["state"] for c in body["channels"]}
    assert states[str(Channel.in_app)] == "CONFIGURED"
    # DISABLED from L35: DISCORD_ENABLED defaults to false, which is a
    # different fact from "configured and broken" and from "never set up".
    assert states[str(Channel.discord)] in ("NOT_CONFIGURED", "DISABLED")


async def test_deliveries_are_scoped_to_your_own_notifications(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _notify(app, ALICE["email"])
    await client.post("/auth/logout", headers=_csrf(client))
    client.cookies.clear()
    await client.post("/auth/register", json=BOB)
    assert (await client.get("/v1/notifications/deliveries")).json()["page"]["total"] == 0


async def test_the_listing_is_paginated_with_the_same_ceiling_as_everything_else(
    client: AsyncClient,
) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.get("/v1/notifications?limit=10000")
    assert r.status_code == 422


# ================================================= 14. the realtime interface


def test_notification_created_is_now_produced() -> None:
    """L07 catalogued the type and the scope; L34 is what emits it."""
    assert EventType.NOTIFICATION_CREATED in PRODUCED_NOW


async def test_the_in_app_channel_publishes_on_the_users_own_channel() -> None:
    published: list[Event] = []

    class _Hub:
        async def publish(self, event: Event) -> None:
            published.append(event)

    channel = InAppChannel(_Hub())
    result = await channel.send(_envelope(context={}))
    assert result.delivered
    assert published[0].type == "NOTIFICATION_CREATED"
    assert published[0].channel == "user:u1"
    # The frame is a nudge, not the record: no body travels on it.
    assert "body" not in published[0].payload


async def test_a_publish_failure_does_not_fail_the_delivery() -> None:
    """Section 13. The row is durable; the socket is a convenience."""

    class _BrokenHub:
        async def publish(self, event: Event) -> None:
            raise RuntimeError("bus down")

    result = await InAppChannel(_BrokenHub()).send(_envelope(context={}))
    assert result.delivered
    assert "next read" in result.detail
