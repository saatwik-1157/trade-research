"""The journal's vocabulary: what a trade IS, and what state it is in.

Section 4 is the whole of this module:

    ORDER      an instruction to buy or sell
    FILL       what the venue actually executed
    POSITION   what is currently held
    TRADE      the completed episode, entry through exit

They are four different things and this platform already has three of them in
three tables. A trade is the fourth, and **it is one row per POSITION EPISODE** —
a position that filled in two parts and closed in three is ONE trade, not two
and not three, and the six events are its timeline.

**The exit vocabulary is L21's, not a second one.** Section 17 says to use the
existing Position Management reason system where available, and it is available:
`app.positions.policies.ExitReason` has eight members and an evaluation order
that L21 reasoned about. Inventing `STRATEGY_EXIT` here beside L21's
`strategy_exit` would give the platform two spellings of one fact, and the one
nobody looked at would be the one that got written.

The brief lists four reasons L21 does not have — `manual_exit`, `broker_exit`,
`emergency_exit` is shared, and `unknown`. Two of those are real and are added
as journal-only values with the reason recorded; `emergency_exit` already
exists; and the rest map one to one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.models.execution import TRADE_STATUSES
from app.positions.policies import ExitReason


class TradeStatus(StrEnum):
    """Section 15, and only the states a trade can actually be in.

    `CANCELLED` and `REJECTED` from the brief's list are deliberately absent.
    A journal row is written when a POSITION closes, and a cancelled or rejected
    order never opened one — `orders.status` already carries both words, and
    recording a rejection as a trade is exactly the confusion §25 warns against.
    """

    open = "open"
    partially_closed = "partially_closed"
    closed = "closed"
    #: Section 28. The platform says one thing and the venue says another, and
    #: nothing here resolves that by choosing. It is NOT `closed`: §50 is
    #: explicit that a trade must not be finalised until closure is confirmed.
    reconciliation_required = "reconciliation_required"
    #: We do not know. A close whose outcome the venue never confirmed.
    unknown = "unknown"


#: The enum and the CHECK constraint must not drift apart, so the module that
#: owns the column is the one that defines the tuple and this asserts on import.
assert tuple(str(s) for s in TradeStatus) == TRADE_STATUSES, (
    "TradeStatus and TRADE_STATUSES disagree; the CHECK constraint would reject "
    "a state the code can produce"
)

#: States in which the episode is over and the figures are final. Section 32:
#: a completed trade is a historical fact, and these are the rows that means.
FINAL: frozenset[TradeStatus] = frozenset({TradeStatus.closed})


class JournalExit(StrEnum):
    """Exit reasons the journal adds to L21's, and only where L21 has none.

    Each exists because it describes something L21's policy engine cannot
    decide: a person closed it, the venue closed it, or nobody recorded why.
    """

    #: A person closed it through the API. L21's policies do not produce this
    #: because a policy is a rule and this is its absence.
    manual_exit = "manual_exit"
    #: The venue closed it — a stop-out, a margin call, an expiry. The platform
    #: did not decide this and must not claim it did.
    broker_exit = "broker_exit"
    #: Nothing recorded a reason. NOT a default: it says the record is
    #: incomplete, which is a different claim from any of the others.
    unknown = "unknown"


#: Every exit reason a trade may carry: L21's eight plus the three above.
EXIT_REASONS: frozenset[str] = frozenset(
    [str(reason) for reason in ExitReason] + [str(reason) for reason in JournalExit]
)


def exit_reason_of(recorded: str | None) -> str:
    """One recorded reason, normalised — or `unknown`, said out loud.

    An unrecognised string becomes `unknown` rather than being passed through,
    because a vocabulary that accepts anything is not a vocabulary. It is never
    silently mapped onto the nearest member: guessing that an unknown reason was
    a stop loss would put a fabricated fact in a historical record.
    """
    if recorded and recorded in EXIT_REASONS:
        return recorded
    return str(JournalExit.unknown)


class Holding:
    """Section 20. Exact, from the timestamps, or nothing.

    Not a class with state — a namespace for the conversions, so the reporting
    layer picks a unit rather than this module choosing one for it. §20 says not
    to use approximate durations when exact timestamps exist, and the way to
    honour that is to keep the exact value and convert at the edge.
    """

    @staticmethod
    def between(opened_at: datetime | None, closed_at: datetime | None) -> timedelta | None:
        if opened_at is None or closed_at is None:
            return None
        return closed_at - opened_at

    @staticmethod
    def as_dict(opened_at: datetime | None, closed_at: datetime | None) -> dict[str, Any]:
        span = Holding.between(opened_at, closed_at)
        if span is None:
            return {
                "seconds": None,
                "minutes": None,
                "hours": None,
                "days": None,
                "note": "one of the timestamps is missing, so the duration is unknown",
            }
        seconds = span.total_seconds()
        return {
            # Exact. The derived units are conveniences and the seconds are the
            # measurement, which is why a negative one is reported rather than
            # clamped: see `quality.py`, where it is a data-quality finding.
            "seconds": seconds,
            "minutes": seconds / 60,
            "hours": seconds / 3600,
            "days": seconds / 86400,
            "note": "closed_at - opened_at, exactly. Never rounded or bucketed.",
        }


__all__ = [
    "EXIT_REASONS",
    "FINAL",
    "ExitReason",
    "Holding",
    "JournalExit",
    "TradeStatus",
    "exit_reason_of",
]
