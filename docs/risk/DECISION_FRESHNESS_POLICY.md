# Decision freshness policy

Level 81 §6. Built because it was the one part of that level with real ground
under it — everything else there depends on research objects Levels 74–80 did
not build.

## The rule it implements

> *"Do NOT use one universal freshness threshold for all data types."*
>
> Market price may need seconds/minutes. Quarterly fundamentals may remain
> valid for weeks. Guidance can change immediately after earnings.

Before this, `Input.freshness()` compared every input against one
`DEFAULT_MAX_AGE` of five minutes. That called an hour-old quote *and* a
two-week-old filed quarter by the same name, and it was wrong about both: the
quote had been superseded hundreds of times, and the quarter was still exactly
as true as the day it was filed.

## Where it lives

`app/portfolio/decision.py` — `Staleness`, `POLICY`, `staleness_for`, and
`Input.kind`.

Precedence, highest first:

1. An explicit `max_age=` / `aging_after=` argument.
2. The policy for this input's `kind`.
3. `DEFAULT_MAX_AGE`.

**Nothing that existed before changed behaviour.** An `Input` with no `kind`
resolves to the default, which is what every call site written before this
policy passes.

## The table

| kind | stale after | aging after | what it rests on |
|---|---|---|---|
| `quote` | 30s | 10s | a price is evidence about a moment |
| `position` | 5m | 2m | changes only on a fill, but a stale reading is how a platform trades against a position it no longer has |
| `account` | 5m | 2m | balance and margin move with every open position's float |
| `risk_state` | 1m | 30s | a limit breached a minute ago and not re-read is a limit not enforced |
| `fundamentals` | 45d | 21d | a filed quarter stays true until the next is filed; 45d spans a quarter plus filing lag |
| `guidance` | **never** | 21d | event-bounded — see below |
| `earnings` | **never** | 21d | event-bounded — a reported quarter is superseded, not decayed |
| `channel` | 30d | 7d | the least defensible row here — no channel source is wired and nothing has measured the decay |

## Event-bounded inputs

`max_age=None` means **age cannot make this input stale**, and it is not a
licence to trust it forever.

Guidance is invalidated by the company changing it, not by a clock. It is
exactly as true the day before an earnings call as the day it was issued, and
then it can be worthless in a minute. A duration cannot express that, and a
duration chosen anyway says something false in both directions — stale while
still current, fresh the moment it stopped being true.

So those kinds carry `aging_after` and never `STALE`. **`AGING` on an
event-bounded input means "nobody has checked whether the event happened"** —
a statement about the platform rather than about the fact, which is the same
distinction the review layer draws between `UNKNOWN` and `POOR`.

## Every number here is an assumption

Not one of these durations is a measurement. Nothing on this platform has a
measured staleness distribution for any input kind, and `DEFAULT_MAX_AGE`
already recorded itself in exactly those terms.

A table of confident-looking durations is **worse** than one default if it
hides that, because it looks measured. So every entry states what it rests on,
and a test enforces it: `test_every_policy_entry_says_why` fails on an entry
with a thin reason, and on an event-bounded entry that can never prompt anyone
to look.

The value of the table is not that the numbers are right. It is that they are
written down where they can be argued with and approved, instead of one value
applied everywhere by default.

## What it cannot do

It cannot detect the event that supersedes an event-bounded input. Nothing in
this platform ingests guidance or earnings, so `AGING` after 21 days is the
strongest statement available — "somebody should check" — and there is no
mechanism to check with. That arrives with Level 74's guidance intelligence, if
it is ever built.

An unknown `kind` returns the default rather than raising. A caller naming a
kind nobody wrote a policy for is in a decision path, and an exception there
would turn a missing table entry into an outage.
