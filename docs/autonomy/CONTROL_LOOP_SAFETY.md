# CONTROL_LOOP_SAFETY.md

Runaway loop protection. L62 §12, 2026-09-06.

---

**The loop detection itself is L61's** and is documented in
`AUTONOMOUS_ACTION_POLICY.md`. This records what L62 verified and what it
added, without restating the mechanism.

## The shape being guarded against

    decision → action → observation → new decision → action → …

**This is the L51 defect in another costume.** The bot supervisor had five good
safety gates — safe mode, kill switches, unresolved orders, unsettled
positions, adapter health — and not one of them asked *how many times*, so a
bot that crashed on start restarted forever. Reduce-restore-reduce is the same
shape: it looks like responsiveness and it is thrashing.

## What exists

| Control | Where | Limit |
|---|---|---|
| Direction-change detection | `portfolio/control.py` `ActionLedger` | 3 reversals per target per hour |
| Action frequency | `safety/envelope.py` | 6 per target per window |
| Concurrent actions | `safety/envelope.py` | 3 |
| Chain length | `safety/envelope.py` | 3 |
| Retries | `safety/envelope.py` | 1 |
| Action fingerprint | `safety/verification.py` | one execution per identical action |
| Recovery budget | `bots/budget.py` | 3 restarts/hour, 5-min cooldown |

Chain length is the one worth naming: a chain is how a control loop turns one
surprise into a cascade, and it is invisible to a per-action limit.

## The interaction that matters most

**Thrashing never blocks an emergency stop.** An action that tightens a hard
constraint is exempt from the oscillation demotion. If instability could
suppress the response to instability, the guard would disable the thing it
exists to protect. `test_thrashing_never_blocks_an_emergency_stop`.

Oscillation can only ever **demote**. It is never a path to more autonomy — a
forbidden action stays forbidden on a thrashing target.

## Idempotency

An action's fingerprint covers what makes it the same action: kind, target,
effect, account, environment, the magnitudes, and the policy version. It
deliberately **excludes** the proposal id and the timestamp — including them
would make every resubmission unique and the check useless.

It **includes** the account and the rule set, so "we already did this" can
never be true across a boundary where it is not.

Only a proposal that actually cleared is remembered.
`test_a_rejected_proposal_is_not_remembered_as_done` pins the failure mode this
would otherwise introduce: a refusal caused by a passing condition must not
become permanent.

**The cache is process-local**, deliberately. It answers "have I verified this
action", and a restart legitimately resets that question — unlike a capital
reservation, where a restart losing state was the L54 defect. The durable
backstop for orders remains `orders.intent_id` UNIQUE.
