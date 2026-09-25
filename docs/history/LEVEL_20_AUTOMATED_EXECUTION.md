# LEVEL 20 — AUTOMATED EXECUTION ENGINE

Completed 2026-09-03. Adaptive build mode: audit → baseline → plan → implement
→ integrate → test → verify → document.

The engine is an **orchestration layer**. It runs an externally-arriving
signal through gates that already exist, and it computes none of them: no risk
limit, no quantity, no fill. It holds no broker adapter and imports none — a
test parses every module in the package to prove it.

---

## 1. What already existed

The audit's finding is that most of the pipeline was built, and one link was
missing.

| Stage | Existed as | Verdict |
|---|---|---|
| Webhook gateway | `app/webhooks/gateway.py` — secret, idempotency, symbol resolution, writes a `Signal`, publishes `SIGNAL_CREATED` | **KEEP** |
| Signal record | `signals` table with `signal_key UNIQUE`, a status vocabulary and an auth strength | **KEEP** |
| Strategy engine | `app/strategies/` | **KEEP** |
| AI seat | `AiFilter` / `AiVerdict` — advisory, may only decline | **KEEP**, reused |
| Risk | `app/risk/` | **KEEP** |
| Sizing | `app/sizing/` (L18) | **KEEP** |
| OMS | `app/oms/` (L19) | **KEEP** |
| Broker adapter | `app/brokers/` | **KEEP** |
| Position manager | `app/positions/` | **KEEP** |
| Workers | `app/workers/base.py` — supervised loop, heartbeat, registry | **KEEP**, reused |
| A strategy-driven pipeline | `app/paper/engine.py` (L16) | **KEEP** |
| Background execution surviving a closed browser | `app/paper/service.py` | **KEEP** |

**The missing link:** `SIGNAL_CREATED` had **no consumer**. Two producers
published it — the webhook gateway and the strategy engine — and both said so
in their own docstrings. A TradingView alert became a row and stopped there.

That is exactly the gap `IMPLEMENTATION_PRIORITY.md` had recorded as **A8**,
blocked on L18 and L19. Both are now built, so it was buildable.

---

## 2. What was added, and what it deliberately is not

### 2.1 `app/execution/pipeline.py` — the orchestrator

It is **not a second `PaperEngine`**, and the distinction is the level's main
design decision:

| | drives | runs a strategy? |
|---|---|---|
| `app/paper/engine.py` (L16) | bars from `app.marketdata` | yes — it *produces* the signal |
| `app/execution/pipeline.py` (L20) | a `Signal` row that already exists | no — the decision was made elsewhere |

They share every gate, and neither computes a risk limit, a quantity or a fill
of its own. Merging them would mean one class that both generates signals from
bars and consumes signals from outside, which is two jobs wearing one name.

### 2.2 One outcome vocabulary, extracted rather than duplicated

`Outcome` and `NO_ORDER` moved from `app/paper/engine.py` to
`app/execution/outcome.py`; the paper engine imports and re-exports them, so
nothing downstream changed. The reason is §35's counters: a paper bot
reporting `risk_vetoed` and an orchestrator reporting something else cannot be
added together, and the first dashboard built over them would silently
under-count one.

L20 adds nine values for the failure modes an **externally-arriving** signal
has and a strategy-generated one does not: `signal_invalid`,
`strategy_unknown`, `strategy_disabled`, `signal_stale`,
`source_unauthorized`, `no_venue`, `execution_unknown`, `order_submitted`,
`partially_filled`.

`ORDER_UNSETTLED` is deliberately separate from `NO_ORDER`: **"nothing was
sent" and "something was sent and we do not know what happened" are the two
facts an operator must never confuse**, because only the first is safe to
retry.

### 2.3 `app/execution/worker.py` — browser independence

A `app.workers.Worker`: the same supervised loop with a heartbeat every other
background job uses. Not a second worker system, scheduler or queue.

**Why a worker rather than the webhook request.** §20 says not to run
execution inside the HTTP request, and the reason is stronger than latency: an
alert executed inside the request is an alert whose execution is lost if the
connection drops after the gateway wrote the row. The gateway's job ends at
"this signal is recorded"; the worker's starts there, and the `signals` table
is the handover.

**A signal is claimed before it is processed.** The read and the status change
are one transaction with `FOR UPDATE SKIP LOCKED`, so two workers cannot both
take it. The in-process `seen` set and the OMS's `intent_id` uniqueness are the
second and third guards — this is the first, and the only one that survives
two processes.

### 2.4 `POST /v1/execution/start|stop`, `GET /v1/execution/status`

A **control plane, not the execution path**. There is no route that takes a
payload and trades it: signals reach the engine as recorded rows, so there is
exactly one consumer of the execution path, and a test asserts the router
never mentions `IncomingSignal` or `pipeline.process`.

The worker is **registered at startup and not started**, for the same reason
no broker adapter is registered at startup.

---

## 3. Decisions

### 3.1 A refusal usually consumes the signal — except one

Most refusals mark the signal finished. A vetoed signal left in `new` would
re-run the same veto every two seconds forever; a disabled strategy's queue
would all fire the moment it was re-enabled.

**`execution_unknown` does not consume it, and neither do `no_venue`,
`spec_incomplete` or `strategy_error`.** Those are conditions that can clear,
and a signal parked in `new` is one a later pass or a reconciliation can still
act on. Marking them finished would throw a signal away because a dependency
was briefly down. `status_for` returns `None` for exactly those, and a test
pins the set.

### 3.2 The live fence is stated twice on purpose

`mode == "live"` is refused by the pipeline *and* by the Risk Engine. A
defence that exists once is a defence that can be removed once.

### 3.3 A weak-authenticated alert is recorded and not executed

The gateway records every accepted alert; the orchestrator refuses to *act* on
one whose authentication was weak. "We heard it" and "we act on it" are
deliberately different bars.

### 3.4 Signal age is measured against the signal's own timestamp

Not against when the worker happened to read it. "The alert was late" and "we
were slow to look" are different faults, and only the first is a reason not to
trade.

### 3.5 The engine never raises

Every path out of `process()` is a recorded `ExecutionResult`. A stage that
raises becomes `strategy_error` with the exception in the detail, because a
loop that runs unattended needs a decision it can record, not an exception it
has to classify.

### 3.6 One correlation id

`execution_id` is minted once per attempt and lands on the order's sizing
snapshot, the signal's metadata and every log line — so "why did this trade
happen" has one string to follow from the alert to the fill.

---

## 4. Changes, classified

**ADD** — `app/execution/` (`outcome.py`, `pipeline.py`, `worker.py`),
`app/api/v1/execution.py`, `tests/test_execution.py` (48 tests), five API
tests, and the three storage-to-pipeline adapters in `app/main.py`.

**REFACTOR (MOVE)** — `Outcome` and `NO_ORDER` out of `app/paper/engine.py`,
re-exported from it.

**KEEP** — everything else. The gateway, the strategy engine, the AI seat,
risk, sizing, the OMS, the broker adapter, the position manager, the worker
base and the paper pipeline are all called, none reimplemented.

**REMOVE / REPLACE** — nothing.

---

## 5. Safety invariants (§46)

| # | Invariant | How it holds |
|---|---|---|
| 1 | TradingView cannot execute directly | An alert becomes a row; the worker is the only consumer, and it runs every gate. |
| 2 | AI cannot execute directly | The seat runs before risk, sees neither risk nor the OMS, and can only decline. |
| 3 | Risk approval mandatory | The OMS takes an `Approval`; only `RiskEngine.approve` makes one. |
| 4 | Sizing output mandatory | The volume comes from `app.sizing`; the pipeline defines no sizing function, asserted by a test. |
| 5 | The OMS controls submission | The pipeline calls `manager.create`/`submit` and holds no adapter. |
| 6 | The adapter is the boundary | No module in `app/execution` imports MetaTrader5 or anything MT5-named. |
| 7 | Duplicates cannot double-execute | Row status, `seen`, `guard_resend`, `intent_id UNIQUE` — four guards, three of which survive a restart. |
| 8 | Unknown requires reconciliation | `execution_unknown` does not consume the signal, and the OMS refuses a second order for that intent. |
| 9 | Disconnect stops new orders | A disconnected adapter produces `failed`; nothing is invented. |
| 10 | Browser closure stops nothing | It is a supervised worker; a test drives a signal to a fill with no request in flight. |
| 11 | Live disabled by default | Refused by the pipeline and by risk; all ten gates false. |
| 12 | Kill switch stops execution | Checked first inside `RiskEngine.evaluate`; a test proves the pass ends there. |
| 13–15 | Position, daily-loss and exposure limits | Enforced by the Risk Engine, unchanged; tests drive them through the pipeline. |
| 16 | No secrets in logs | The pipeline holds no credential; log lines carry ids, prices and outcomes. |
| 17 | No future data in backtests | Untouched by this level; pinned by L18's tests. |
| 18 | Fills are the venue's | The OMS's rule, unchanged. |
| 19 | History preserved | No migration in this level; no table written except `signals.status`. |
| 20 | Working functionality intact | The paper engine re-exports what it used to define; its 93 tests pass unchanged. |

---

## 6. Tests

`tests/test_execution.py` is **48 tests**, covering the brief's list including
the end-to-end paper trade (§41) and the live safety test (§42). Five API
tests cover the control plane.

The three that matter most:

- `test_a_signal_cannot_reach_a_venue_without_passing_risk` — the veto is not
  a check the orchestrator performs, it is a token it cannot forge.
- `test_an_unresolved_intent_leaves_the_signal_unconsumed` — the one refusal
  that must not consume the signal.
- `test_the_pipeline_never_raises`.

---

## 7. Remaining, and honest about it

- **The worker is registered and not started.** Starting it is an operator
  action (`POST /v1/execution/start`), and with no adapter registered every
  signal parks as `no_venue` — which is the correct behaviour of a deployment
  that has not been pointed at a venue, not a bug.
- **The pipeline's risk limits are the engine defaults, not the account's
  stored configuration.** `RiskService.limits_for` is the right source and the
  wiring belongs with the bot manager (L22), which owns per-bot configuration.
  Until then the pipeline runs on defaults, which are the *more* restrictive
  reading, and this is stated rather than hidden.
- **`_to_incoming_signal` reads the entry price from the alert's
  `price_reported`.** A market order sized against the alert's own price is
  approximate; the correct source is a live quote at execution time, which
  needs the market-data subscription L08 lists as missing. Recorded as a
  limitation because the alternative — inventing a price — is the failure this
  repository exists to prevent.
- **The AI seat is a `Protocol` with no implementation.** That is L24/L27's
  work. Absent means no opinion, and no opinion is not approval.
- **Notifications (§34) are not wired.** There is no delivery channel on this
  platform (L34), and `UnconfiguredDelivery` refuses rather than pretending.
  The events exist on the bus; a notifier is a consumer away.
