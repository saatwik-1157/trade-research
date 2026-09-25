# STRATEGY_STATE_MACHINE.md

Which strategy-version status may follow which. L57, 2026-09-06.

---

## The defect

`strategy_versions.status` is a lifecycle, and it was **the one lifecycle in
this platform with no transition guard.**

`POST /v1/strategy-builder/{key}/versions/{n}/status` checked the
**destination** thoroughly — is it reachable, is it blocked until a later level
builds the machinery to enforce it, and for `validated`, does the definition
still validate against the current platform. Then:

```python
version.status = wanted
```

**The (from, to) pair was never consulted.** So `retired → validated` was one
call: a retired strategy brought back with no re-validation of its
dependencies, its model status, its market-data compatibility or its approval.

L57 step 21 states the rule it broke: *"Do not blindly reactivate an old
strategy."*

## Why it is worth naming

Four other subsystems already do this properly — `app/oms/state.py`,
`app/bots/state.py`, `app/risk/state.py`, `app/portfolio/state.py` — each with
a `TRANSITIONS` table and a `check_transition` that raises. The pattern was
established, used everywhere else, and simply absent here.

This is the recurring shape in this repository: not a missing idea, a missing
application of one.

## The machine

```
draft ──────────► validated
  │                   │
  │                   ├──────► draft        (withdraw validation)
  │                   │
  └──────► retired ◄──┘
              │
              └──► (nothing)
```

| From | May become | Why |
|---|---|---|
| `draft` | `validated`, `retired` | the definition passed re-validation; or nobody finished it |
| `validated` | `retired`, `draft` | the ordinary end; or an operator deciding it is not ready after all |
| `retired` | **nothing** | terminal |

## Why `retired` is terminal

Because it is the only rule that forces the evidence L57 step 21 requires.

Reactivation means creating a **new version** — which is drafted, re-validated
against the *current* platform, and approved on its own merits. Allowing
`retired → validated` would let a strategy skip all of that by editing one
field and inherit a decision made before it was retired.

**It does not delete anything.** Step 20's rule holds: retired means *no new
deployment*, never *delete history*. The row, its configuration, its trades,
its analytics and its audit trail all remain. What it cannot do is quietly
become current again.

A test asserts the module reaches no persistence at all — no session, no model,
no `delete`, `drop` or `truncate` — read from the syntax tree rather than the
text.

## Two edges worth stating

**Setting a status to itself is refused.** A no-op is not a transition, and
accepting it would write an audit row recording a change that did not happen.
The route short-circuits before the guard and returns the current state.

**An unknown status is refused by name.** `active`, `paper`, `backtested` and
`paused` are statuses the route blocks with *"the machinery that would enforce
it is built at level NN"*. They must not slip in through the transition check
either, and the error names the three real ones.

## What was NOT built

L57 asks for a fifteen-state lifecycle — `VALIDATING`, `SHADOW`,
`PENDING_APPROVAL`, `DEPLOYED`, `ACTIVE`, `RESTRICTED`, `DEGRADED`,
`QUARANTINED`, `SUSPENDED`, `RETIREMENT_REVIEW` and the rest.

**Three exist, and adding twelve more would be adding states nothing
enforces** — precisely what the existing route already refuses to do, and for
the reason it gives: *"Marking it so would let a strategy claim a state nothing
checks."* That refusal predates this level and is the right instinct.

The states become worth adding as the machinery that enforces each one arrives.
`QUARANTINED` is the nearest: L56 wired `strategies.is_active` so the pipeline
refuses new signals from a switched-off strategy while open positions stay
under the Position Manager — that is quarantine's behaviour, implemented, on
the strategy rather than the version.

## Regression

`tests/test_strategy_lifecycle.py`, 12 tests: the legal moves, `retired`
terminal in every direction, self-transitions refused, unknown statuses
refused, the enum matching the database CHECK constraint, every status having a
transition entry, and — because a guard that exists and is not called is the
defect one level up — that the route calls it **before** it writes.
