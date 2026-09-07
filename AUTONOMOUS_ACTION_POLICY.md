# AUTONOMOUS_ACTION_POLICY.md

What the platform may do to itself without asking. L61, 2026-09-06.

---

## The one rule

**Autonomy may take permissions away. It may never grant them.**

Every "never automatically increase" in the brief — risk limits, leverage,
drawdown tolerance, position size, allocation, live trading — reduces to that
sentence, and the classifier implements it directly rather than as a list of
special cases.

## Classified by effect, not by name

The obvious implementation is a set of banned action names:

```python
FORBIDDEN = {"increase_leverage", "enable_live_trading", ...}
```

**That is a blocklist one rename away from a hole.** A new action kind nobody
added to the set, a refactor that renames one, a plausible-sounding
`adjust_exposure_ceiling` — each walks straight through.

So an action describes **what it does**:

| Field | Meaning |
|---|---|
| `effect` | `TIGHTENS` / `NEUTRAL` / `LOOSENS` — which way it moves permissions |
| `touches_hard_limit` | risk limit, leverage, drawdown cap, kill switch, trading mode |
| `within_bounds` | is the magnitude inside a range somebody approved |
| `reversible` | can it be undone without a second decision |

A test proves the point by classifying two deliberately innocuous names —
`a_name_nobody_thought_of` and `innocuous_sounding_tweak` — and both are
refused for what they do.

## The table

| Effect | Hard limit? | Classification |
|---|---|---|
| `LOOSENS` | **yes** | **FORBIDDEN** |
| `TIGHTENS` | yes | `AUTO_APPLY_BOUNDED` |
| `LOOSENS` | no | `REQUIRES_APPROVAL` |
| `TIGHTENS`, in bounds, reversible | no | `AUTO_APPLY_BOUNDED` |
| `TIGHTENS`, out of bounds or one-way | no | `REQUIRES_APPROVAL` |
| `NEUTRAL` | — | `RECOMMEND` |

Three consequences worth stating explicitly:

**`FORBIDDEN` has no approval path here.** Not "requires two signatures" — no
path. `within_bounds=True` and `reversible=True` do not rescue it. A control
loop that could be argued into raising its own ceiling has no ceiling; raising
a hard limit is a governance action a person takes elsewhere, with its own
approval.

**Tightening a hard limit is automatic on purpose.** Safe mode, a kill switch,
blocking new entries. The same reasoning the safe-mode route already gives for
requiring no step-up: *a control expensive to engage is one nobody engages in
an emergency.*

**Loosening a soft thing still needs a person**, even in bounds. "Never
automatically increase risk" has no in-range exception.

## A loop that oscillates stops controlling

`ActionLedger` counts direction changes per target within a window. Three
reversals in an hour and the target drops to `REQUIRES_APPROVAL`:

> *"{target} has reversed direction {n} times in 1h, which is its limit of 3.
> A control that alternates is not responding to conditions, it is arguing with
> itself; it holds until a person looks."*

**This is the L51 defect in another costume.** The bot supervisor had five good
safety gates — safe mode, kill switches, unresolved orders, unsettled
positions, adapter health — and not one of them asked *how many times*, so a
bot that crashed on start restarted forever. Reduce-restore-reduce is the same
shape: it looks like responsiveness and it is thrashing.

Two calibrations, both tested:

* **Two reversals is not thrashing.** Reduce, then restore when the condition
  clears, is the mechanism working. The third is where it becomes a loop.
* **The window expires.** A loop an hour ago is not a loop now, or the guard
  latches forever on a system that has since settled.

### The interaction that matters most

**Thrashing never blocks an emergency stop.** If instability could suppress the
response to instability, the guard would disable the thing it exists to
protect. An action that tightens a hard limit is exempt from the oscillation
demotion, and there is a test for exactly that.

And oscillation can only ever **demote**. It is never a path to more autonomy —
a forbidden action stays forbidden on a thrashing target.

## What this is not

**It applies nothing.** It classifies a proposal and returns the
classification. A syntax-tree test fails if the module calls `place_order`,
`close_position`, `cancel_order`, `submit`, `approve`, `set_limits`, `execute`
or `commit`, or imports anything from `app.` at all.

A control layer that could apply its own decisions would be the L45 C-2 defect
at the top of the stack: a component quietly assuming another's authority.

## What was not built

L61 asks for the full closed loop — state estimation, outcome measurement,
expected-versus-actual, policy effectiveness, canary adaptation, rollback.

**Those need a loop that has run.** No autonomous action has ever been applied
on this platform; there are no outcomes to measure, no policy whose
effectiveness could be assessed, and no rollback that has been exercised.
Building the measurement half now would be building it against imagined data.

What exists is the half that must be right **before** the first action is ever
applied: what may be done autonomously, and when the loop must stop. Those are
the parts it would be tempting to add afterwards, once something is already
running.

`DEFAULT_OSCILLATION_LIMIT = 3` and a one-hour window are configuration
decisions, not measurements — no control loop has run here — and they are
recorded as assumptions requiring approval.
