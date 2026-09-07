"""notifications, notification_deliveries, notification_preferences,
audit_logs, system_events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import IdMixin, JSONType, TimestampMixin
from app.notifications.contract import (
    ENVIRONMENTS,
    Category,
    Channel,
    DeliveryStatus,
    Severity,
)

LEVEL_CHECK = "level IN ('debug','info','warning','error','critical')"


def _in(column: str, values: tuple[str, ...] | list[str]) -> str:
    return column + " IN ('" + "','".join(values) + "')"


#: Built from the L34 enums rather than typed out again, so a value the code
#: can produce and the schema rejects cannot exist -- the rule
#: `app/models/review.py` recorded for the review statuses.
SEVERITIES: tuple[str, ...] = tuple(str(s) for s in Severity)
CATEGORIES: tuple[str, ...] = tuple(str(c) for c in Category)
CHANNELS: tuple[str, ...] = tuple(str(c) for c in Channel)
DELIVERY_STATUSES: tuple[str, ...] = tuple(str(s) for s in DeliveryStatus)


class Notification(IdMixin, TimestampMixin, Base):
    """One thing one person is told about one event. Level 34, sections 9 and 31.

    **The record, not the delivery.** A notification is created once per
    recipient per event; how it reached them -- in-app, email, Discord -- is a
    row per channel in `notification_deliveries`. Before L34 this table
    conflated the two and carried a `channel`, which is why that column is
    still here: it is L05's, it is written `inapp` by the model-monitoring path
    that has used it since L29, and removing it would delete recorded history
    to tidy up a name.

    **`user_id` NULL means the row is a platform record with no recipient.**
    L29's `ModelMonitor._persist` writes those: the monitoring run's own note
    of what it raised. The API never serves them -- every read is scoped to the
    signed-in user -- so they cannot appear as somebody's notification, and
    they are not deleted because they are the only record that run left.

    **`read_at` is the fact; `status` is kept in step with it.** One function
    sets both (`app.notifications.service.mark_read`), because two writers to
    two representations of one fact eventually disagree.

    **Idempotency is the database's, not the worker's.** `UNIQUE (user_id,
    dedup_key)` is section 31: the same event redelivered resolves to the
    existing row rather than writing a second one. Both columns are NULL on the
    pre-L34 rows, and SQL treats NULLs as distinct, so those rows neither
    collide with each other nor with anything L34 writes -- the "NULLs are
    distinct" behaviour that trapped migration 0020 is what makes this safe.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("channel IN ('inapp','email','push','discord')", name="channel"),
        CheckConstraint("status IN ('pending','sent','failed','read')", name="status"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        CheckConstraint(
            "category IS NULL OR " + _in("category", CATEGORIES),
            name="category",
        ),
        # Section 23. NULL means "not about a trading environment" -- a model
        # registration is not paper or live, and stamping it either would say
        # something untrue. It is never a stand-in for an environment we failed
        # to resolve: that case stores the literal 'unknown'.
        CheckConstraint(
            "environment IS NULL OR " + _in("environment", ENVIRONMENTS),
            name="environment",
        ),
        UniqueConstraint("user_id", "dedup_key", name="one_notification_per_event_per_user"),
        # Section 14 and section 48. The unread count is
        # `WHERE user_id = ? AND read_at IS NULL`, and it runs on every page
        # load; this is the index that keeps it off a table scan.
        Index("ix_notifications_user_unread", "user_id", "read_at"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_condition", "user_id", "condition_key", "created_at"),
    )

    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: L05's column. See the class docstring: kept, still written by L29.
    channel: Mapped[str] = mapped_column(String(8), nullable=False, default="inapp")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default=str(Severity.info))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="pending", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------ L34
    #: The domain event this came from. Traceable back to the bus, and the
    #: basis of `dedup_key`.
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    environment: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    #: Section 35. What it is about, so the browser can resolve a route. The
    #: backend never stores a URL: a page rename would invalidate every row.
    entity_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: sha256(event id, user id). Section 31.
    dedup_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: sha256(type, entity, severity). Section 20's cooldown key. Severity is
    #: in it deliberately, so a WARNING becoming a CRITICAL is never suppressed.
    condition_key: Mapped[str | None] = mapped_column(String(32), nullable=True)

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "category": self.category,
            "severity": self.severity,
            "environment": self.environment,
            "title": self.title,
            "body": self.body,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "read": self.is_read,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": self.created_at.isoformat(),
            "context": self.payload or {},
        }


class NotificationDelivery(IdMixin, TimestampMixin, Base):
    """One attempt to put one notification on one channel. Sections 10 and 27.

    **One row per (notification, channel), enforced.** Section 31: the same
    notification cannot be emailed twice because a worker retried the wrong
    thing. The row is the attempt *record*; `attempt_count` is how many tries
    it took, not how many rows there are.

    **A failure here is a fact about the channel, never about the event.**
    Section 25 of L35 and section 56 of L34: an email provider being down does
    not make a trade unclosed. Nothing reads this table to decide anything
    except whether to try again.

    **`failure_reason` is a category and a message, never a payload.** The
    adapter's own words, truncated. No credential ever reaches it, because no
    adapter is given one to put there -- `NotificationEnvelope` carries no
    secret, and provider configuration is read from settings inside the
    adapter and never returned.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("status", DELIVERY_STATUSES), name="status"),
        CheckConstraint("attempt_count >= 0", name="attempts_are_not_negative"),
        UniqueConstraint("notification_id", "channel", name="one_delivery_per_channel"),
        # The worker's own query: what is still owed, worst first.
        Index("ix_notification_deliveries_queue", "status", "priority", "next_attempt_at"),
    )

    notification_id: Mapped[str] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=str(DeliveryStatus.pending), index=True
    )
    #: Section 29. The severity's rank, copied onto the delivery so the worker
    #: can order by it without joining. Denormalised deliberately and written
    #: once at creation: it describes the notification at the moment the
    #: delivery was queued, and a notification's severity never changes.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: When the worker may next try. Section 30's backoff, stored rather than
    #: computed, so a restarted worker honours the wait it already decided on.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "notification_id": self.notification_id,
            "channel": self.channel,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "next_attempt_at": self.next_attempt_at.isoformat() if self.next_attempt_at else None,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "failure_reason": self.failure_reason,
            "provider_message_id": self.provider_message_id,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat(),
        }


class NotificationPreference(IdMixin, TimestampMixin, Base):
    """What one user asked to be told, for one category on one channel.

    Sections 15 and 46. Rows exist only where a user has expressed an opinion;
    everything else falls back to `app.notifications.preferences.DEFAULTS`. A
    table pre-populated with every pair for every user would make "the default
    changed" unrepresentable -- the stored row would keep answering with the
    old default forever.

    **Section 17, in the schema's own terms.** Nothing in this table is read by
    the risk engine, the OMS, the position manager or a broker adapter. It
    decides whether somebody is told, and it decides nothing else.
    """

    __tablename__ = "notification_preferences"
    __table_args__ = (
        CheckConstraint(_in("category", CATEGORIES), name="category"),
        CheckConstraint(_in("channel", CHANNELS), name="channel"),
        CheckConstraint(_in("min_severity", SEVERITIES), name="min_severity"),
        # Section 16, in the schema. The in-app channel cannot be switched off,
        # so a row that says otherwise cannot be written -- not by the API, and
        # not by anything else with a connection.
        CheckConstraint("channel <> 'IN_APP' OR enabled", name="in_app_cannot_be_disabled"),
        UniqueConstraint("user_id", "category", "channel", name="one_preference_per_pair"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    min_severity: Mapped[str] = mapped_column(String(8), nullable=False, default=str(Severity.info))


class AuditLog(IdMixin, Base):
    """Who did what to which resource. Append-only by convention.

    **Append-only, and section 32 of L36 makes that a property rather than a
    habit.** No route in this application updates or deletes a row here: the
    only writer is `app.core.audit.record`, which inserts, and the only reader
    is `GET /v1/admin/audit-logs`. A test enumerates the application for a
    DELETE or an UPDATE against this table and asserts there is none, so an
    administrator cannot erase their own trail through the API they are
    administering.

    **`environment` was added at L36** as a column rather than a detail key,
    because section 35 asks for it as a FILTER and a JSON scan is not one. It
    is NULL for an action that is not about a trading environment -- a role
    change is neither paper nor live, and stamping it would say something
    untrue, the same rule `notifications.environment` follows.

    **`details` is scrubbed on the way in, never on the way out.** Passwords,
    hashes, session and reset tokens, API keys and broker credentials never
    enter the table at any depth, so nothing here needs filtering when it is
    read -- including the `before`/`after` state snapshots L36 writes.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "environment IS NULL OR " + _in("environment", ENVIRONMENTS),
            name="environment",
        ),
        # Section 35's filters, as one index: the trail is read newest-first,
        # optionally narrowed to one actor.
        Index("ix_audit_logs_actor_occurred", "actor_user_id", "occurred_at"),
        Index("ix_audit_logs_action_occurred", "action", "occurred_at"),
    )

    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    details: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


class SystemEvent(IdMixin, Base):
    """Operational events: connects, disconnects, reconciliations, halts."""

    __tablename__ = "system_events"
    __table_args__ = (CheckConstraint(LEVEL_CHECK, name="level"),)

    component: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


__all__ = [
    "CATEGORIES",
    "CHANNELS",
    "DELIVERY_STATUSES",
    "LEVEL_CHECK",
    "SEVERITIES",
    "AuditLog",
    "Notification",
    "NotificationDelivery",
    "NotificationPreference",
    "SystemEvent",
]
