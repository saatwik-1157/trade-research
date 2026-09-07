"""Not saying the same thing twice, and not saying it fifty times a minute.

Sections 20, 21 and 31. Two mechanisms, deliberately separate, because they
answer two different questions.

**The event key answers "is this the same event?"** It is the hash of the
event's own id, and it is stored under `UNIQUE (user_id, dedup_key)`. Redis
pub/sub delivers at-least-once and a reconnecting subscriber gets a replay, so
the same TRADE_RECORDED arrives twice as a matter of routine. Section 31 asks
for one logical notification; the database is where that guarantee belongs,
because a check the worker has to remember is a check a worker eventually
forgets. `app.realtime.hub.SeenEvents` is the cheap in-process first line and
this is the durable one -- the hub's own docstring says a consumer that must
never act twice also checks its own state, and this is that state.

**The condition key answers "is this the same problem?"** It is the hash of
`(type, entity, severity)` and it carries no event id, so fifty workers
noticing one disconnected terminal produce fifty events that share it. A
notification whose condition key was seen within the cooldown is suppressed.
That is section 20's example, and it is the same reasoning
`app.monitoring.alerts.deduplicate` records for L29 -- restated rather than
imported because L29 keys on a monitoring fingerprint and L34 keys on an
event, and one function serving both would have to know about both.

**Severity is in the condition key, and that is section 21.** A WARNING that
becomes a CRITICAL is not the same problem still there; it is a problem that
got worse, and suppressing it would hide the transition an operator most needs
to see. So it hashes differently and is never suppressed. The cost is that a
condition flapping between two severities is said twice, which is the correct
trade: the alternative hides a state change.

**A discrete fact has no cooldown at all.** A trade closes once. There is
nothing to debounce, and a cooldown on it would silently drop the second of
two trades on the same symbol in the same ten minutes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from app.notifications.contract import Severity


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def event_key(event_id: str, *, user_id: str) -> str:
    """Section 31. One event, one notification per recipient.

    Scoped to the user as well as the event: one TRADE_RECORDED may legitimately
    become two notifications for two people, and a key that ignored the
    recipient would let the first one written silence the second.
    """
    if not event_id:
        raise ValueError("an event with no id cannot be deduplicated")
    return _hash("event", event_id, user_id)


def condition_key(
    event_type: str,
    *,
    user_id: str,
    entity_type: str | None,
    entity_id: str | None,
    severity: Severity,
) -> str:
    """Section 20. What makes two events the same *problem*.

    Deliberately excludes the event id and the timestamp -- those are what make
    them different moments. Deliberately includes the severity, for section 21.

    An entity that the event did not name falls back to the empty string rather
    than to something invented. Two unattributed events of one type at one
    severity then share a key, which is the right answer: with nothing to tell
    them apart, they are one condition as far as anyone can observe.
    """
    return _hash(
        "condition",
        event_type,
        user_id,
        entity_type or "",
        entity_id or "",
        str(severity),
    )


def suppressed(
    *,
    last_seen: datetime | None,
    now: datetime,
    cooldown_seconds: int | None,
) -> tuple[bool, str]:
    """Whether to suppress, and the sentence explaining it either way.

    The reason is returned rather than logged here so the caller can store it:
    a suppressed notification that leaves no trace is indistinguishable from an
    event that never arrived, which is the confusion the whole module exists to
    avoid.
    """
    if cooldown_seconds is None:
        return False, "no cooldown: this event type reports a discrete fact, not a condition"
    if last_seen is None:
        return False, "new: this condition was not open"
    elapsed = now - last_seen
    if elapsed >= timedelta(seconds=cooldown_seconds):
        return (
            False,
            f"still open after the {cooldown_seconds}s cooldown, so it is said again",
        )
    remaining = int((timedelta(seconds=cooldown_seconds) - elapsed).total_seconds())
    return (
        True,
        f"the same condition at the same severity was reported {int(elapsed.total_seconds())}s "
        f"ago; suppressed for another {remaining}s. A severity change is a different "
        "condition and is never suppressed.",
    )


__all__ = ["condition_key", "event_key", "suppressed"]
