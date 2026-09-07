"""The replay clock and the session state machine.

**Simulated time comes from the dataset, never from the wall clock.** The
current market time during replay is the timestamp of the bar being processed.
The machine's clock is used for one thing only -- deciding how long to sleep
between bars so playback is watchable -- and it can never influence a trading
decision.

That separation is why speed is provably free: `advance()` returns the next bar
regardless of speed, and speed only feeds `pacing_delay()`. A test runs the
same session at 1x and 100x and asserts the financial results are identical.

The state machine refuses illegal transitions rather than tolerating them. A
COMPLETED session cannot be resumed into RUNNING, because a session that could
would produce a second, different result under the same id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ReplayState(StrEnum):
    """Where a session is.

    Mapped onto the L05 `replay_sessions.status` vocabulary, which had
    queued/running/finished/failed/cancelled and gains `paused` by migration
    0007. `created` maps to `queued`, `completed` to `finished` and `stopped`
    to `cancelled`: the same states under the names the schema already uses,
    rather than a parallel vocabulary that would have to be translated at every
    boundary.
    """

    created = "queued"
    running = "running"
    paused = "paused"
    completed = "finished"
    stopped = "cancelled"
    failed = "failed"


# Legal moves. Anything absent is refused, and the refusal names what was
# attempted -- a state machine that silently ignored a bad transition would let
# a caller believe a session resumed when it did not.
TRANSITIONS: dict[ReplayState, frozenset[ReplayState]] = {
    ReplayState.created: frozenset({ReplayState.running, ReplayState.stopped, ReplayState.failed}),
    ReplayState.running: frozenset(
        {
            ReplayState.paused,
            ReplayState.completed,
            ReplayState.stopped,
            ReplayState.failed,
        }
    ),
    ReplayState.paused: frozenset({ReplayState.running, ReplayState.stopped, ReplayState.failed}),
    # Terminal. A completed session that could go back to running would produce
    # a second, different result under the same id.
    ReplayState.completed: frozenset(),
    ReplayState.stopped: frozenset(),
    ReplayState.failed: frozenset(),
}

TERMINAL = frozenset({ReplayState.completed, ReplayState.stopped, ReplayState.failed})


class IllegalTransition(Exception):
    """A move the state machine does not allow. Never silently ignored."""


def check_transition(current: ReplayState, wanted: ReplayState) -> None:
    if wanted not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(str(s) for s in TRANSITIONS[current])) or "nothing"
        raise IllegalTransition(
            f"a {current} session cannot become {wanted}; it may become {allowed}"
        )


# Playback speeds. Bounded because speed only paces the sleep, and a speed of
# zero would stall rather than pause -- pausing is a state, not a speed.
SPEEDS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 50.0, 100.0)
MAX_SPEED = 100.0
# The wall-clock seconds one bar takes at 1x. Small enough to watch, large
# enough that a step is visible.
BASE_DELAY_SECONDS = 0.5


class SpeedError(Exception):
    """An unsupported speed. Refused rather than clamped."""


def check_speed(speed: float) -> float:
    if speed <= 0:
        raise SpeedError(
            f"speed must be positive, not {speed}; pausing is a state, not a speed of zero"
        )
    if speed > MAX_SPEED:
        raise SpeedError(f"speed must be at most {MAX_SPEED}x, not {speed}")
    return float(speed)


@dataclass
class ReplayClock:
    """Simulated market time, driven by the dataset.

    `now()` is the timestamp of the bar last revealed. It is the only time a
    strategy sees during replay, which is what makes a replayed decision the
    decision that would have been made then.
    """

    timestamps: list[datetime]
    speed: float = 1.0
    cursor: int = -1  # -1 means nothing has been revealed yet
    _seq: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        check_speed(self.speed)
        if not self.timestamps:
            raise ValueError("a replay clock needs at least one timestamp")
        # Ascending order is asserted, never imposed silently: a dataset that
        # arrived out of order is a data problem, and reordering it here would
        # hide it from the validation that should have caught it.
        if any(b < a for a, b in zip(self.timestamps, self.timestamps[1:], strict=False)):
            raise ValueError("replay timestamps are not in ascending order")

    def now(self) -> datetime | None:
        """Simulated market time: the bar last revealed, or None before the start."""
        return self.timestamps[self.cursor] if self.cursor >= 0 else None

    @property
    def exhausted(self) -> bool:
        return self.cursor >= len(self.timestamps) - 1

    @property
    def revealed(self) -> int:
        return self.cursor + 1

    @property
    def total(self) -> int:
        return len(self.timestamps)

    @property
    def progress(self) -> float:
        return self.revealed / self.total if self.total else 1.0

    def advance(self) -> int | None:
        """Reveal exactly one more bar. Returns its index, or None at the end.

        Speed is not consulted here, and that is the whole reason a replay at
        1x and one at 100x produce the same trades: speed cannot skip, reorder
        or merge an event because it is not part of this function.
        """
        if self.exhausted:
            return None
        self.cursor += 1
        return self.cursor

    def next_sequence(self) -> int:
        """A monotonic sequence number for the event stream.

        Frontend synchronisation needs to order a market event, a signal, a
        risk decision and a fill that all share one simulated timestamp.
        """
        self._seq += 1
        return self._seq

    def pacing_delay(self) -> float:
        """Wall-clock seconds to wait before the next bar. Pacing only.

        The single place the machine's clock is allowed to matter, and it
        cannot reach a trading decision from here.
        """
        return BASE_DELAY_SECONDS / self.speed

    def set_speed(self, speed: float) -> float:
        self.speed = check_speed(speed)
        return self.speed

    def reset(self) -> None:
        self.cursor = -1
        self._seq = 0
