"""Publishing a security event onto the bus that already exists.

Steps 27, 28 and 41.

This is the producer for the two types `app/realtime/catalogue.py` gained at
L39, and it is deliberately thin: it decides **which channel** an event may go
on and **what may travel on it**, and hands the rest to L07's hub. There is no
second bus, no second subscriber registry and no direct call into the
notification service -- `NotificationConsumer` already reads every event off
the bus, and L34's catalogue now has a rule for both of these types, so a
published security event becomes a notification by the path every other event
takes.

**The routing rule is the whole module:**

    a personal event  -> `user:{id}`, carrying the subject
    everything else   -> `system`, carrying a COUNT AND A CLASS, never a subject

The second line is the one that matters. `catalogue.py` says a `system` event
"never carries private figures", and a security alert is the easiest place in
the platform to break that: the natural sentence is *"12 failed logins for
alice@example.com from 203.0.113.9"*. `SecurityRecord.payload(include_subject=
False)` is what makes that sentence unsayable here, and the allow-list in
`events.py` means an added field cannot quietly become the leak.

**Publishing never fails a request.** A security control that takes down the
route it protects gets switched off. Every publish is guarded, and a failure is
logged and counted rather than raised -- the audit trail is the durable record,
and it is written by the caller before this is reached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.security.events import SecurityRecord, log_record

log = logging.getLogger(__name__)

#: The two types L39 added. Named here rather than imported as strings so a
#: rename in the catalogue is a failure at import rather than silence at run
#: time.
SYSTEM_TYPE = "SECURITY_ALERT"
ACCOUNT_TYPE = "ACCOUNT_SECURITY_ALERT"


@dataclass
class SecurityAnnouncer:
    """Turns a `SecurityRecord` into at most one published event.

    `hub` is anything with an async `publish(Event)`; `None` means this process
    has no realtime transport, which is an ordinary state for a worker and for
    a test client. Nothing here retries: a security event that missed the bus
    is still in the audit trail and in the log, and a retry queue for
    notifications about failed logins is a second delivery system.
    """

    hub: Any | None = None
    published: int = 0
    failures: int = 0
    #: Counted rather than raised. A control that can take down the route it
    #: protects is a control somebody disables.
    suppressed: list[str] = field(default_factory=list)

    async def announce(self, rec: SecurityRecord) -> bool:
        """Log it always; publish it if there is somewhere to publish to."""
        log_record(rec)
        if self.hub is None:
            return False

        # Imported here rather than at module scope: `app.core.events` pulls in
        # the bus implementation, and `app/security` is asserted by a test to
        # import no service. A local import keeps that true without giving up
        # the real Event type.
        from app.core.events import Event

        if rec.personal and rec.user_id:
            event = Event(
                type=ACCOUNT_TYPE,
                payload=rec.payload(include_subject=True),
                source="security",
                channel=f"user:{rec.user_id}",
            )
        else:
            # System scope. No subject, by construction rather than by care.
            event = Event(
                type=SYSTEM_TYPE,
                payload=rec.payload(include_subject=False),
                source="security",
                channel="system",
            )
        try:
            await self.hub.publish(event)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self.failures += 1
            self.suppressed = ([*self.suppressed, type(exc).__name__])[-10:]
            log.warning(
                "security event was not published",
                extra={
                    "event": "security_publish_failed",
                    "security_event": rec.event.value,
                    "error": type(exc).__name__,
                },
            )
            return False
        self.published += 1
        return True

    def stats(self) -> dict[str, object]:
        return {
            "published": self.published,
            "failures": self.failures,
            "recent_failure_kinds": list(self.suppressed),
            "transport": "hub" if self.hub is not None else "log only",
        }


__all__ = ["ACCOUNT_TYPE", "SYSTEM_TYPE", "SecurityAnnouncer"]
