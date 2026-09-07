# AUTONOMOUS_INCIDENT_RESPONSE.md

What happens automatically when something breaks, per incident. Level 51,
2026-09-06.

**Read the "Automatic action" column carefully: it is mostly "none".** That is
the current honest state of this platform, and for most rows it is the right
state — `Worker.run` already survives a failing tick, and a component that
recovers by itself does not need an orchestrator to restart it.

**There is no generic retry.** Rule: an action appears below only if something
in the code performs it.

---

## Infrastructure

### Redis unavailable

* **Detection** — `app/core/events.py` counts consecutive publish failures.
* **Classification** — DEGRADED. Durable state is PostgreSQL; Redis loss costs
  the event fan-out, not correctness.
* **Containment** — the circuit breaker opens after **3** consecutive failures,
  so a dead bus stops being awaited on every publish.
* **Automatic action** — degrade. Nothing is restarted.
* **Verification** — `/health/ready` reports 503; the webhook keeps accepting
  and deduplicating.
* **Escalation** — logged. **Observed live at L44:** live 200, ready 503,
  trading 503, webhook still worked, and recovery needed no restart.
* **Audit** — breaker state in the health surface.

### PostgreSQL unavailable

* **Detection** — health check and any query.
* **Classification** — CRITICAL. Nothing may be accepted that cannot be recorded.
* **Containment** — the webhook answers **500**, deliberately: TradingView
  retries on 5xx, and a retried alert is better than a lost one.
* **Automatic action** — none.
* **Verification** — on restore, ready returns 200 and dedup is intact.
* **Escalation** — logged. **Observed live at L44**, recovered with no restart.

### A worker's tick raises

* **Detection** — `Worker.run` catches and counts.
* **Automatic action** — **none**; the loop continues. There is no worker
  auto-restart in this platform, and the loop surviving is why one has not been
  urgent.
* **Escalation** — `WorkerRegistry.stale()` exposes workers whose heartbeat
  stopped.

---

## Trading components

### A bot's heartbeat stops

* **Detection** — `BotSupervisor._check_heartbeat`, against `stale_after`.
* **Classification** — the run is marked `crashed`. **It is not restarted by
  this step**; marking a run dead and deciding to restart it are separate
  decisions, and doing both in one loop makes the second invisible.
* **Automatic action** — none at this step.
* **Audit** — `bot_events`.

### A crashed bot run

* **Policy check** — `recovery_gate` refuses on any of five conditions: safe
  mode engaged; a kill switch covering platform, account or strategy; an order
  on the account whose venue state was never established; a position that could
  not be settled; a registered broker adapter reporting unusable.
* **Budget check** — **added at L51.** Three attempts per bot per hour, five
  minute cooldown, counted across runs from `bot_events`. Runs *after* the
  safety gates so a genuine safety refusal keeps its own message.
* **Automatic action** — restart, **if a runner is wired.** In the deployed
  application `BotSupervisorWorker` passes none, so the run is marked
  `recovering` and the supervisor says so honestly.
* **Verification** — the runner returns a boolean; a decline or a raise leaves
  the run `crashed`.
* **Escalation** — `recovery_refused`, `recovery_failed` or
  `recovery_budget_exhausted` on `bot_events`, plus a warning log naming the
  condition.
* **Never** — recovery does not undo a kill switch. That is a decision somebody
  made.

### An order whose venue state was never established

* **Detection** — the OMS parks it `unknown`; startup reconciliation reads the
  `orders` table.
* **Classification** — CRITICAL → SAFE_MODE latch, `UNKNOWN_ORDER_STATE`.
* **Automatic action** — **none, ever.** Reconciliation *asks* the venue; it
  never re-sends. An IPC timeout after `order_send` looks exactly like a
  rejection from here, and guessing costs a duplicate position.
* **Important history** — this chain only became reachable on the automated
  path with the **L45 C-1 fix**. Before it the pipeline wrote no `orders` row,
  so reconciliation reported `unresolved=0` and **safe mode never latched.**
* **Escalation** — safe mode blocks new orders at three server-side paths;
  exiting requires step-up re-authentication and re-runs the whole 16-step
  sequence, clearing only latches whose condition has actually gone.

### A durable write fails between create and submit

* **Detection** — the store raises.
* **Automatic action** — **refuse the send.** New outcome `not_recorded`: the
  order is discarded from the OMS and the signal is *not* consumed, so a
  database that blinks costs a delay rather than a trading signal.
* **Never** — send anyway. An order at a venue the platform has no record of is
  the state no guard can reason about.

---

## Not implemented, and deliberately

| Incident | Why there is no automatic response |
|---|---|
| MT5 / broker disconnect | **No terminal or venue has ever been connected.** The documented procedure exists (`PRODUCTION_OPERATIONS_RUNBOOK.md`) and reconnection is manual by design: after the terminal returns, the API is restarted so the startup sequence reconciles *before* anything trades. |
| Market-data staleness | The freshness check exists and reports `UNKNOWN` honestly because `market_bars` covers one instrument. There is nothing to disconnect. |
| AI provider failure | Every AI test drives a stub; no provider has ever been called. A fallback written now would be a guess at a failure mode nobody has seen, and Phase 11 forbids inventing a permissive one. |
| Strategy-level isolation | A bot can be disabled. There is no strategy-level isolation distinct from that, and adding one would be new autonomy on a platform whose execution core was repaired the same day. |

---

## The rule that governs conflicts (Phase 25)

```
SAFETY  >  EXECUTION  >  OPTIMIZATION  >  ANALYTICS
```

Concretely: if the RiskEngine cannot be reached, new orders stop. Nothing in
the analytics or intelligence layers may lift that, and the Phase 30 audit
proves no unattended path can reach the calls that would.
