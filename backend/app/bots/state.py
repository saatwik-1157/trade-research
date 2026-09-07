"""The bot lifecycle: nine states, and the moves between them.

This is the vocabulary and the legal transitions, and nothing else — no
runner, no worker, no database, no clock. `app/paper/service.py` imports it,
so the paper runner and any future runner move through the same machine.

**Six states existed and are kept under their own names.** The brief names
`ERROR`; this project has always called it `crashed`, which is more specific
and is what every existing row says. Renaming it would rewrite history to
match a document. `halted` likewise is this project's word for "a kill switch
stopped it", which is a different fact from "it crashed" and from "somebody
stopped it".

**Three were added at L22, and one of them fixes a real defect.**

  * `paused` — `PaperService.pause_bot` set the in-memory status to `paused`
    and wrote **`stopping`** to `bot_runs`, because the table had no value for
    it. So a paused bot and a bot shutting down were the same row, and after a
    restart nothing could tell them apart. A supervisor reading that row would
    either resume a bot somebody had deliberately paused, or abandon one that
    was only mid-shutdown. Both are wrong, and only one of them is visible.
  * `recovering` — a supervisor is restarting it right now. Distinct from
    `crashed`, which means nobody is acting: only the first means an answer is
    coming, exactly as `reconciling` differs from `unknown` for a position.
  * `disabled` — prevented from running until somebody explicitly re-enables
    it. Distinct from `stopped`, which anyone may start again.

`created` is deliberately NOT a run state. A `bot_runs` row exists because a
run was attempted; a bot that has never run has no row, and inventing one to
represent "configured but never started" would make the run table lie about
how many times the bot has run.
"""

from __future__ import annotations

from enum import StrEnum


class BotState(StrEnum):
    """The `bot_runs.status` vocabulary, shared by every runner."""

    #: Dependencies are being checked. Nothing is processing signals yet.
    starting = "starting"
    #: Processing signals.
    running = "running"
    #: Configured and deliberately not trading. Existing positions are still
    #: managed; this stops NEW trades, nothing else.
    paused = "paused"
    #: Shutting down: the loop has been told to stop and has not finished.
    stopping = "stopping"
    #: Finished. Anyone may start it again.
    stopped = "stopped"
    #: It failed. Nobody is acting on it yet.
    crashed = "crashed"
    #: A supervisor is restarting it right now.
    recovering = "recovering"
    #: A kill switch or an emergency stop ended it.
    halted = "halted"
    #: Prevented from running until explicitly re-enabled.
    disabled = "disabled"


TRANSITIONS: dict[BotState, frozenset[BotState]] = {
    BotState.starting: frozenset(
        {BotState.running, BotState.crashed, BotState.stopped, BotState.halted}
    ),
    BotState.running: frozenset(
        {
            BotState.paused,
            BotState.stopping,
            BotState.stopped,
            BotState.crashed,
            BotState.halted,
        }
    ),
    BotState.paused: frozenset(
        {
            BotState.running,
            BotState.stopping,
            BotState.stopped,
            BotState.crashed,
            BotState.halted,
            # A paused bot may be taken out of service without resuming first.
            BotState.disabled,
        }
    ),
    BotState.stopping: frozenset({BotState.stopped, BotState.crashed, BotState.halted}),
    # Terminal for THIS run. A new run gets a new row rather than reviving one,
    # so a run's history is never rewritten.
    BotState.stopped: frozenset({BotState.disabled}),
    BotState.halted: frozenset({BotState.disabled}),
    # A crashed run may be recovered, abandoned, or taken out of service.
    BotState.crashed: frozenset({BotState.recovering, BotState.stopped, BotState.disabled}),
    # Recovery either works or does not. It never goes straight to `running`
    # without passing `starting`, because starting is where the dependency
    # checks live and a recovered bot must pass them like any other.
    BotState.recovering: frozenset(
        {BotState.starting, BotState.crashed, BotState.stopped, BotState.halted}
    ),
    #: Terminal until somebody re-enables the BOT, which starts a new run.
    BotState.disabled: frozenset(),
}

#: A run that is still going, in the sense that a supervisor should watch it.
ACTIVE = frozenset({BotState.starting, BotState.running, BotState.paused, BotState.stopping})

#: A run that is over. No heartbeat is expected and none is missed.
FINISHED = frozenset({BotState.stopped, BotState.crashed, BotState.halted, BotState.disabled})

#: States in which a bot may create NEW trades. Deliberately just one: pausing,
#: stopping and every failure state all mean "no new trades", and collapsing
#: that into "not stopped" would let a crashed bot keep trading.
MAY_TRADE = frozenset({BotState.running})

#: States a supervisor may attempt to recover from. `halted` is NOT one: a kill
#: switch is a decision somebody made, and recovering out of it automatically
#: would be a bot-level bypass of a global safety control.
RECOVERABLE = frozenset({BotState.crashed})


class IllegalBotTransition(Exception):
    """A move the bot lifecycle does not allow."""


def check_transition(current: BotState, wanted: BotState) -> None:
    if wanted not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(str(s) for s in TRANSITIONS[current])) or "nothing"
        raise IllegalBotTransition(
            f"a {current} bot cannot become {wanted}; it may become {allowed}"
        )


def may_trade(state: BotState) -> bool:
    """Whether a bot in this state may open a NEW trade.

    Existing positions are not this function's business: they stay under the
    position manager whatever the bot is doing, which is the whole point of
    §32.
    """
    return state in MAY_TRADE


def is_recoverable(state: BotState) -> bool:
    return state in RECOVERABLE
