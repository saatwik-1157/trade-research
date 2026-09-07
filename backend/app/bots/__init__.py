"""The bot manager (L22).

`state.py` is the nine-state lifecycle, shared by every runner --
`app/paper/service.py` imports it rather than keeping its own vocabulary.

`limits.py` is the per-bot restriction layer, and its one rule is that a bot
limit can only ever TIGHTEN an account's: `effective()` combines the two and
cannot produce a looser figure than either input.

`supervisor.py` watches runs. It measures heartbeats rather than trusting a
status column, marks a silent run crashed, and restarts one only when every
safety gate agrees -- refusing by default when no safety check is wired,
because the absence of a check is not evidence that recovery is safe.

Nothing in this package trades. It holds no adapter, creates no order, and
imports neither `app.oms` nor `app.brokers`; a test parses every module to
prove it. The trading path stays
bot -> strategy -> AI -> risk -> sizing -> OMS -> adapter.
"""

from app.bots.limits import (
    ALLOWED,
    BotCounters,
    BotLimits,
    LimitVerdict,
    check,
)
from app.bots.state import (
    ACTIVE,
    FINISHED,
    MAY_TRADE,
    RECOVERABLE,
    TRANSITIONS,
    BotState,
    IllegalBotTransition,
    check_transition,
    is_recoverable,
    may_trade,
)
from app.bots.supervisor import (
    DEFAULT_STALE_AFTER,
    BotSupervisor,
    Finding,
    RestartPlan,
    SweepReport,
)
from app.bots.worker import BotSupervisorWorker

__all__ = [
    "ACTIVE",
    "ALLOWED",
    "DEFAULT_STALE_AFTER",
    "FINISHED",
    "MAY_TRADE",
    "RECOVERABLE",
    "TRANSITIONS",
    "BotCounters",
    "BotLimits",
    "BotState",
    "BotSupervisor",
    "BotSupervisorWorker",
    "Finding",
    "IllegalBotTransition",
    "LimitVerdict",
    "RestartPlan",
    "SweepReport",
    "check",
    "check_transition",
    "is_recoverable",
    "may_trade",
]
