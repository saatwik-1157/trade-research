# FINAL_RISK_REGISTER.md

Every unresolved issue, L42, 2026-09-05. Nothing here is hidden and nothing is
softened.

**After the L45 fixes: CRITICAL 0 · HIGH 3 · MEDIUM 1 · LOW 2 ·
INFORMATIONAL 3.**

**Four CRITICAL defects and one HIGH finding were found on 2026-09-06** by an adversarial audit that
was told to attack the platform's safety claims rather than confirm them. Each
was reproduced independently. They are recorded in full in
`PRODUCTION_PILOT_READINESS.md`.

**All four CRITICAL defects were fixed on 2026-09-06**, each with a regression
test that fails on the old code. Two of them pin the pre-fix behaviour as an
explicit assertion, so the test cannot quietly stop testing anything. They are
kept below rather than deleted: a register that only lists what is open loses
the record of what was wrong and how it was found, and three of these four were
found only because an audit was told to break claims rather than confirm them.

**The platform is still not pilot-eligible, and the fixes are not why.** No
broker has ever been connected and no backup has ever been restored — H-1 and
H-2, unchanged, and now the top of the list.

**F-1 was also fixed on 2026-09-06** and turned out to be five missing links
rather than the one it named; see `SIGNAL_ROUTING.md`. One link is deliberately
left open — no bracket policy exists for an externally sourced signal, and
choosing one is a trading decision requiring approval.

*(L43 counts were CRITICAL 0 · HIGH 2 · MEDIUM 1 · LOW 2 · INFORMATIONAL 3.)*

---

## CRITICAL

### C-1 · An unknown order is re-sent across a process restart

**Subsystem:** OMS / execution pipeline. **Class:** TRADING SAFETY.

The rule "never retry an order whose venue state was never established" holds
only within one process lifetime. Reproduced:

```
SAME process   : pass2 = duplicate_signal  -> REFUSED
ACROSS restart : pass2 = filled            -> ALLOWED
```

Every guard is process-local or absent: `seen` and `by_intent` are in-memory;
`guard_resend` consults the empty `by_intent`; **the pipeline never writes an
`orders` row**, so `orders.intent_id` UNIQUE never fires; `OrderManager.resume()`
and `load_unresolved()` have **zero callers**; and startup reconciliation
queries the empty `orders` table, so safe mode never latches.

A restart is the documented normal deploy (`stop -> migrate -> start`).

**Status:** **FIXED**, 2026-09-06.

`app/execution/store.py` (new) makes the pipeline durable: the `orders` row is
written before anything is transmitted (§18's ordering, which the API order
route always honoured and the pipeline never did), the duplicate guard is asked
of the database as well as of memory, and a durable write that fails refuses
the send outright — new outcome `not_recorded`, with the created order
discarded from the OMS and the signal left unconsumed, so a database that
blinks costs a delay rather than a trading signal.

**Regression:** `tests/test_execution_durability.py` (27 tests). A restart
there is a fresh pipeline, registry and manager; only the database and venue
carry over. `test_the_restart_regression_is_capable_of_failing` pins the old
behaviour as an assertion. The fixture enforces foreign keys, which SQLite
ignores by default and PostgreSQL does not.

**Two further defects were uncovered by fixing it**, both now closed:
`OrderRepository.persist` never wrote an account (leaving recovered orders
belonging to nobody and silently emptying two per-account queries), and a
"safe resend" of a `failed` intent could not be recorded at all, because
`orders.intent_id` is UNIQUE — it would have surfaced as an IntegrityError
between `create` and `submit`. The schema is the stricter authority; the guard
now refuses first, with a message that says why.

### C-2 · Closing a position bypasses the RiskEngine and the OMS

**Subsystem:** positions. **Class:** TRADING SAFETY.

`app/positions/broker_executor.py:110` calls `manager.adapter.close_position()`
directly — no Approval, no OMS submit, no `intent_id`, no order record.
Reachable at `POST /v1/positions/{id}/close`. No `intent_id` means **no
idempotency**: two concurrent closes are two venue calls.

**The module docstring asserts the opposite** ("the Risk Engine approves it, the
OMS submits it... the second path is always the one nobody is watching"). The
wider lesson: this platform's safety argument leans on docstrings, and this one
is confidently false.

Mitigating: a close is risk-reducing, the route is authenticated and
permissioned, and it checks environment mode.

**Status:** **FIXED**, 2026-09-06.

The close now takes the path the docstring claimed: `RiskEngine.approve_close`
mints the `Approval`, `OrderManager.create` makes the order and its
`intent_id`, `OrderManager.close` runs the same lifecycle `submit` runs, and
the order is recorded before and after the venue is called. `.adapter` is no
longer read anywhere in the module.

**The design question it forced is the part worth keeping.** Routing a close
through the RiskEngine *naively* would have been worse than the bug: every
limit here bounds the risk of TAKING a position, so applying them to a close
refuses to reduce exposure at the moment exposure is worst — a breached daily
loss would trap the position, a kill switch would trap every open position
behind it. That is very likely why the original code went around the engine.

So `approve_close` is a separate entry point. It evaluates, records, and
approves regardless, re-stamping the breached checks `enforced=False` so they
reach the order's `risk_snapshot`. Only the mode fence still refuses. The
decision is written down in the method, the executor's module docstring and
`RiskService.engine_for_close`, because "risk approved it" now means something
different for a close than for an open.

`Approval` has two constructors instead of one, so the invariant is now **one
class, not one method**: `test_only_the_risk_engine_can_mint_an_approval`
walks every file in `app/` and fails on an `Approval` built outside
`app/risk/engine.py`.

**Regression:** six tests, including two concurrent identical closes reaching
the venue exactly once, and a breached limit that cannot trap an open position.

### C-3 · The guard test does not guard the close path

**Subsystem:** tests. **Class:** TRADING SAFETY (contributing).

`test_the_broker_executor_reaches_no_terminal_of_its_own` asserts only that the
module imports nothing named `MetaTrader5`/`mt5`. It passes on the broken code
and reads as though the path were guarded.

**Status:** **FIXED**, 2026-09-06. Two tests added beside it, one negative
(`never_touches_an_adapter_directly` — no attribute named `adapter` is read
anywhere in the module) and one positive (`goes_through_risk_and_the_oms` —
`approve_close`, `create`, `close` and `guard_resend` are all called). Both,
because asserting the absence of a shortcut is not the same as asserting the
presence of the real path.

### C-4 · The Approval binding check is vacuous

**Subsystem:** OMS / risk. **Class:** TRADING SAFETY (latent bypass).

Recorded here for the first time — the L45 count said four and this register
listed three.

`app/oms/service.py` called `approval.binds(approval.bound_fields())`. Both
sides derived from the same object, so the digest always equalled
`request_hash` and **the check could never fail**, while its error message
described catching exactly the substitution it could not see. Not exploitable
on either live path (both pass one account through), but `order_type` was
genuinely unbound: risk evaluated a market order and the OMS could create a
limit or stop one.

**A safety check that cannot fail is worse than an absent one, because it reads
as protection in every audit.**

**Status:** **FIXED**, 2026-09-06. `Approval.binds_order()` overrides the three
fields the caller controls; the OMS validation is split so identity and expiry
are checked first and binding after the shape checks, keeping the precise
messages. Four regression tests, including an AST scan for the exact tautology
— written against the syntax tree because the first draft matched its own
docstring.

---

Four issues closed at L43 (M-1 CI, M-2 the realtime flake, M-3 concurrency,
L-3 the Redis breaker). The two HIGH items are unchanged and both still block
live trading, because neither can be closed by writing code: one needs a
storage decision, the other needs a demo account.

*(Original L42 counts: CRITICAL 0 · HIGH 2 · MEDIUM 4 · LOW 3 · INFORMATIONAL 3)*

The two HIGH items both block live trading. Neither blocks deploying and
operating the platform in paper mode.

### C-4 · The Approval binding check is vacuous

`app/oms/service.py:822` calls `approval.binds(approval.bound_fields())`; both
sides derive from the same object, so it **always returns True and can never
fire**. Its message claims to check "this order" and never looks at one.

Not exploitable today (both call sites pass one account throughout), but
`order_type` is genuinely unbound — `bound_fields()` hardcodes `"market"` while
the API accepts `limit`/`stop` — and any future caller passing a different
`account_id` is silently authorized. **A safety check that cannot fail is worse
than an absent one: it reads as protection in every audit.**

**Next action:** pass the order's real fields to `binds()`, and bind `order_type`.

---

## HIGH

### F-1 · The TradingView chain cannot complete in the deployed wiring

The webhook gateway never writes `account_id` into a signal's `meta`;
`_to_incoming_signal` requires it. **Measured: all 118 signals in the deployed
database sit at `status = new`.** The headline flow has never been able to
complete end to end, independent of the absent broker — L40/L41 verified the two
halves separately and never joined them through the worker.

Fails closed, so not a safety defect. **Next action:** record the account on the
signal at the gateway, then add a test that drives webhook → worker → pipeline
as one chain.


### H-1 · No backup has ever been taken or restored

**Subsystem:** database / deployment
**Impact:** total loss of trading history, journal, audit trail and model
registry on a volume failure. There is nothing to restore from.
**Detail:** no scheduled `pg_dump`, no storage target, no retention policy, no
encryption, and **no restore has ever been performed**. L41's own brief states
that a backup is not operational until a restore has been tested.
**Mitigation in place:** trading data is on a named Docker volume, not
ephemeral storage. L41 added `wal_level=replica` and `max_wal_size=1GB` so the
WAL exists when somebody takes a base backup. `BACKUP_RESTORE.md` documents a
fifteen-step restore order with reconciliation at step 8, and keeps an honest
RPO of *"since the last manual dump"*.
**Status:** OPEN. L38 deliberately refused to write a script it could not
test, and L41 did not overturn that.
**Next action:** decide where backups go — that is a deployment decision this
repository cannot make alone — then schedule `pg_dump`, and **restore one into
a scratch database and compare row counts** before calling it done.

### H-2 · No broker adapter has ever been registered

**Subsystem:** MT5 / broker
**Impact:** every claim about reconciliation, disconnect handling, order
rejection, partial fills and unknown-state recovery rests on a fake this
repository wrote. Real retcodes, requotes, slippage and reconnect behaviour are
unverified.
**Mitigation in place:** the platform *knows* it: `broker` reports
NOT_CONFIGURED, `/health/trading` returns 503, and the recovery sequence reports
"this is not a clean reconciliation; it is the absence of one" rather than OK.
`assert_demo` refuses any non-demo `trade_mode` below the platform.
**Status:** OPEN, and **no test can close it.**
**Next action:** connect a demo account and re-run the reconciliation and
disconnect scenarios against it.

---

## MEDIUM

### ~~M-1 · CI is red~~ — **CLOSED at L43**

`mypy` reports **no issues in 346 source files**; `ruff check` and
`ruff format --check` are both clean. The pipeline's backend job passes.

The fixes were typing gaps rather than cosmetics. The one worth naming:
`ExecutionPipeline.safe_mode` was typed `object | None`, so mypy could not
check the two attribute accesses the pipeline makes on it. It is now a
`SafeModeLatch` Protocol exposing **only** `engaged` and `reasons` — no
`release`, no `engage`, no setter — so "execution cannot release safe mode" is
now a property of the type rather than of nobody having written the call.

Also: two `Result.rowcount` accesses given documented casts, one double
`getattr` bound once (it read the attribute twice and could in principle have
seen two different values), and an `ast.Attribute` narrowed properly instead of
via `getattr(..., "attr")`.

**Original finding, retained:**

### M-1 (original) · CI was red

**Subsystem:** CI/CD
**Impact:** the pipeline gates on `mypy` and `ruff format --check`, and both
fail — so **no commit since roughly L34 can have passed CI**. A red pipeline is
one people learn to ignore, which is how the next real failure gets through.
**Detail:** 16 `mypy` errors and 12 files needing formatting, all pre-existing
in L34–L38 code (`app/admin/service.py`, `app/notifications/service.py`,
`app/observability/collectors.py`, `app/execution/pipeline.py`) and three older
test modules. **None is in L39–L42 code.**
**Status:** OPEN, reported at L40 and again here.
**Next action:** cheapest high-value fix in the repository. It belongs to the
levels that own the files.

### ~~M-2 · `test_realtime.py` is flaky~~ — **CLOSED at L43**

**Root cause found and fixed: a connection shared across two event loops.**

The `app` fixture is `async` and runs in pytest-asyncio's loop. `ws_client` is
`sync` and drives the app through `TestClient`, which spins up its *own* loop in
a portal thread. `StaticPool` holds exactly one DBAPI connection and handed that
same connection to both — and an `aiosqlite` connection owns a thread and a
queue bound to the loop that created it. The session lookup during the WebSocket
handshake would intermittently run on the wrong loop and come back empty, which
the handshake correctly reported as "not signed in" (4401).

The fix is one line: a **file-backed** SQLite database under `tmp_path` instead
of in-memory with `StaticPool`. Each loop opens its own connection to the same
file; nothing crosses loops but bytes on disk. Per-test isolation is unchanged.

**Before: 51, 50, 50. After: 51, 51, 51, 51, 51 — five consecutive clean runs.**

Cost: the module runs in ~120s instead of ~50s. A deterministic two-minute
module is worth more than a flaky fifty-second one.

**Original finding, retained:**

### M-2 (original) · `test_realtime.py` was flaky

**Subsystem:** realtime / test harness
**Impact:** 1–2 failures per run, a different test each time. It weakens the
evidence for the WebSocket layer and it trains readers to ignore a red suite.
**Detail:** measured 51/51, 50/51, 50/51 over consecutive runs. Always a
handshake closing 4401 despite a successful register. **Not** a regression: the
L39 middleware made it worse (8 vs 2) and rewriting it as pure ASGI removed
that contribution. The application behaviour is verified independently —
**HTTP 101 through nginx against the running stack.**
**Root cause:** not established. Strongest hypothesis is the async `app`
fixture interacting with the sync `TestClient`'s own event loop over a shared
SQLite `StaticPool` connection.
**Status:** OPEN, documented in `KNOWN_TEST_LIMITATIONS.md` §5b.
**Next action:** convert `ws_client` to an async client, or give the module a
per-test engine.

### ~~M-3 · True request concurrency is untested~~ — **CLOSED at L43**

The integration fixture now runs against **real PostgreSQL** when
`TEST_DATABASE_URL` is set, so each request gets its own pooled connection and
genuine races are expressible. **The whole file passes on PostgreSQL: 37/37**,
including two concurrency tests that skip on SQLite.

Two tests, deliberately paired:

* `test_concurrent_identical_webhooks_produce_one_signal` — ten identical
  alerts delivered at once must produce **one** signal and **no 5xx**.
* `test_concurrent_distinct_alerts_each_produce_a_signal` — the control. Ten
  *different* alerts must produce ten signals. Without it the first test would
  pass just as well on an implementation that dropped everything, and a
  deduplication test without a distinctness control proves only that something
  was lost.

**No third defect was hiding behind the two found at L40.** The register warned
one might be; it is not there. That is a result, not an absence of one.

The fixture carries the same production-name guard as `test_migrations.py`: it
calls `drop_all`, so it refuses any database whose name does not contain
`test`/`scratch`/`ci`/`tmp`.

**Original finding, retained:**

### M-3 (original) · True request concurrency was untested

**Subsystem:** testing
**Impact:** the platform's duplicate-order protection is its most safety-
critical property and the harness cannot exercise it under real concurrency.
**Detail:** SQLite with `StaticPool` shares one connection, so concurrent
requests roll back each other's work. **Two real defects were found at L40 in
exactly this area, and the second only appeared after the first was fixed** —
there may be a third that one connection cannot reach.
**Mitigation:** pipeline-level concurrency *is* genuinely tested; both unique
keys have race handlers, asserted against the source; and PostgreSQL's UNIQUE
constraints are the real arbiter in production.
**Next action:** run `test_integration.py -k concurrent` against PostgreSQL.

### M-4 · The frontend is tested only against mocks

**Subsystem:** frontend
**Impact:** a renamed Pydantic field breaks the running application with **no
test failing**. All 32 frontend files mock `services.ts`.
**Mitigation:** `tsc --noEmit` over hand-written types; backend response-model
tests. Both are weaker than a contract test.
**Next action:** a schema-driven contract test, or a thin Playwright layer over
the deployed stack.

---

## LOW

### L-1 · The worker container cannot be scaled

No leader election. Two worker replicas are two monitoring workers and two
notification consumers. Duplicate work is *caught* by unique constraints rather
than prevented. Documented in `docker-compose.prod.yml`,
`DEPLOYMENT_ARCHITECTURE.md` and `PRODUCTION_RUNBOOK.md`.
**Next action:** a lease row and a renewal, if scaling is ever needed.

### L-2 · Dependencies are floors, not pins

`backend/requirements.txt` uses `>=`, so two builds a month apart can resolve
different patch versions. Frontend is locked (`package-lock.json`, `npm ci`) and
`npm audit --omit=dev` reports **0 vulnerabilities**.
**Mitigation:** every image carries its commit, so *what* is running is always
answerable even when *reproducing* it is not.

### ~~L-3 · Redis outage costs 0.5 s per request~~ — **CLOSED at L43**

A circuit breaker on `RedisEventBus`: three consecutive failures open it, it
skips for five seconds, then one probe closes it on success.

**Measured, Redis stopped, 20 sequential webhook POSTs:**

| | per request |
|---|---|
| L39 baseline | 2.06 s |
| L40 (bounded timeouts) | 0.53 s |
| **L43 (breaker)** | **0.02 s** |

The failure mode this closes was the wrong way round: the platform got slowest
exactly when it was already degraded, on the endpoint whose caller retries on
timeout.

**A skipped publish still raises**, and that is the load-bearing decision.
Returning quietly would turn "the event was not delivered" into "the event was
delivered" — the one outcome worse than being slow. Every call site already
guards `publish`; the breaker only makes an already-safe failure faster.

Verified: opens after 3 failures, skips 5 of 8, resets to 0 on a successful
probe. Four tests in `tests/test_events.py`.

**Original finding, retained:**

### L-3 (original) · Redis outage cost 0.5 s per request

Bounded at L40 from a measured 2.06 s. Correctness never depended on Redis.
**Next action:** a circuit breaker so a known-down Redis is skipped rather than
retried.

---

## INFORMATIONAL

### I-1 · `market_bars` covers one instrument; no model is deployed

Both are honestly reported (`UNKNOWN`, `NOT_CONFIGURED`) rather than defaulted.
Neither is a defect; both mean parts of the platform have never run on real
inputs.

### I-2 · No load or stress testing

No throughput, latency-under-load or memory-growth figure has been measured, and
none is claimed anywhere. The resource caps are tested to hold, not derived from
measured capacity.

### I-3 · Coverage is not measured

No `--cov` run; no percentage claimed. Test counts are counts of tests.

---

## Closed during L37–L42

Recorded because the register should show what auditing actually achieved:

| Was | Found | Fixed |
|---|---|---|
| Migrations 0024/0025 could not be applied to PostgreSQL | L40 | L40 |
| Concurrent duplicate webhook returned **500** (TradingView retries on 5xx) | L40 | L40 |
| `StaleDataError` after the rollback | L40 | L40 |
| Webhook took **2.06 s** with Redis down | L40 | L40 |
| WebSocket **dead behind nginx** (404, not 101) | L41 | L41 |
| Production overlay could not close a port | L41 | L41 |
| `Tracker.observe` erased "it was down" — **no recovery could ever be announced** | L37 | L37 |
| `Outcome.safe_mode` missing from `NO_ORDER` | L38 | L38 |
| Step-up grant burned on impossible actions | L40 | L40 |
| CORS origin with a path accepted | L40 | L40 |
| Security middleware doubled WebSocket flakiness | L41 | L41 |
| `PyYAML` undeclared — CI would fail collection | **L42** | **L42** |
| Migration tests could drop a production schema | **L42** | **L42** |

---

## Closed at L43 (post-audit hardening)

| Issue | Evidence |
|---|---|
| M-1 CI red | `mypy` clean over 346 files; `ruff check` and `ruff format --check` clean |
| M-2 realtime flake | root cause found (cross-event-loop connection); **5 consecutive clean runs** after 3 flaky ones |
| M-3 concurrency untested | integration suite **37/37 on PostgreSQL**, including a genuine 10-way race and its distinctness control |
| L-3 Redis latency | **0.53 s -> 0.02 s** per request measured, with the breaker verified to open, skip and reset |

Two defects were also fixed at L42 itself: an undeclared `PyYAML` that would
have failed CI on a clean runner, and a migration suite that would drop the
schema of any database it was pointed at.
