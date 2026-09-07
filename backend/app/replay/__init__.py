"""Market replay: a controllable walk over historical bars.

Reuses L08's provider and validator, L12's Strategy contract, L14's metrics and
cost model, and L07's event bus. What it adds is the clock, the session state
machine and an incremental execution path -- because a batch simulator cannot
be paused between bars.

The incremental path is the risk this package creates, and it is answered by a
test rather than a promise: `test_replay_matches_the_backtest_exactly` runs the
same strategy over the same bars through L14 and through here and asserts the
trade lists are identical.

**Replay holds no broker adapter and imports none.** Simulated execution is
structural, not configured: there is no setting that could point it at a venue,
because there is no venue reference to point.
"""

from app.replay.clock import (
    SPEEDS,
    IllegalTransition,
    ReplayClock,
    ReplayState,
    SpeedError,
    check_transition,
)
from app.replay.engine import EventKind, ReplayEngine, ReplayEvent, ReplayPortfolio, atr_for
from app.replay.service import ReplayBusy, ReplayService, Session, run_to_completion

__all__ = [
    "SPEEDS",
    "EventKind",
    "IllegalTransition",
    "ReplayBusy",
    "ReplayClock",
    "ReplayEngine",
    "ReplayEvent",
    "ReplayPortfolio",
    "ReplayService",
    "ReplayState",
    "Session",
    "SpeedError",
    "atr_for",
    "check_transition",
    "run_to_completion",
]
