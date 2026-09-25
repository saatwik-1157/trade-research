# INTEGRATION_TEST_REPORT.md

Level 40, 2026-09-05.

**Headline: seven real defects found, all seven fixed.** Two had been present
since L09, two since L34/L36, and the worst of them meant **the database
migrations could not be applied to PostgreSQL at all**. Not one was reachable
from any single subsystem's test suite.

---

## Environment

| | |
|---|---|
| Mode | `TRADING_MODE=paper`, `LIVE_TRADING=false`, all 11 `LIVE_GATES` False |
| Database | in-memory SQLite per test (`StaticPool`); **Postgres 16 and Redis 7 running** on 127.0.0.1:5440/6390 for the full run |
| Broker | `FakeBroker(mode="paper")` only. **No MT5 terminal, no live adapter, no real order.** |
| Python | 3.14.2 local; 3.12-slim in the image |
| Node | vitest 4.1.11, jsdom |

`tests/conftest.py` clears `ENVIRONMENT`, `TRADING_MODE`, `LIVE_TRADING`,
`DATABASE_URL` and `REDIS_URL` before every test, so no shell environment can
change what was measured.

## Tests added

| File | Count | Purpose |
|---|---|---|
| `backend/tests/test_integration.py` | **35** | L40's cross-subsystem suite |
| `backend/tests/test_security.py` | **44** | L39's outstanding deliverable, folded in as MISSING coverage |

## Tests fixed — 14, none weakened

L39 put a step-up re-authentication gate in front of four dangerous actions.
Fourteen pre-existing tests exercised three of them and began failing.

They were updated to **satisfy** the gate — each obtains a real grant by
posting the account's password to `POST /v1/security/step-up` — rather than
being relaxed around it. That distinction is the point: a test that skipped the
gate would no longer be evidence that a legitimate operator can still get
through, which is half of what a security control has to prove.

| File | Count |
|---|---|
| `tests/test_admin.py` | 11 (deactivate, activate, revoke-sessions) |
| `tests/test_recovery.py` | 3 (safe-mode exit) |

Two tests were **added** in the same pass to assert the gate itself:
`test_releasing_safe_mode_without_re_authentication_is_refused` and
`test_entering_safe_mode_needs_no_re_authentication` — the asymmetry is
deliberate and now has a test saying so.

---

## Critical bugs found

### 1. A concurrently re-delivered webhook returned 500 — REAL BUG, since L09

`webhook_events.idempotency_key` is unique. Its insert was the **one unguarded
flush** on the accept path: the `signals.signal_key` race a few lines later was
handled and this one was not, so whichever request lost the earlier race raised
`IntegrityError` out to the error handler.

Why it matters more than an ugly log line: **TradingView retries on 5xx.** The
single condition the gateway exists to absorb — the same alert delivered twice
— was answered in the way most likely to produce a third delivery.

*Fixed*: the insert now sits in the same `except IntegrityError` shape as the
signal insert and returns a `duplicate` outcome.

### 2. `StaleDataError` after the rollback — REAL BUG, since L09

Found only **after** fixing (1), because the unguarded flush had been raising
before the session could reach this state.

Rolling the transaction back left both `event_row` and `signal` in the
session's identity map believing they were persistent, so the next flush
emitted an `UPDATE` for a row that did not exist:

```
sqlalchemy.orm.exc.StaleDataError: UPDATE statement on table 'webhook_events'
expected to update 1 row(s); 0 were matched.
```

*Fixed*: a `SAVEPOINT` (`db.begin_nested()`) scopes the event insert so its
failure does not poison the outer transaction, and both race handlers expunge
the orphaned instances.

### 3. A 2.06-second webhook when Redis is down — REAL BUG, measured

Not guessed. Timed directly:

```
before:  20 posts: 41.1s (2.06s each)
after:   20 posts: 10.5s (0.53s each)
```

`aioredis.from_url(url, decode_responses=True)` inherited redis-py's retry
defaults, so every publish spent ~2 seconds failing to connect to a refused
port before the *existing, correct* guard logged `event publish failed` and
carried on.

Correctness never depended on Redis — the signal is committed before the
publish and a failure is a warning. **Latency did, and the failure mode was the
wrong way round: the platform got slowest exactly when it was already
degraded**, on the one endpoint whose caller retries on timeout.

*Fixed*: `socket_connect_timeout=0.5`, `socket_timeout=2.0`,
`retry_on_timeout=False`, `health_check_interval=30`. The caller's guard is the
retry policy — one attempt, then carry on and say so.

*Residual*: 0.5s per request during a Redis outage, bounded and predictable. A
circuit breaker would remove it; that is a change to L07 infrastructure and is
recorded as a recommendation rather than made here.

### 4. A single-use step-up grant spent on an impossible action — REAL BUG, in L39 code

The gate ran before `set_active`'s own guards, so an admin trying to deactivate
themselves — or the last administrator — was shown *"confirm your password"*
for an action forbidden whoever they are, burned a single-use grant on it, and
only then got the real refusal.

*Fixed*: `check_set_active` extracted from `set_active`, called by the router
**before** the gate and by `set_active` itself, so there is one implementation
of the rules and no path that skips them.

### 5. A CORS origin carrying a path was accepted — REAL BUG, in L39 code

`http://localhost:3000/app` passed the new validator. A browser compares
*origins*, so an entry with a path matches nothing — and does so silently, with
the symptom appearing long after the deploy that caused it.

*Fixed*: `urlsplit`-based validation requiring scheme, host, and no path, query
or fragment. Found by a test written in the same session as the validator.

### 6. Migrations 0024 and 0025 could not be applied to PostgreSQL — REAL BUG, CRITICAL

The most serious defect the level found, and the one closest to production.

`test_migrations.py` has three tests that need `TEST_DATABASE_URL`. Nobody had
ever set it, so all three had been **skipping since L02** — the suite reported
them as skips and the aggregate looked healthy. Postgres was running for this
level, so they were pointed at it for the first time. Two of the three failed
immediately:

```
asyncpg.exceptions.UndefinedObjectError: constraint
"ck_notifications_ck_notifications_severity" of relation "notifications"
does not exist
```

The name is doubled. Migration 0002 created the constraint as
`op.f("ck_notifications_severity")` — `op.f` marks a name as *final*. Migration
0024 (L34) then dropped it **without** `op.f`, so the metadata naming
convention `ck_%(table_name)s_%(constraint_name)s` prefixed the already-prefixed
name a second time. Migration 0025 (L36) had the identical defect on
`audit_logs`.

**Why no test caught it for six levels:** the suite runs on SQLite, where
`batch_alter_table` rebuilds the table rather than issuing `ALTER TABLE ... DROP
CONSTRAINT`. A drop of a constraint that is not found is therefore silent.
PostgreSQL refuses it — and PostgreSQL is the only engine the platform is
deployed on.

**The upgrade path from 0023 to head was broken on the production engine.** A
deployment running `alembic upgrade head` against a real database would have
failed in the middle of migration 0024, leaving the schema partially applied.

*Fixed*: `op.f(...)` on the four already-final constraint names across both
migrations. One further correction came out of it — the unique constraint
`one_notification_per_event_per_user` must **not** be wrapped, because the `uq`
convention carries no `%(constraint_name)s` token and an explicitly named
unique constraint keeps its literal name. That was verified against
`pg_constraint` rather than reasoned about, after the first guess was wrong.

*Verified*: all three migration tests pass against PostgreSQL 16, including
`test_round_trip_downgrade_then_upgrade` — a full upgrade, downgrade and
re-upgrade of the entire 25-migration chain.

### 7. The security-headers middleware doubled WebSocket flakiness — REAL BUG, in L39 code

`test_realtime.py` needs Redis, so it had never run on this machine. With Redis
up it failed 7–11 tests per run, and **a different set each run**.

Bisected by disabling the new middleware: **8 flaky socket failures with it, 2
without**, over the same module. The first implementation used
`app.middleware("http")`, which wraps every request in a `BaseHTTPMiddleware` —
an anyio task group plus a memory-stream pump. This application already had two
of those, and a third was enough to make an existing timing sensitivity fire
reliably.

*Fixed*: rewritten as a **pure ASGI middleware**. It adds no task group, no
stream and no scheduling point, sets the headers on the `http.response.start`
message, and returns immediately for non-HTTP scopes — so a WebSocket handshake
never enters it at all. `test_realtime.py` then went to **51/51, repeatably**.

The residual 2 pre-existing flakes also disappeared, which is worth stating
precisely: they were not diagnosed, and they may return. What is established is
that the middleware was the dominant cause.

---

## Failure triage

Every failure seen during the level, classified per step 54:

| Failure | Class | Resolution |
|---|---|---|
| Webhook 500 on concurrent duplicate | **REAL BUG** | fixed (1) |
| `StaleDataError` after rollback | **REAL BUG** | fixed (2) |
| 2.06s webhook with Redis down | **REAL BUG** | fixed (3) |
| Step-up burned on impossible action | **REAL BUG** | fixed (4) |
| CORS origin with a path accepted | **REAL BUG** | fixed (5) |
| 14 admin/recovery tests failing | EXPECTED — new requirement | updated to satisfy the gate |
| `T0` fixed 35 days in the past | TEST BUG | freshness gate was right; test data was wrong |
| `venue.submitted` / `fail_next(...)` | TEST BUG | wrong `FakeBroker` API |
| `Outcome.submitted` | TEST BUG | the member is `order_submitted` |
| `Order(account_id=...)` | TEST BUG | columns are `broker_account_id`/`paper_account_id` |
| `Signal` imported from `models.execution` | TEST BUG | it lives in `models.signals` |
| `created_an_order` asserted False on a rejection | TEST BUG | it answers "may something exist at the venue?"; conservative True is correct |
| "password" banned in the posture output | TEST BUG | `password_hashing` is a control *name*; rewritten to check secret **values** |
| Route-introspection found 67 "unprotected" routes | TEST BUG | FastAPI dependency names are not guard names; **replaced** with real unauthenticated HTTP probes, not tuned |
| `datetime.now(` in `process` | TEST BUG | one defaulting read is legitimate; the assertion now targets `_process` |
| Header tests failing on Redis | ENVIRONMENT | moved off `TestClient` to `ASGITransport`, which runs no lifespan |
| `test_cors.py` failing on Redis | ENVIRONMENT | 4th Redis-dependent module; identified and recorded |
| Concurrent webhook test | **HARNESS LIMITATION** | `StaticPool` shares one connection; test made sequential and the limit documented |

**No test was skipped, deleted, weakened, or had its error handling disabled.**

---

---

## Results

| Suite | Result |
|---|---|
| **Backend, full** | **2338 passed, 0 failed, 1 skipped** (Postgres 16 + Redis 7 up, `TEST_DATABASE_URL` set) |
| Backend, new integration file | 35 passed |
| Backend, new security file | 44 passed |
| `test_migrations.py` against **PostgreSQL** | 3 passed — first time ever run |
| Frontend | **247 passed, 0 failed** across 32 files |
| `ruff check app tests` | clean |
| `ruff format --check` (files touched this level) | clean |
| `mypy` (files touched this level) | clean |
| `tsc --noEmit` | clean |
| `npm run lint` | clean |

**This is the first completed full backend run on this machine.** Previous
levels recorded the suite being killed by the OS for low memory; the run
completed in 22 minutes with the datastores up.

### Gates that are still red, and were before this level

* **`mypy` reports 16 errors** in L34–L38 code and older test modules
  (`app/admin/service.py`, `app/notifications/service.py`,
  `app/observability/collectors.py`, `app/execution/pipeline.py`, and three
  test files). CI gates on `mypy`, so **CI is red and was red before L39**.
  None is in code this level wrote. They are left for their owning levels
  rather than fixed here.
* **`ruff format --check .` reports 12 files** that would be reformatted, all
  untouched by this level. Same conclusion: pre-existing, and CI gates on it.

Neither was hidden and neither was "fixed" by loosening the gate.

## Critical trading test gate (step 55)

| Gate | Result | Evidence |
|---|---|---|
| TradingView webhook accepted | **PASS** | `test_a_webhook_becomes_an_order_a_position_and_a_journal_row` |
| Duplicate webhook prevented | **PASS** | 5 tests, gateway + pipeline levels |
| Invalid webhook rejected | **PASS** | 8 cases; no signal row, no order row |
| Strategy validation | **PASS** | `test_strategies`, `test_strategy_builder` |
| AI integration safe | **PASS** | `test_an_approving_ai_cannot_override_a_risk_veto` |
| RiskEngine veto | **PASS** | venue receives nothing |
| PositionSizing | **PASS** | quantity is a multiple of the venue step, within min/max |
| OMS state machine | **PASS** | `test_oms` |
| Unknown order reconciliation | **PASS** | no retry; safe mode latches; order untouched |
| BrokerAdapter in test mode | **PASS** | `FakeBroker` only |
| MT5 disconnect safe | **PASS (against a fake)** | see limitations |
| Position reconciliation | **PASS** | `test_positions`, `test_recovery` |
| No duplicate orders | **PASS** | including a genuinely raced pipeline pass |
| Paper/live isolation | **PASS** | 3 tests |
| Trade journal consistency | **PASS** | `test_trade_journal` |
| Portfolio consistency | **PASS** | `test_portfolio` |
| Analytics consistency | **PASS** | `test_analytics` |
| Bot recovery | **PASS** | `test_bots`, `test_recovery` |
| Worker recovery | **PASS** | `test_workers` |
| Notification failure isolated | **PASS** | a raising bus does not stop a trade |
| Security authorization | **PASS** | every `/v1/` write route probed unauthenticated |
| No future leakage | **PASS** | `process` reads the clock once; `_process` never |
| No destructive DB behavior | **PASS** | no `DROP`/`TRUNCATE`/`DELETE FROM` in any package |

**23 of 23.**

## Performance findings (step 56)

Only one was measured, and it produced the only optimisation made:

* **Webhook publish with Redis down: 2.06s → 0.53s.** See bug 3.
* **Residual**: 0.5s per request during an outage. Recommendation: a circuit
  breaker on the bus client so a known-down Redis is skipped rather than
  retried. Not built — it is a change to L07 infrastructure and step 56 says
  not to optimise blindly.
* **Suite wall time** is dominated by per-test app construction, not by any
  single slow path.

**Nothing else was measured, and no throughput or latency figure is claimed.**
See `KNOWN_TEST_LIMITATIONS.md` §6.

## Recommendations

1. **Run the concurrency tests against Postgres.** Two real bugs were found in
   this area and the second only appeared after the first was fixed. One
   connection cannot rule out a third.
2. **A circuit breaker on the event bus client.**
3. **A frontend↔backend contract test.** A renamed Pydantic field would break
   the running application today with no test failing.
4. **Register a real demo adapter.** It is the single largest gap in the whole
   suite and no test can close it.
