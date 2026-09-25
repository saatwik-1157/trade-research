# AUTONOMY_POLICY.md

What this platform may do without a person. Level 51, 2026-09-06.

**Every classification below was checked against the code, not against the
previous level's documentation.** Where a capability does not exist, it says so
rather than describing an intention.

---

## The distinction that matters

Two questions get confused, and keeping them apart is the whole policy:

* **What may run unattended?** A worker tick, a supervisor sweep, a recovery
  step — code nobody asked for at the moment it runs.
* **What may that code CHANGE?** Trading through the gated path *is* the
  platform's purpose, so "unattended code must not reach a venue" would be both
  false and wrong to assert — the execution worker reaching the OMS is the
  platform working. What must never happen autonomously is a change to **what
  is permitted**: risk limits, kill switches, safe-mode latches, model
  promotion, the trading mode.

`tests/test_autonomy.py::test_no_autonomous_path_can_change_what_is_permitted`
enforces the second by walking the import graph from every `Worker` subclass.
**It passes**, and its matcher is separately proven able to fire.

---

## Levels

### LEVEL 0 — OBSERVE

| Capability | Where | State |
|---|---|---|
| Collect metrics | `app/observability/collectors.py`, `metrics.py` | **Exists** |
| Five-state health with hysteresis | `app/monitoring/health.py` | **Exists**, observed live |
| Incident streak tracking | `app/observability/incidents.py` `Tracker` | **Exists** |
| Structured JSON logs | `app/core/logging` | **Exists** |

### LEVEL 1 — ALERT

| Capability | Where | State |
|---|---|---|
| Send alerts, 4 channels, dedup, retry, severity | `app/notifications/service.py` | **Exists** |
| Record an incident | `app/observability/incidents.py::record` | **Exists** |
| Failure isolated from trading | verified: a raising bus does not stop an order | **Exists** |

### LEVEL 2 — LOW-RISK AUTO-RECOVERY

| Capability | State |
|---|---|
| Redis circuit breaker (3 consecutive failures) | **Exists** — `app/core/events.py` |
| Recover DB/Redis outage without restart | **Exists**, verified live at L44 |
| Retry a notification | **Exists** — `app/notifications/worker.py` |
| Restart a failed non-trading worker | **DOES NOT EXIST.** `WorkerRegistry` counts failures and never acts |
| Reconnect a market-data provider | **DOES NOT EXIST** |
| Bot restart | **Latent** — the supervisor has the seat; the deployed worker passes no runner |

**The absence of worker auto-restart is not an oversight to be filled quickly.**
`Worker.run` already survives a failing tick — it counts the failure and keeps
looping — so "restart the worker" is a narrower need than it sounds.

### LEVEL 3 — SAFETY CONTAINMENT

| Capability | Where | State |
|---|---|---|
| Enter safe mode, latched, with reasons | `app/recovery/safe_mode.py` | **Exists** |
| Safe mode blocks new orders at 3 server-side paths | verified structurally | **Exists** |
| Latch on unresolved orders at startup | `app/recovery/reconciliation.py` | **Exists** — and **only became reachable on the automated path with the L45 C-1 fix**, which made the pipeline write the `orders` row this reads |
| Kill switches: global / account / strategy / bot | `app/risk/service.py` | **Exists** |
| Pause a bot | `is_disabled` on the row | **Exists** |
| Isolate one failed strategy without stopping the platform | **PARTIAL** — a bot can be disabled; there is no strategy-level isolation distinct from that |

### LEVEL 4 — HUMAN APPROVAL REQUIRED

Strategy deployment · model promotion · risk configuration · broker and account
changes · exiting safe mode · **letting a bot take its bracket from the alert**
(`BOT_BRACKET_SOURCE`, added at L45 F-1 — it lifts a quarantine, and under
fixed-risk sizing a tighter stop produces a larger position).

Enforced today by: permission checks, a mandatory reason on every
administrative write, step-up re-authentication on four dangerous actions
(scoped, single-use, 300s), and the Phase 30 audit above proving no unattended
path reaches them.

### LEVEL 5 — NEVER AUTOMATED

Unrestricted live trading · leverage increase · risk-limit increase · disabling
a kill switch · bypassing the RiskEngine.

`TRADING_MODE=paper` and `LIVE_TRADING=false`. Seven independent locks; with
both flags flipped, `live_execution_allowed` is still False with 10 blockers.
`tests/test_autonomy.py` additionally asserts that **nothing reassigns either
setting after construction** — a mode that can move at runtime is a mode that
can move for the wrong reason.

---

## Guardrails (Phase 3)

```
ACTION IDENTIFIED -> POLICY CHECK -> AUTHORIZATION -> SAFETY CHECK
                  -> EXECUTION -> VERIFICATION -> AUDIT
```

**Honest status: this pipeline is implemented for exactly one action — bot
recovery — and does not exist as a general framework.** For that one action:

| Step | Where |
|---|---|
| Policy check | `recovery_gate` — safe mode, kill switch, unresolved orders, unsettled positions, unusable adapter |
| **Budget check** | `app/bots/budget.py` — **added at L51**, see below |
| Execution | `BotSupervisor._consider_recovery` |
| Verification | the runner returns a boolean; a decline leaves the run crashed |
| Audit | `bot_events`: `recovery_started` / `recovery_completed` / `recovery_refused` / `recovery_budget_exhausted` |

Building a general guardrail framework before there is a second action to put
through it would be inventing an abstraction from one example.

---

## Recovery budget (Phase 19) — added at this level

**The defect it fixes was real and unbounded.** Every gate the bot supervisor
had asks whether recovery is safe *right now*. **None asked how many times it
had already been tried.** A bot that crashes immediately on start passes every
gate on every sweep: restart → new run → crash → next sweep → restart, at the
sweep interval, forever.

Reproduced in `tests/test_bots.py::test_the_budget_is_what_stops_it`, which runs
the identical loop with a generous budget and asserts it *does* run away — so
the bounded test beside it is proven to be doing the work.

```
max_attempts  3      per bot, counted across runs
window        1 hour
cooldown      5 min   (the sweep runs every 30s; without this a
                       crash-on-start bot loops twice a minute)
```

**These are configuration decisions, not measurements.** No bot has ever
crashed on this platform, so there is no observed distribution to fit. They are
recorded as assumptions requiring approval, per Phase 20's instruction about
missing thresholds.

Attempts are counted **per bot, from `bot_events`**, not per run — a per-run
count is always one, because each attempt creates a new run. No new table and
no second counter to drift.

The budget runs **after** the safety gates, so "the platform is in safe mode"
keeps its own message rather than being replaced by "out of budget". Both would
be true and only one tells the operator what to do.

---

## What is NOT authorised

```
AUTONOMOUS UNRESTRICTED LIVE TRADING:      NOT AUTHORIZED
AUTONOMOUS RISK INCREASE:                  NOT AUTHORIZED
AUTONOMOUS STRATEGY/MODEL PROMOTION:       NOT AUTHORIZED
```

## What this level did not build, and why

**A HealthOrchestrator that acts.** The health signals exist and are good. An
orchestrator that consumes them and takes recovery actions would be new
autonomous machinery on a platform where four CRITICAL execution-core defects
were fixed **this same day** (C-1 to C-4), and where the one autonomous seat
that did exist turned out to be unbounded. Adding more autonomy before that
settles is the wrong order.

**Chaos validation against a broker.** No venue has ever been connected. MT5
disconnect, broker timeout and reconciliation-failure scenarios cannot be
executed, and writing them up as passing would be fabricated recovery evidence.
