# AUTONOMOUS_ACTION_SAFETY_ENVELOPE.md

The bounds an autonomous action must sit inside. L62 §5, 2026-09-06.

Source: `backend/app/safety/envelope.py`.

---

## Why there is an envelope at all

L61 classified actions by **direction** — does this take permission away or
grant it — and that is what decides whether an action may ever be automatic.
It says nothing about **size**.

"Reduce the allocation" classifies identically whether it reduces by 2% or by
100%, and one of those is a reduction while the other is a liquidation nobody
sized. The envelope is that missing half.

It does not re-decide direction. `verification.py` runs the envelope *first*
and feeds the result in as the `within_bounds` that L61's `Action` had always
taken on trust from its caller.

## The bounds

| Bound | Default | What it limits |
|---|---|---|
| `max_allocation_change` | 0.10 | fraction of capital one action may move between strategies |
| `max_capital_movement` | 1000 | absolute capital one action may move |
| `max_exposure_change` | 0.10 | fraction of gross exposure one action may change |
| `max_actions_per_window` | 6 | actions per target per window |
| `action_window` | 1 hour | the window those are counted over |
| `min_cooldown` | 5 min | before the same target is touched again |
| `max_concurrent_actions` | 3 | in flight at once, across all targets |
| `max_chain_length` | 3 | actions following from one observation |
| `max_retries` | 1 | |
| `min_confidence` | 0.60 | before an advisory input may move anything |
| `max_data_age` | 5 min | how old the evidence may be |

**Every one of these is a configuration decision, not a measurement.** No
autonomous action has ever been applied on this platform, so there is no
observed distribution of allocation changes or action rates to fit to. They are
deliberately tight: the cost of a too-small envelope is an action that needs a
person, and the cost of a too-large one is what this level exists to prevent.

**They require approval before anything runs against them.**

`max_data_age` deliberately equals `decision.DEFAULT_MAX_AGE`. They are two
separate numbers with the same value, and
`test_the_envelope_agrees_with_the_decision_layers_freshness_limit` fails if one
moves without the other — so the codebase cannot end up with two staleness
rules.

## No component can widen its own envelope

This needed **no new mechanism**, which is the part worth understanding.

Every field above is a hard limit. `ENVELOPE_TARGETS` names them, and
`ActionProposal.hard_limit()` returns true for any action aimed at one —
*whatever the proposal itself claimed*. A proposal gets to describe its own
effect; it does not get to decide that the bounds it is judged against are an
ordinary parameter.

So an action proposing to change a bound is an action that **loosens a hard
constraint**, which L61's `classify()` already returns `FORBIDDEN` for, with no
approval path and no `within_bounds` escape.

The rule did not need enforcing. The envelope needed to be *inside the thing
L61 already protected*. `test_an_action_aimed_at_the_envelope_itself_is_forbidden`
proves it across three targets, including one that declares
`touches_hard_limit=False`.

## Absent is not zero

A breach reports **both numbers** — `"max_capital_movement: proposed 15000
against a limit of 10000"` — because "exceeds the allocation limit" is not
something an operator can act on.

A magnitude that is `None` is **not checked** by `within_envelope`: absent is
not zero, and silently treating a missing size as 0 would let an unsized action
through every bound.

But absence must not become a way past the gate either, and at L62 it briefly
was. An action stating `confidence=0.0` was refused while one stating
`confidence=None` passed unchecked — the absent input treated as more
trustworthy than the honest one. `verify()` now refuses a proposal that states
no confidence when the envelope requires any. See
`AUTONOMOUS_CONTROL_VERIFICATION.md`.

A deterministic action states `confidence=1` and says so. Leaving the field out
is not a way to skip a check.
