"""Security events: what the platform says out loud when something is wrong.

Steps 26, 27, 28 and 41.

L34 defined `Category.security` -- *"authentication and privilege events"* --
and gave it **no rule**, because there was no event type to route. That seat is
what this module sits in. Two types are added to the L07 catalogue, and the
split between them is an authorization decision rather than a taxonomy:

    ACCOUNT_SECURITY_ALERT   Scope.user.   One person's own account: their
                             password changed, their sessions revoked, a
                             step-up they failed. It goes to them.
    SECURITY_ALERT           Scope.system. A platform-wide condition: a burst
                             of failed logins, a CSRF wall being hit, an
                             administrative action taken. It goes to admins.

**The system-scoped one carries no identifiers.** `catalogue.py` says a
`system` event *"never carries private figures"*, and a security alert is the
single easiest place to break that rule: the natural text is *"12 failed logins
for alice@example.com from 203.0.113.9"*, which publishes an email address and
an IP to every signed-in browser. So `SECURITY_ALERT` carries a **count and a
class** and never a subject. Whoever needs the subject reads the audit trail,
which is gated on `audit:read`.

**Nothing here logs a credential.** The brief lists what must never be logged --
passwords, API keys, tokens, broker credentials, webhook secrets, session
tokens, authorization headers, private keys -- and this module cannot: the
payload builders below take a fixed set of fields, and `app/core/logging.py`'s
scrubber runs over everything besides. A failed password attempt records *that*
one happened, never what was tried. A near-miss password is still a password.

**Session identifiers are hashed before they are recorded, never printed.**
A session id in a security event is a session token in a message bus, and the
bus fans out to browsers.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


class SecurityEvent(StrEnum):
    """What happened. The vocabulary the audit trail and the alerts share."""

    # Authentication (step 26).
    login_failed = "LOGIN_FAILED"
    login_rate_limited = "LOGIN_RATE_LIMITED"
    password_changed = "PASSWORD_CHANGED"
    password_reset_requested = "PASSWORD_RESET_REQUESTED"
    sessions_revoked = "SESSIONS_REVOKED"

    # Authorization (step 27).
    permission_denied = "PERMISSION_DENIED"
    csrf_failed = "CSRF_FAILED"
    rate_limited = "RATE_LIMITED"

    # Step-up (this level).
    step_up_granted = "STEP_UP_GRANTED"
    step_up_failed = "STEP_UP_FAILED"
    step_up_missing = "STEP_UP_MISSING"

    # Trading-adjacent (step 41). These are the ones that matter most here:
    # a trading platform's security incidents are mostly ordinary actions
    # taken by the wrong person.
    admin_action = "ADMIN_ACTION"
    dangerous_action = "DANGEROUS_ACTION"
    webhook_rejected = "WEBHOOK_REJECTED"
    kill_switch_engaged = "KILL_SWITCH_ENGAGED"
    safe_mode_engaged = "SAFE_MODE_ENGAGED"


class SecuritySeverity(StrEnum):
    """Deliberately L34's words, not a sixth vocabulary.

    `app/notifications/contract.py` already defines these five and the
    `notifications.severity` column already has a CHECK constraint listing
    them. Spelling a security-specific set here would put a second vocabulary
    in one column, which is the defect L34 caught in itself.
    """

    info = "INFO"
    warning = "WARNING"
    error = "ERROR"
    critical = "CRITICAL"


#: How seriously each event is taken by default. A caller may raise a severity
#: for context -- ten failed logins is not one failed login -- but never lower
#: it, because a threshold that can be argued down is not a threshold.
DEFAULT_SEVERITY: dict[SecurityEvent, SecuritySeverity] = {
    SecurityEvent.login_failed: SecuritySeverity.info,
    SecurityEvent.login_rate_limited: SecuritySeverity.warning,
    SecurityEvent.password_changed: SecuritySeverity.warning,
    SecurityEvent.password_reset_requested: SecuritySeverity.info,
    SecurityEvent.sessions_revoked: SecuritySeverity.warning,
    SecurityEvent.permission_denied: SecuritySeverity.warning,
    SecurityEvent.csrf_failed: SecuritySeverity.warning,
    SecurityEvent.rate_limited: SecuritySeverity.info,
    SecurityEvent.step_up_granted: SecuritySeverity.info,
    SecurityEvent.step_up_failed: SecuritySeverity.warning,
    SecurityEvent.step_up_missing: SecuritySeverity.warning,
    SecurityEvent.admin_action: SecuritySeverity.warning,
    SecurityEvent.dangerous_action: SecuritySeverity.error,
    SecurityEvent.webhook_rejected: SecuritySeverity.warning,
    SecurityEvent.kill_switch_engaged: SecuritySeverity.critical,
    SecurityEvent.safe_mode_engaged: SecuritySeverity.critical,
}

#: Events that concern one account and are told to its owner rather than to
#: the operators. Everything else is a platform condition.
PERSONAL: frozenset[SecurityEvent] = frozenset(
    {
        SecurityEvent.password_changed,
        SecurityEvent.password_reset_requested,
        SecurityEvent.sessions_revoked,
        SecurityEvent.step_up_failed,
    }
)

#: The only keys a security payload may carry. An allow-list rather than a
#: deny-list, for the reason L34's `READABLE_KEYS` is one: a deny-list has to
#: predict the name of the field that leaks, and the leak is always the field
#: nobody thought of. Note what is absent: `email`, `password`, `token`,
#: `secret`, `authorization`, `session_id`, `ip`, `user_agent`.
PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "event",
        "severity",
        "at",
        "count",
        "window_seconds",
        "action",
        "resource",
        "scope",
        "outcome",
        "detail",
    }
)


class PayloadRefused(ValueError):
    """A key that is not on the allow-list tried to enter a security event."""


def fingerprint(value: str) -> str:
    """A stable, short, non-reversible handle for a session or a token.

    Twelve hex characters of SHA-256. Enough to correlate two records that
    concern the same session; not enough to be one. It is never published on a
    channel -- it exists so an operator reading two audit rows can tell whether
    they are the same session without the row containing the session.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class SecurityRecord:
    """One security event, ready to be logged, audited or published."""

    event: SecurityEvent
    severity: SecuritySeverity
    detail: str
    user_id: str | None = None
    at: datetime | None = None
    count: int | None = None
    window_seconds: int | None = None
    action: str | None = None
    resource: str | None = None
    scope: str | None = None
    outcome: str | None = None

    @property
    def personal(self) -> bool:
        return self.event in PERSONAL

    def payload(self, *, include_subject: bool) -> dict[str, Any]:
        """The publishable body.

        `include_subject` is False for anything going to a `system` channel.
        It is not a convenience: it is the rule that keeps an email address out
        of a message every signed-in browser receives.
        """
        body: dict[str, Any] = {
            "event": self.event.value,
            "severity": self.severity.value,
            "at": (self.at or datetime.now(UTC)).isoformat(timespec="seconds"),
            "detail": self.detail,
        }
        for name in ("count", "window_seconds", "action", "resource", "scope", "outcome"):
            value = getattr(self, name)
            if value is not None:
                body[name] = value
        if include_subject and self.user_id:
            # Only ever on a user-scoped channel, which `channels.py` already
            # authorizes to that user alone.
            body["user_id"] = self.user_id
        bad = set(body) - PAYLOAD_KEYS - {"user_id"}
        if bad:
            raise PayloadRefused(
                f"a security event may not carry {sorted(bad)}. The allow-list exists "
                "because a deny-list has to predict the name of the field that leaks."
            )
        return body


def record(
    event: SecurityEvent,
    detail: str,
    *,
    user_id: str | None = None,
    severity: SecuritySeverity | None = None,
    **fields: Any,
) -> SecurityRecord:
    """Build a record, defaulting the severity and refusing to lower it."""
    default = DEFAULT_SEVERITY[event]
    chosen = severity or default
    order = list(SecuritySeverity)
    if order.index(chosen) < order.index(default):
        # Raising is allowed -- context makes things worse. Lowering is not.
        chosen = default
    unknown = set(fields) - PAYLOAD_KEYS
    if unknown:
        raise PayloadRefused(f"a security event may not carry {sorted(unknown)}")
    return SecurityRecord(event=event, severity=chosen, detail=detail, user_id=user_id, **fields)


def log_record(rec: SecurityRecord) -> None:
    """Structured log line. Never the credential, never the session token."""
    log.log(
        logging.WARNING if rec.severity is not SecuritySeverity.info else logging.INFO,
        "security event",
        extra={
            "event": "security_event",
            "security_event": rec.event.value,
            "severity": rec.severity.value,
            "user_id": rec.user_id,
            "detail": rec.detail,
        },
    )


__all__ = [
    "DEFAULT_SEVERITY",
    "PAYLOAD_KEYS",
    "PERSONAL",
    "PayloadRefused",
    "SecurityEvent",
    "SecurityRecord",
    "SecuritySeverity",
    "fingerprint",
    "log_record",
    "record",
]
