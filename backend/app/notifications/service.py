"""The notification service: one domain event in, notifications out.

This is the whole of sections 3, 5 and 65 in one object. A trading service
publishes a domain event and stops thinking about it; this decides who is told,
whether they have already been told, what the message says, and which channels
it goes to. Nothing in `app/risk`, `app/oms`, `app/bots` or `app/brokers` calls
anything here, and nothing here calls anything there.

The order of work, and why it is that order:

    1. look up the routing rule        an uncatalogued or deliberately silent
                                       type stops here, having cost one dict read
    2. resolve the recipients          from the event's own channel, never its payload
    3. grade the severity              from the rule, or from the event where it
                                       carries one
    4. resolve the environment         from the payload, then the account; never guessed
    5. render the message              from recorded fields only
    6. deduplicate, per recipient      the same event twice is one notification
    7. check the cooldown              the same condition twice in a minute is one
    8. persist the notification        BEFORE anything is delivered (section 13)
    9. resolve preferences             per recipient, with a floor they cannot lower
   10. queue a delivery per channel    in-app now, the rest to the worker

**Nothing on this path can fail a trade.** `on_event` catches everything it
does not expect and returns a report saying what went wrong. It is called by
the consumer, which is a worker, which is not on any execution path -- and even
if it were, a raised exception could not travel back to a producer that has
already returned. Section 56 is tested by injecting a channel that raises and
asserting the trade, the journal row and the risk decision are untouched.

**Nothing here calculates a risk figure.** Section 19. The severity of a risk
event is read from the words the RiskEngine used; there is no threshold in this
package and no comparison against one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Role, User, utcnow
from app.core.events import Event
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.bots import Bot
from app.models.ops import Notification, NotificationDelivery, NotificationPreference
from app.notifications import dedup, templates
from app.notifications import preferences as prefs
from app.notifications.catalogue import Rule, rule_for
from app.notifications.channels.base import ChannelRegistry
from app.notifications.contract import (
    ENVIRONMENTS,
    SEVERITY_RANK,
    TERMINAL,
    Audience,
    Category,
    Channel,
    DeliveryResult,
    DeliveryStatus,
    NotificationEnvelope,
    Severity,
)

log = logging.getLogger("app.notifications")

#: How many people one event may notify. A platform notice addressed to
#: EVERYONE on a large deployment would otherwise write a row per user inside
#: one transaction. The cap is logged when it bites, so a truncated fan-out is
#: visible rather than silent.
MAX_RECIPIENTS = 500

#: Section 30. Three tries, then FAILED. Not "keep going until it works":
#: an unbounded retry against a permanently broken provider is a queue that
#: never drains, and every notification behind it is late.
MAX_ATTEMPTS = 3

#: Exponential, in seconds, indexed by attempts already made.
BACKOFF: tuple[int, ...] = (30, 120, 600)

#: How many deliveries one worker pass takes. Bounded for the reason every
#: sweep in this platform is bounded: an unbounded pass is a pass that can
#: hold a transaction open for an unbounded time.
MAX_PER_PASS = 50


def backoff_seconds(attempts: int) -> int:
    return BACKOFF[min(attempts, len(BACKOFF) - 1)]


@dataclass
class Outcome:
    """What one event produced. Returned, never raised."""

    event_id: str
    event_type: str
    handled: bool = False
    reason: str = ""
    created: list[str] = field(default_factory=list)
    duplicates: int = 0
    suppressed: int = 0
    recipients: int = 0
    deliveries: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "handled": self.handled,
            "reason": self.reason,
            "created": list(self.created),
            "duplicates": self.duplicates,
            "suppressed": self.suppressed,
            "recipients": self.recipients,
            "deliveries": self.deliveries,
            "error": self.error,
        }


@dataclass(frozen=True)
class Recipient:
    user_id: str
    email: str | None


class NotificationService:
    """Stateless apart from its channel registry. One per process."""

    def __init__(self, channels: ChannelRegistry | None = None, *, base_url: str = "") -> None:
        self.channels = channels or ChannelRegistry()
        self.base_url = base_url.rstrip("/")

    # ==================================================================== read

    async def unread_count(self, db: AsyncSession, user_id: str) -> int:
        """Section 14. One indexed count, never a fetch-and-len.

        `ix_notifications_user_unread` is `(user_id, read_at)`, so this is an
        index-only count however many thousand rows the user has.
        """
        return int(
            await db.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_id, Notification.read_at.is_(None)
                )
            )
            or 0
        )

    async def counts_by_severity(self, db: AsyncSession, user_id: str) -> dict[str, int]:
        rows = (
            await db.execute(
                select(Notification.severity, func.count(Notification.id))
                .where(Notification.user_id == user_id, Notification.read_at.is_(None))
                .group_by(Notification.severity)
            )
        ).all()
        return {str(name): int(count) for name, count in rows}

    async def mark_read(
        self, db: AsyncSession, user_id: str, notification_id: str
    ) -> Notification | None:
        """The one place `read_at` and `status` are both set.

        Two representations of one fact, kept in step by one function, because
        two writers eventually disagree and then neither can be trusted.
        Idempotent: marking an already-read notification read again does not
        move its timestamp, so "when did I read this" stays true.
        """
        row = await db.scalar(
            select(Notification).where(
                Notification.id == notification_id, Notification.user_id == user_id
            )
        )
        if row is None:
            return None
        if row.read_at is None:
            row.read_at = utcnow()
            row.status = "read"
        return row

    async def mark_all_read(self, db: AsyncSession, user_id: str) -> int:
        """Returns how many were actually unread. Section 47's bulk operation.

        A single UPDATE rather than a read-then-write loop: the loop would hold
        every row in memory to change one column on each.
        """
        now = utcnow()
        result = await db.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            .values(read_at=now, status="read")
        )
        # `execute()` is typed as returning `Result`, which has no
        # `rowcount`; an UPDATE always yields a `CursorResult`, which does.
        # The cast documents that rather than reaching past the type.
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    # =============================================================== consuming

    async def on_event(self, db: AsyncSession, event: Event) -> Outcome:
        """Turn one domain event into notifications. Never raises.

        The caller is a background consumer and the producer has long since
        returned, but the guarantee is stated here rather than assumed there:
        section 56 is that a notification failure cannot affect trading, and
        the cheapest way to keep it is for this function to have no way of
        propagating one.
        """
        outcome = Outcome(event_id=event.id, event_type=event.type)
        try:
            return await self._handle(db, event, outcome)
        except Exception as exc:  # noqa: BLE001 - reported on the outcome, never raised
            outcome.error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning(
                "an event could not be turned into notifications",
                extra={
                    "event": "notification_event_failed",
                    "event_type": event.type,
                    "event_id": event.id,
                    "correlation_id": event.correlation_id,
                },
            )
            return outcome

    async def _handle(self, db: AsyncSession, event: Event, outcome: Outcome) -> Outcome:
        rule = rule_for(event.type)
        if rule is None:
            outcome.reason = (
                f"{event.type} produces no notification. It is either not in the event "
                "catalogue or deliberately silent; see app/notifications/catalogue.py."
            )
            return outcome

        payload = dict(event.payload or {})
        severity = rule.severity_for(payload)
        environment = self._environment(rule, payload, event)
        title, body, context = templates.render(
            event.type, payload, rule=rule, environment=environment, severity=severity
        )
        entity_type = rule.entity_type
        entity_id = rule.entity_id_from(payload) or self._ref(event)

        recipients = await self._recipients(db, rule, event)
        outcome.recipients = len(recipients)
        if not recipients:
            outcome.reason = (
                f"{event.type} was published on {event.channel!r}, which resolves to "
                "nobody. Nothing is created; a notification addressed to no one is a row "
                "no one can read."
            )
            return outcome

        now = utcnow()
        for recipient in recipients:
            created = await self._for_recipient(
                db,
                recipient=recipient,
                event=event,
                rule=rule,
                severity=severity,
                environment=environment,
                title=title,
                body=body,
                context=context,
                entity_type=entity_type,
                entity_id=entity_id,
                now=now,
                outcome=outcome,
            )
            if created is not None:
                outcome.created.append(created.id)
        outcome.handled = True
        outcome.reason = outcome.reason or "handled"
        return outcome

    async def _for_recipient(
        self,
        db: AsyncSession,
        *,
        recipient: Recipient,
        event: Event,
        rule: Rule,
        severity: Severity,
        environment: str | None,
        title: str,
        body: str,
        context: dict[str, Any],
        entity_type: str | None,
        entity_id: str | None,
        now: datetime,
        outcome: Outcome,
    ) -> Notification | None:
        event_key = dedup.event_key(event.id, user_id=recipient.user_id)
        condition_key = dedup.condition_key(
            event.type,
            user_id=recipient.user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            severity=severity,
        )

        # Section 31. The cheap check first; the UNIQUE constraint below is the
        # one that actually holds under two workers reading the same replay.
        already = await db.scalar(
            select(Notification.id).where(
                Notification.user_id == recipient.user_id,
                Notification.dedup_key == event_key,
            )
        )
        if already is not None:
            outcome.duplicates += 1
            return None

        # Section 20 and 21.
        last_seen = await self._condition_last_seen(db, recipient.user_id, condition_key, rule, now)
        muted, why = dedup.suppressed(
            last_seen=last_seen, now=now, cooldown_seconds=rule.cooldown_seconds
        )
        if muted:
            outcome.suppressed += 1
            log.info(
                "notification suppressed by cooldown",
                extra={
                    "event": "notification_suppressed",
                    "event_type": event.type,
                    "reason": why,
                },
            )
            return None

        row = Notification(
            user_id=recipient.user_id,
            channel="inapp",
            event_type=event.type[:32],
            event_id=event.id[:64],
            category=str(rule.category),
            severity=str(severity),
            environment=environment,
            title=title[:200],
            body=body,
            payload=context,
            entity_type=entity_type,
            entity_id=(entity_id or None) and str(entity_id)[:64],
            status="sent",
            sent_at=now,
            dedup_key=event_key,
            condition_key=condition_key,
        )
        db.add(row)
        try:
            # Section 13: the record is durable BEFORE anything is delivered.
            # A browser that was offline for the frame reads it on reconnect,
            # which is the whole point of persisting first.
            await db.flush()
        except IntegrityError:
            # Two consumers raced on the same replayed event. The constraint
            # decided; this one rolls back its own row and reports a duplicate,
            # which is the correct answer rather than an error.
            await db.rollback()
            outcome.duplicates += 1
            return None

        wanted = prefs.resolve(
            rule.category,
            severity,
            await self._preferences(db, recipient.user_id),
            available=None,  # every channel gets a row; unavailable ones are SKIPPED
        )
        for channel in wanted:
            db.add(
                NotificationDelivery(
                    notification_id=row.id,
                    channel=str(channel),
                    status=str(DeliveryStatus.pending),
                    priority=SEVERITY_RANK[severity],
                    attempt_count=0,
                    next_attempt_at=now,
                )
            )
            outcome.deliveries += 1
        await db.flush()
        return row

    async def _condition_last_seen(
        self,
        db: AsyncSession,
        user_id: str,
        condition_key: str,
        rule: Rule,
        now: datetime,
    ) -> datetime | None:
        """When this exact condition was last reported to this user.

        Bounded by the cooldown window, so the index range scan is over minutes
        rather than the whole history. A type with no cooldown never asks.
        """
        if rule.cooldown_seconds is None:
            return None
        since = now - timedelta(seconds=rule.cooldown_seconds)
        return await db.scalar(
            select(func.max(Notification.created_at)).where(
                Notification.user_id == user_id,
                Notification.condition_key == condition_key,
                Notification.created_at >= since,
            )
        )

    # ============================================================== recipients

    async def _recipients(self, db: AsyncSession, rule: Rule, event: Event) -> list[Recipient]:
        """Who is told, resolved from the event's routing rather than its body.

        A payload that named a `user_id` would be a payload that could name
        somebody else's -- the same reasoning `app/realtime/channels.py` uses
        when it refuses to authorize from the subscribe frame.
        """
        if rule.audience is Audience.owner:
            owner = await self._owner_of(db, event)
            if owner is None:
                return []
            return await self._as_recipients(db, [owner])
        if rule.audience is Audience.operators:
            return await self._by_role(db, Role.trader)
        if rule.audience is Audience.admins:
            return await self._by_role(db, Role.admin)
        return await self._by_role(db, Role.user)

    async def _owner_of(self, db: AsyncSession, event: Event) -> str | None:
        """The user behind `account:{id}`, `bot:{id}` or `user:{id}`.

        Read from the database, never taken from the frame. `event.user_id` is
        used only as a last resort and only when the event carried no channel
        that resolves -- a publisher setting it is asserting something about
        its own event, which is different from a client asserting it.
        """
        scope, _, ref = (event.channel or "").partition(":")
        if scope == "user" and ref:
            return ref
        if scope == "account" and ref:
            owner = await db.scalar(
                select(PaperAccount.user_id).where(PaperAccount.id == ref)
            ) or await db.scalar(select(BrokerAccount.user_id).where(BrokerAccount.id == ref))
            return owner
        if scope == "bot" and ref:
            return await db.scalar(select(Bot.user_id).where(Bot.id == ref))
        return event.user_id

    async def _by_role(self, db: AsyncSession, need: Role) -> list[Recipient]:
        """Every active user at or above this role.

        Expressed as a role rather than a permission because the permission
        table maps one onto the other and the query has to be SQL. If
        `manage_ai_models` ever moves off TRADER, `app/auth/permissions.py`
        moves it and `min_role_for_permission` is what this should read --
        which is why the two callers name the role that permission sits on
        today rather than a literal.
        """
        ranks = {Role.user: 0, Role.trader: 1, Role.admin: 2}
        allowed = [str(r) for r, rank in ranks.items() if rank >= ranks[need]]
        rows = (
            await db.execute(
                select(User.id, User.email)
                .where(User.is_active.is_(True), User.role.in_(allowed))
                .order_by(User.created_at)
                .limit(MAX_RECIPIENTS + 1)
            )
        ).all()
        if len(rows) > MAX_RECIPIENTS:
            log.warning(
                "a broadcast notification was truncated",
                extra={"event": "notification_fanout_capped", "cap": MAX_RECIPIENTS},
            )
            rows = rows[:MAX_RECIPIENTS]
        return [Recipient(user_id=str(i), email=e) for i, e in rows]

    async def _as_recipients(self, db: AsyncSession, user_ids: list[str]) -> list[Recipient]:
        rows = (
            await db.execute(
                select(User.id, User.email).where(User.id.in_(user_ids), User.is_active.is_(True))
            )
        ).all()
        return [Recipient(user_id=str(i), email=e) for i, e in rows]

    # ============================================================= environment

    def _ref(self, event: Event) -> str | None:
        _, _, ref = (event.channel or "").partition(":")
        return ref or None

    def _environment(self, rule: Rule, payload: dict[str, Any], event: Event) -> str | None:
        """Section 23. From the event, or `unknown`. Never a default.

        A notification about something that is not a trading environment --
        a model registration, a platform notice -- carries None, because
        stamping it `[PAPER]` would say something untrue about where it
        applies. A TRADING-shaped one that arrives without an environment
        carries the literal `unknown`, because leaving it blank would let a
        reader supply whichever environment they were expecting.
        """
        stated = payload.get("environment") or payload.get("mode")
        if isinstance(stated, str) and stated.lower() in ENVIRONMENTS:
            return stated.lower()
        if rule.category not in templates.STAMPED:
            return None
        return "unknown"

    async def resolve_environment(self, db: AsyncSession, account_id: str) -> str | None:
        """The environment of an account, read from the row that defines it.

        `paper_accounts` are the simulator by construction; a broker account
        says `demo` or `live` and the platform believes it rather than
        inferring from configuration.
        """
        if await db.scalar(select(PaperAccount.id).where(PaperAccount.id == account_id)):
            return "paper"
        mode = await db.scalar(
            select(BrokerAccount.account_mode).where(BrokerAccount.id == account_id)
        )
        return str(mode) if mode else None

    # ============================================================= preferences

    async def _preferences(
        self, db: AsyncSession, user_id: str
    ) -> dict[tuple[Category, Channel], prefs.Setting]:
        rows = (
            await db.scalars(
                select(NotificationPreference).where(NotificationPreference.user_id == user_id)
            )
        ).all()
        out: dict[tuple[Category, Channel], prefs.Setting] = {}
        for row in rows:
            try:
                key = (Category(row.category), Channel(row.channel))
            except ValueError:  # pragma: no cover - the CHECK constraint prevents it
                continue
            out[key] = prefs.Setting(bool(row.enabled), Severity(row.min_severity))
        return out

    async def preferences_for(self, db: AsyncSession, user_id: str) -> list[dict[str, object]]:
        return prefs.as_rows(await self._preferences(db, user_id))

    async def set_preference(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        category: Category,
        channel: Channel,
        enabled: bool,
        min_severity: Severity,
    ) -> prefs.Setting:
        """Store one pair, after the floor has had its say.

        `validate` raises rather than clamping, so a user who tries to silence
        critical in-app alerts is told they cannot instead of believing they
        have.
        """
        setting = prefs.validate(category, channel, enabled=enabled, min_severity=min_severity)
        row = await db.scalar(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.category == str(category),
                NotificationPreference.channel == str(channel),
            )
        )
        if row is None:
            row = NotificationPreference(
                user_id=user_id, category=str(category), channel=str(channel)
            )
            db.add(row)
        row.enabled = setting.enabled
        row.min_severity = str(setting.min_severity)
        await db.flush()
        return setting

    # ================================================================ delivery

    async def deliver_pending(
        self, db: AsyncSession, *, limit: int = MAX_PER_PASS, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """One worker pass. Section 28, 29 and 30.

        Ordered by priority descending then by age, which is section 29's
        priority queue expressed as an ORDER BY rather than as a second queue
        system. `ix_notification_deliveries_queue` is the index it reads.
        """
        now = now or utcnow()
        due = (
            select(NotificationDelivery)
            .where(
                NotificationDelivery.status.in_(
                    [str(DeliveryStatus.pending), str(DeliveryStatus.retrying)]
                ),
                (NotificationDelivery.next_attempt_at.is_(None))
                | (NotificationDelivery.next_attempt_at <= now),
            )
            .order_by(
                NotificationDelivery.priority.desc(),
                NotificationDelivery.created_at.asc(),
            )
            .limit(limit)
        )
        out: list[dict[str, Any]] = []
        for delivery in (await db.scalars(due)).all():
            out.append(await self.attempt(db, delivery, now=now))
        return out

    async def attempt(
        self, db: AsyncSession, delivery: NotificationDelivery, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """One delivery attempt. Isolated: one channel's failure is its own.

        Section 26 of L35 says it plainly and it is true from L34: IN_APP
        succeeding and EMAIL failing is a notification that was delivered on
        one channel and not the other. Neither row knows about the other, so
        neither can affect it.
        """
        now = now or utcnow()
        notification = await db.get(Notification, delivery.notification_id)
        if notification is None:  # pragma: no cover - FK cascade prevents it
            delivery.status = str(DeliveryStatus.failed)
            delivery.failure_reason = "the notification it belongs to is gone"
            return {"delivery_id": delivery.id, "status": delivery.status}

        try:
            channel = Channel(delivery.channel)
        except ValueError:  # pragma: no cover - the CHECK constraint prevents it
            delivery.status = str(DeliveryStatus.failed)
            delivery.failure_reason = f"unknown channel {delivery.channel!r}"
            return {"delivery_id": delivery.id, "status": delivery.status}

        adapter = self.channels.get(channel)
        envelope = await self._envelope(db, notification, channel)
        delivery.attempt_count += 1
        delivery.last_attempt_at = now
        started = utcnow()
        try:
            result = await adapter.send(envelope)
        except Exception as exc:  # noqa: BLE001 - an adapter's crash is a retryable failure
            result = DeliveryResult(
                status=DeliveryStatus.retrying,
                detail=f"{type(exc).__name__}: {exc}"[:300],
                retryable=True,
            )
            log.warning(
                "a notification channel raised",
                extra={
                    "event": "notification_channel_error",
                    "channel": str(channel),
                    "notification_id": notification.id,
                },
            )
        delivery.duration_ms = (utcnow() - started).total_seconds() * 1000
        self._record(delivery, result, now=now)
        await db.flush()
        return {
            "delivery_id": delivery.id,
            "channel": str(channel),
            "status": delivery.status,
            "attempt": delivery.attempt_count,
            "detail": delivery.failure_reason,
        }

    def _record(
        self, delivery: NotificationDelivery, result: DeliveryResult, *, now: datetime
    ) -> None:
        """Write the outcome, and decide whether there will be another try.

        The bound is on ATTEMPTS, not on elapsed time: section 30 asks for
        bounded retries, and a time bound would keep retrying a permanently
        misconfigured provider for as long as the window lasted.
        """
        if result.delivered:
            delivery.status = str(DeliveryStatus.delivered)
            delivery.delivered_at = now
            delivery.failure_reason = None
            delivery.provider_message_id = result.provider_message_id
            delivery.next_attempt_at = None
            return
        if result.status is DeliveryStatus.skipped:
            delivery.status = str(DeliveryStatus.skipped)
            delivery.failure_reason = result.detail[:500]
            delivery.next_attempt_at = None
            return
        delivery.failure_reason = result.detail[:500]
        if not result.retryable or delivery.attempt_count >= MAX_ATTEMPTS:
            delivery.status = str(DeliveryStatus.failed)
            delivery.next_attempt_at = None
            if not result.retryable:
                delivery.failure_reason = (
                    f"{result.detail} (not retried: this is a configuration or "
                    "addressing error, not a temporary one)"
                )[:500]
            return
        delivery.status = str(DeliveryStatus.retrying)
        wait = result.retry_after_seconds or backoff_seconds(delivery.attempt_count)
        delivery.next_attempt_at = now + timedelta(seconds=wait)

    async def _envelope(
        self, db: AsyncSession, row: Notification, channel: Channel
    ) -> NotificationEnvelope:
        """The envelope an adapter is given.

        The recipient's email address is looked up HERE, not by the adapter, so
        no adapter needs a session. The link is built from the configured base
        URL and carries no token -- section 31 of L35.
        """
        context = dict(row.payload or {})
        if channel is Channel.email and row.user_id:
            context["recipient_email"] = await db.scalar(
                select(User.email).where(User.id == row.user_id)
            )
        if self.base_url and row.entity_type and row.entity_id:
            context["link"] = f"{self.base_url}/alerts?notification={row.id}"
        return NotificationEnvelope(
            notification_id=row.id,
            event_id=row.event_id or "",
            event_type=row.event_type,
            category=Category(row.category) if row.category else Category.system,
            severity=Severity(row.severity),
            environment=row.environment or "",
            title=row.title,
            body=row.body or "",
            user_id=row.user_id or "",
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            created_at=row.created_at,
            context=context,
        )

    # =========================================================== observability

    async def stats(self, db: AsyncSession) -> dict[str, Any]:
        """Section 51. Counts, for the health surface and L37 to read.

        Counts only. No payload, no address, no title -- an observability
        endpoint that quoted a notification body would be a way to read
        somebody else's notifications through the metrics door.
        """
        by_status = {
            str(name): int(count)
            for name, count in (
                await db.execute(
                    select(
                        NotificationDelivery.status, func.count(NotificationDelivery.id)
                    ).group_by(NotificationDelivery.status)
                )
            ).all()
        }
        by_channel = {
            f"{channel}:{status}": int(count)
            for channel, status, count in (
                await db.execute(
                    select(
                        NotificationDelivery.channel,
                        NotificationDelivery.status,
                        func.count(NotificationDelivery.id),
                    ).group_by(NotificationDelivery.channel, NotificationDelivery.status)
                )
            ).all()
        }
        queued = sum(
            count for status, count in by_status.items() if status not in {str(s) for s in TERMINAL}
        )
        return {
            "notifications_created": int(await db.scalar(select(func.count(Notification.id))) or 0),
            "deliveries_by_status": by_status,
            "deliveries_by_channel": by_channel,
            "queue_depth": queued,
            "channels": self.channels.describe(),
            "max_attempts": MAX_ATTEMPTS,
            "backoff_seconds": list(BACKOFF),
        }


__all__ = [
    "BACKOFF",
    "MAX_ATTEMPTS",
    "MAX_PER_PASS",
    "MAX_RECIPIENTS",
    "NotificationService",
    "Outcome",
    "Recipient",
    "backoff_seconds",
]
