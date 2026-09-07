"""What one pass of an execution pipeline did. One vocabulary, every path.

Extracted from `app/paper/engine.py` at L20, where it worked and was reachable
only by the paper engine. `app/paper/engine.py` imports and re-exports it, so
every existing caller and test is unchanged.

**Why one vocabulary rather than two.** The brief asks for counters across the
whole pipeline (§35). A paper bot that reports `risk_vetoed` and an
orchestrator that reports `RISK_REJECTED` cannot be added together, so the
first dashboard built over them would silently under-count one of the two. The
enum is the thing that makes the counters poolable.

**Every refusal has its own value.** Collapsing them into "rejected" makes the
counters useless: a veto from risk, a refusal from sizing, a stale feed and a
disabled strategy are four different faults with four different fixes.

L20 adds the values an EXTERNALLY-ARRIVING signal can fail on, which a
strategy-generated one cannot: the payload was malformed, the strategy named
does not exist or is switched off, the signal arrived too late to act on, or
the source was not authorized. A bar produced by our own strategy engine has
none of those failure modes, which is why they were not here before.
"""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    """What one pass of the pipeline did. Every refusal has its own value."""

    # --- strategy-driven passes (L16) ------------------------------------
    warming_up = "warming_up"
    no_signal = "no_signal"
    hold = "hold"
    duplicate_signal = "duplicate_signal"
    market_data_stale = "market_data_stale"
    account_not_tradeable = "account_not_tradeable"
    kill_switch = "kill_switch"
    ai_rejected = "ai_rejected"
    risk_vetoed = "risk_vetoed"
    risk_halted = "risk_halted"
    sizing_refused = "sizing_refused"
    spec_incomplete = "spec_incomplete"
    execution_rejected = "execution_rejected"
    filled = "filled"
    position_closed = "position_closed"
    strategy_error = "strategy_error"

    # --- externally-arriving signals (L20) -------------------------------
    #: The payload could not be read as a signal at all.
    signal_invalid = "signal_invalid"
    #: It named a strategy this platform does not have.
    strategy_unknown = "strategy_unknown"
    #: L38. The platform is in safe mode: a startup or reconciliation
    #: check found a condition that must be resolved before new work
    #: starts. Its own outcome rather than `account_not_tradeable`,
    #: because the two need different actions -- one is an account
    #: setting and the other is an unreconciled venue.
    safe_mode = "safe_mode"
    #: The strategy exists and is switched off, or its bot is paused.
    strategy_disabled = "strategy_disabled"
    #: It arrived too late to act on. Age is measured against the signal's own
    #: timestamp, not against when we happened to read it.
    signal_stale = "signal_stale"
    #: The source could not be authenticated to the strength this mode needs.
    source_unauthorized = "source_unauthorized"
    #: No order manager is registered for the account, so there is no venue.
    no_venue = "no_venue"
    #: The venue's answer was unclear. Reconciled, never retried.
    execution_unknown = "execution_unknown"
    #: L45 C-1. The order could not be written down, so it was not sent. A
    #: fault on our side, not a refusal of the trade: the signal is PARKED
    #: rather than retired, so a later pass acts on it once the database is
    #: back. It is deliberately not `execution_unknown` -- nothing reached a
    #: venue, and confusing "never sent" with "sent, outcome unknown" is the
    #: one mistake that turns a safe retry into a second position.
    not_recorded = "not_recorded"
    #: The order was created and sent, and the venue has not filled it yet.
    order_submitted = "order_submitted"
    #: Partly filled. Not `filled`, and never counted as one.
    partially_filled = "partially_filled"


#: Outcomes that mean "no order was created". Used by the counters and by the
#: tests that assert a refusal really refused.
NO_ORDER = frozenset(
    {
        Outcome.warming_up,
        Outcome.no_signal,
        Outcome.hold,
        Outcome.duplicate_signal,
        Outcome.market_data_stale,
        Outcome.account_not_tradeable,
        Outcome.kill_switch,
        Outcome.ai_rejected,
        Outcome.risk_vetoed,
        Outcome.risk_halted,
        Outcome.sizing_refused,
        Outcome.spec_incomplete,
        Outcome.strategy_error,
        # L20's refusals. Each stops the pass before an order exists.
        Outcome.signal_invalid,
        Outcome.strategy_unknown,
        Outcome.strategy_disabled,
        Outcome.signal_stale,
        Outcome.source_unauthorized,
        Outcome.no_venue,
        # L38. Safe mode stops the pass at gate zero, before anything is sent.
        Outcome.safe_mode,
        # L45 C-1. The durable write failed, so the order was discarded and
        # nothing was transmitted. No order exists -- in memory, on disk or at
        # the venue.
        Outcome.not_recorded,
    }
)

#: Outcomes where an order EXISTS and its fate is not yet settled. Deliberately
#: separate from `NO_ORDER`: "nothing was sent" and "something was sent and we
#: do not know what happened" are the two facts an operator must never confuse,
#: because only the first is safe to retry.
ORDER_UNSETTLED = frozenset(
    {
        Outcome.order_submitted,
        Outcome.partially_filled,
        Outcome.execution_unknown,
    }
)

#: The one outcome that means an order exists and is completely filled.
ORDER_COMPLETE = frozenset({Outcome.filled})


def created_an_order(outcome: Outcome) -> bool:
    """True when something may exist at a venue for this pass.

    The inverse of `NO_ORDER` rather than a list of its own, so a value added
    to the enum without a decision about it counts as "an order may exist",
    which is the conservative reading.
    """
    return outcome not in NO_ORDER
