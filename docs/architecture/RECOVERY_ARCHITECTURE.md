# Recovery, resilience and reconciliation (L38)

Built 2026-09-05.

    FAILURE -> SAFE STATE -> RECOVERY -> RECONCILIATION -> VALIDATION
            -> SAFE RESUME

and never `DISCONNECTED -> EXECUTING`.

---

## 1. The audit: the pieces existed and nothing called them first

Step 1 asks what recovery infrastructure exists before anything is written.
Most of it did.

| Component | Where | Verdict |
|---|---|---|
| Broker reconciliation: venue positions and orders against ours | `app/brokers/reconcile.py` (L10) | **KEEP**, called |
| `OrderManager.reconcile` — the only exit from `unknown` | `app/oms/service.py` (L19) | **KEEP**, never wrapped |
| Position reconciliation | `app/positions/reconciler.py` (L21) | **KEEP**, called |
| Bot supervision: heartbeats, crashed runs, `plan_restart` | `app/bots/supervisor.py` (L22) | **KEEP + FILL ITS SEAT** — §3 |
| `SafetyCheck`, defaulting to refuse-everything | `app/bots/supervisor.py` (L22) | **KEEP** — L38 supplies an implementation |
| Order idempotency: `intent_id`, unique, from `Idempotency-Key` | L19 | **KEEP**, verified |
| Signal idempotency: `signal_key` + the OMS constraint behind it | L20 | **KEEP**, verified |
| Notification idempotency and bounded retry with backoff | L34 | **KEEP**, reused as the pattern |
| Worker loop that survives a failing tick; heartbeat; cooperative stop | `app/workers/base.py` (L02) | **KEEP** |
| Graceful shutdown: workers, then hub, then bus, then engine | `app/main.py` lifespan (L02) | **KEEP + EXTEND** |
| Three-state health checks and readiness | `app/core/health.py` (L02) | **KEEP**, reused by the sequence |
| Detection of staleness, unknown orders, unsettled positions | `app/observability/` (L37) | **KEEP**, consumed |
| `system_events`: component, event type, level, correlation id | `app/models/ops.py` (L05) | **KEEP**, written to |
| Kill switches, latched locks, fail-closed evaluation | `app/risk/service.py` (L17) | **KEEP**, read only |
| Compose healthchecks and `depends_on: service_healthy` | `docker-compose.yml` | **KEEP + MODIFY** — restart policies added |
| A startup sequence that runs any of it | — | **did not exist** |
| Safe mode | — | **did not exist** |
| Backup or restore scripts | — | **do not exist** — §9 |

**The gap was not the checking. It was that nothing ran the checks first.**
`MIGRATION_STATUS.md` had said so since L30: *"the pieces exist; nothing calls
them first."*

---

## 2. What L38 added

Four modules and one latch.

```
app/recovery/
    contract.py        the ladder, safe-mode reasons, step results
    safe_mode.py       the latch, its reasons, and the guard callers use
    reconciliation.py  the existing reconcilers, called in a defined order
    bots.py            the SafetyCheck L22 left a seat for
    manager.py         the startup sequence, the latch decision, the audit trail
```

`reconciliation.py` contains **no comparison logic**. A second implementation of
"do these positions match" is a second answer, and the one on the dashboard
would eventually disagree with the one the OMS acted on.

---

## 3. The startup sequence

Step 17's ordering, run in `create_app`'s lifespan — **before anything consumes
a signal**. The execution worker and the bot supervisor are registered and not
started, so nothing has begun when it runs.

```
 1 configuration          settings and the live gates, read and reported
 2 database               SELECT 1, through the check /health/ready uses
 3 migrations             every expected table is present -- never migrates
 4 event_bus              the hub subscribed and its reader is running
 5 market_data            provider usability and the newest stored bar
 6 webhook_gateway        whether a shared secret is configured at all
 7 broker                 adapters registered and what each says about its link
 8 oms_reconciliation     orders whose venue state was never established
 9 position_reconciliation local positions the platform could not settle
10 risk_engine            loaded, and which kill switches are engaged
11 bot_recovery           plan_restart -- what SHOULD happen, not what does
12 notifications          channels and the delivery queue
13 monitoring             the observability collector
14 operational_mode       the mode this process runs in, and why
```

Steps 7 → 8 → 9 are the order step 13 asks for: a comparison against a venue
that cannot be reached is not a comparison.

**It never raises.** A recovery routine that can crash leaves the platform in
whatever state the crash found. Every check is wrapped; an unexpected exception
becomes a `FAILED` step with its category, and the sequence continues. If the
sequence itself fails, the lifespan latches safe mode with
`RECOVERY_FAILED: the startup recovery sequence itself failed; nothing was
verified`.

**It never enables live trading.** Rule 15. `TRADING_MODE` and `LIVE_TRADING`
are read and reported; nothing writes either, no code path touches
`LIVE_GATES`, and a test greps the package for the assignment. The
`operational_mode` step says so in as many words.

**Three checks are `SKIPPED`, not `OK`**, and the distinction matters:

* no broker adapter registered → *"this is not a clean reconciliation; it is
  the absence of one"*;
* no bar has ever been stored → *"that is not staleness"* — a fresh install
  must not boot into safe mode for having no history;
* no webhook secret → L09's deliberate default, which refuses every alert.

---

## 4. Safe mode

The genuinely new thing. `app/recovery/safe_mode.py`.

**It never closes without a reason.** Step 18 says not to use safe mode to hide
errors, so a latch carries a `SafeModeReason` and a detail, `describe()` returns
every reason currently holding it, and the refusal a user sees names the
condition:

> the platform is in safe mode, so no new order is submitted.
> `UNKNOWN_ORDER_STATE: 1 order whose venue state was never established`.
> Reconcile and release it at /v1/recovery.

**What it blocks**, all three enforced server-side:

| Path | Where |
|---|---|
| new order submission | `POST /v1/orders` — a 409 naming the condition |
| automated execution | `ExecutionPipeline`, gate zero, `Outcome.safe_mode` |
| bot recovery | the `SafetyCheck` handed to `BotSupervisorWorker` |

**What it deliberately does not block**: monitoring, reconciliation, position
and order visibility, risk evaluation, diagnostics, admin inspection and the
recovery routes. Reconciling is how you get out.

**It is a second refusal, never a replacement for one.** Rule 2: recovery
cannot bypass the RiskEngine. Safe mode is checked *before* risk and adds a
refusal; it never approves anything, never releases a kill switch and never
widens a limit. A submission that gets past it faces every check it faced
before.

**It is process state, deliberately.** A stored flag is one somebody forgets to
clear, and one set by a process that has since died is a platform that will not
trade for a reason nobody can find. The latch is re-derived at every startup
from the sequence, and every open and close is written to `system_events` so
the history outlives the state.

**Closing is automatic; opening is not.** `POST /v1/recovery/safe-mode/exit`
**re-runs the startup sequence first** and releases only the latches whose
condition has actually cleared. A release that skipped the re-check would let
somebody resume trading into an unreconciled account. The response says which
latches are still holding, and why.

Which conditions latch it:

| Blocking step | Reason |
|---|---|
| `oms_reconciliation` | `UNKNOWN_ORDER_STATE` |
| `position_reconciliation` | `POSITION_MISMATCH` |
| `broker` | `BROKER_UNREACHABLE` |
| `database`, `migrations` | `DATABASE_INCONSISTENT` |
| the sequence itself failing | `RECOVERY_FAILED` |
| an administrator | `OPERATOR` |

A step with no entry is **reported and does not latch**. `event_bus` not
running is worth saying and is not a reason to stop trading — step 18 again:
do not use safe mode to hide errors.

---

## 5. Unknown order state

Step 6, and the rule the whole level turns on.

```
  UNKNOWN
     |
     +--> QUERY THE VENUE          POST /v1/orders/{id}/reconcile  (L19)
              |
              +--> FOUND      -> synchronise local state
              +--> NOT FOUND  -> `failed`, the one state a fresh order may follow
              +--> UNRESOLVED -> stays blocked, and safe mode says so
```

**Nothing in `app/recovery` re-sends anything.** A parse test asserts the
package never calls `submit`, `place_order`, `cancel`, `modify`, `close` or
`close_now`, and never imports an order manager or an adapter class. The reason
`app/brokers/reconcile.py` gives is the reason: *an IPC timeout after
`order_send` looks exactly like a rejection from the caller's side.*

The bot gate refuses a restart while the account holds one, because a restart
over an unsettled order is a second order waiting to happen.

---

## 6. Bot recovery: L22's seat, filled

`BotSupervisor` has taken a `SafetyCheck` since L22 and defaulted to
`_refuse_by_default`, which refuses everything — *"the absence of a check is not
evidence that recovery is safe."*

L38 supplies an implementation. **It does not lower the bar**: every branch
returns a refusal, `None` means only "these five checks found nothing", and
L22's own gates (the run must be `crashed` rather than `halted`, the bot must
be enabled) still run afterwards.

The five, cheapest first:

1. safe mode is engaged;
2. a kill switch covers the platform, the account or the strategy — a decision
   somebody made, and recovering out of it automatically is the bot-level
   bypass L22 §22 forbids;
3. the account has an order whose venue state was never established;
4. the account has a position the platform could not settle;
5. the account's adapter is registered and not usable — a bot that cannot reach
   its venue restarts into an immediate refusal, and the restart is what would
   then be retried.

---

## 7. Reconciliation repairs nothing

Step 14, and `app/brokers/reconcile.py` already spent a paragraph on why:

> the natural instinct on discovering a position the platform does not know
> about is to close it, and the natural instinct on discovering an internal
> position the venue does not have is to re-send it. Both instincts are wrong.
> The first closes a trade somebody may have opened by hand; the second is
> exactly how a crash between send and log becomes two positions.

`POST /v1/recovery/reconcile` runs the same read-only sweep the startup
sequence runs. A test seeds an unknown order and an unknown position, runs it,
and asserts both are byte-for-byte unchanged.

---

## 8. Idempotency, audited rather than added

Step 8 asks for an audit of duplicate-event risk. Every layer already had one,
and L38 verified rather than rebuilt:

| Path | Guarantee | Level |
|---|---|---|
| TradingView webhook | `webhook_events` recorded before processing | L09 |
| Signal → order | `orders.intent_id`, unique, from `Idempotency-Key` | L19 |
| Signal replay in-process | `ExecutionPipeline.seen`, in front of the constraint | L20 |
| Trade journal | one row per completed trade, keyed on the position | L31 |
| Trade review | `UNIQUE (trade_id, review_version)` | L33 |
| Notification | `UNIQUE (user_id, dedup_key)` from the event id | L34 |
| Delivery | `UNIQUE (notification_id, channel)` | L34 |
| Discord | inherits both | L35 |

L38 added none, and needed to add none.

---

## 9. Backup and restore: documented, not implemented

Steps 22 and 23. **No backup script exists and L38 did not write one**, and
that is a deliberate refusal rather than an omission.

A backup script that this project cannot test is a backup nobody should trust.
`pg_dump` in a container this repository does not deploy, on a schedule nothing
runs, to storage nobody has chosen, with an encryption key nobody holds, is a
file that will be discovered to be empty on the day it matters.

What is documented instead, in `BACKUP_RESTORE.md`: what must be backed up, the
restore order that respects reconciliation, and an **honest RPO/RTO** — step 22
says not to claim one the infrastructure cannot achieve. On this deployment the
honest answer is that there is no automated backup, so the RPO is *since the
last manual dump* and the RTO is *however long a person takes*.

---

## 10. What recovery cannot do

`app/recovery/` imports no order manager class, no broker adapter class, no
risk engine and no sizing service. Four tests hold it:

* an AST walk over imports and called method names;
* a grep for `live_trading =`, `trading_mode =` and `LIVE_GATES[`;
* a grep for `DROP TABLE`, `DELETE FROM`, `TRUNCATE`, `db.delete(` and
  `drop_all`;
* a grep for `DiscordWebhookChannel`, `EmailChannel`, `smtplib` and
  `NotificationService(` — step 28 says not to call Discord from recovery
  logic, and it publishes one `SYSTEM_ALERT` instead.

---

## 11. Tests

`backend/tests/test_recovery.py`, 44 tests.
`frontend/src/components/RecoveryPanel.test.tsx`, 13.

| Test | Step / rule |
|---|---|
| `test_recovery_cannot_submit_modify_or_cancel_anything` | rules 3, 12 |
| `test_recovery_never_enables_live_trading` | rule 15 |
| `test_recovery_destroys_no_historical_record` | rule 13 |
| `test_an_unknown_order_latches_safe_mode_and_is_never_retried` | 6, 18 |
| `test_a_missing_table_latches_safe_mode_rather_than_migrating` | 15 |
| `test_the_startup_sequence_never_raises` | 17, 25 |
| `test_no_market_data_is_skipped_not_stale` | 11 |
| `test_no_broker_adapter_is_skipped_not_a_clean_reconciliation` | 5, 65 |
| `test_reconciliation_repairs_nothing` | 14 |
| `test_safe_mode_blocks_a_new_order_and_names_the_condition` | 18, 19 |
| `test_the_execution_pipeline_refuses_while_safe_mode_is_engaged` | 18 |
| `test_a_bot_is_not_recovered_over_an_unresolved_order` | 6, 10 |
| `test_a_bot_is_not_recovered_out_of_a_kill_switch` | 19 |
| `test_a_clean_account_passes_the_bot_gate` | — the gate refuses, not everything |
| `test_releasing_safe_mode_re_runs_the_sequence_and_refuses_while_it_still_holds` | 18 |
| `test_a_notification_failure_never_blocks_recovery` | 21, rule 14 |
| `test_the_application_runs_the_sequence_on_startup` | 17 |

**A real bug was found writing them.** Adding `Outcome.safe_mode` without
adding it to `NO_ORDER` made `created_order` report `True` for a refusal that
sent nothing — the execution log would have said an order was created when the
pass stopped at gate zero. Caught by
`test_the_execution_pipeline_refuses_while_safe_mode_is_engaged` and fixed.

---

## 12. Known limitations

1. **No automatic reconnection.** Step 5 asks for controlled reconnection after
   a disconnect. The adapter has `reconnects` and the registry has a lock, but
   nothing drives a reconnect loop; a disconnected adapter is detected, latches
   safe mode, and waits for a person. Adding a loop is real work and it belongs
   with a live adapter, which does not exist.
2. **No backup or restore automation** — §9.
3. **No dead-letter queue.** The notification delivery table is the one queue
   and it has bounded retries and a FAILED terminal state, which is the same
   shape. There is no other queue to give one to.
4. **Redis recovery is the client's.** `redis-py` reconnects on its own and the
   bus reports what the last publish actually did. Nothing rebuilds a cache
   because nothing caches authoritative trading state in Redis — the database
   is the source of truth and step 16 says to confirm that rather than assume
   it.
5. **The safe-mode latch is per process.** Two API processes hold two latches.
   Correct for this deployment; a shared latch needs a row and a lease, which
   is a real design rather than a flag.
6. **Fault injection is by mock, not by chaos.** Step 30 asks for controlled
   fault injection where practical. Every failure mode is exercised against a
   fake — a broker registry that raises, a hub that raises, a dropped table —
   and none against a real disconnected terminal, because there is no terminal.
