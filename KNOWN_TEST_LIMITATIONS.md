# KNOWN_TEST_LIMITATIONS.md

What the test suite does **not** prove. Written at L40, because a report that
lists only what passed reads as a claim about everything.

The rule this document exists to enforce: *a limitation that is written down is
a known gap; a limitation that is not is a false claim.*

---

## 1. Almost nothing has ever been told it is wrong by a real venue

The single largest limitation. It has been true since L10 and is **partly
closed as of L70b**, so read the two halves separately.

**What is now verified against a real terminal.** On 2026-09-07 the platform
sent a real order to MetaTrader for the first time and it filled: order
`00943128`, ticket `58325115749`, 0.01 EURUSD at 1.16104, stop and target
applied at the venue. Registration, connect, `assert_demo`, account read,
health, symbols, quotes, an order, a **venue rejection** (10016 INVALID_STOPS,
recorded with its reason and not retried), a fill, and a modify are no longer
taken on the fake's word. `FIRST_DEMO_VENUE_RUN.md` has the whole record.

`tests/test_mt5_demo_venue.py::test_the_real_adapter_registers_against_a_live_demo_terminal`
keeps a piece of that in the suite, and is skipped where the `MetaTrader5`
package is absent — every Linux CI runner and the API image. **CI still proves
none of it.**

**What that run found is the point.** Three defects that no amount of testing
against `FakeBroker` could have surfaced, because the fake agrees with the
platform's model of a venue *by construction*: a foreign key that made every
manual order a 500 on PostgreSQL, a zero-width bracket that made every adapter
order either rejected or opened-and-immediately-closed, and — still open — the
fact that **no broker-mode fill creates a `positions` row at all**, so the
platform cannot manage or close what it opens.

**What is still unverified.** Partial fills, requotes, slippage of any size,
disconnects, unknown-state recovery, cancels, and every symbol but EURUSD. One
order is one sample. "MT5 disconnect is safe" still means "the platform behaves
correctly when `FakeBroker.disconnect()` is called", not when a terminal
crashes.

## 1b. Foreign keys are not enforced anywhere in this suite

**SQLite has foreign keys OFF unless `PRAGMA foreign_keys=ON` is issued per
connection, and nothing here issues it.** PostgreSQL always enforces them. So
any route can write a dangling reference, pass every test, and fail on the real
database — which is exactly what `POST /v1/orders` did, through 174 API tests.

`tests/test_orders_foreign_keys.py` turns the pragma on and covers
`orders.signal_id`. **Every other foreign key is still unenforced in the
SQLite suite**, and that has not changed.

### Narrowed at P2/P4 — a PostgreSQL job now enforces all of them in CI

`tests/test_postgres_schema.py` runs against a real `postgres:16` service
and reports **105 foreign key constraints enforced**, against the 1 the
SQLite suite covers. It also closes a second gap nobody had named: the 27
Alembic revisions that build production's schema were **exercised by
nothing**, because every test calls `Base.metadata.create_all` instead. So
`create_all` and `upgrade head` were two independent descriptions of one
schema that had never been compared, and a model could drift from its
migration indefinitely while the suite stayed green.

Five checks, all passing as of 2026-09-10:

- the migrations apply to an empty PostgreSQL from nothing to head
- the stamped head matches the revision the repository declares
- **the migrations and the models describe the same schema** — tables and
  columns, compared in both directions, because a table only in the models
  never gets created and a table only in the migrations is dead weight
- PostgreSQL refuses a dangling `orders.signal_id`, which SQLite accepts
- the schema really does carry foreign keys, counted rather than assumed

**What this does NOT do.** It does not re-run the suite against PostgreSQL.
49 test files hardcode a `sqlite+aiosqlite://` URL, so pointing the whole
suite at PostgreSQL is a wider change than this and is still the real fix.
What the job does is run the checks SQLite *cannot* do, so the untested
half of the schema story is no longer untested.

These tests are not vacuous, and that was established by watching them
fail: they failed 5 of 5 on the first run, then 1 of 5, then 0 of 5, for
three separate real reasons — alembic's `env.py` calls `asyncio.run()` and
cannot be invoked from inside an async test, and it overrides
`sqlalchemy.url` from `get_settings()`, so the first version silently
migrated a different database and reported that nothing had happened.

## 2. ~~True request concurrency is not tested~~ — RESOLVED at L43

The integration fixture now uses **real PostgreSQL** when
`TEST_DATABASE_URL` is set, so each request gets its own pooled connection.
**37/37 pass on PostgreSQL**, including a genuine ten-way concurrent
duplicate test and a distinctness control that would catch
over-eager deduplication. No third defect was hiding behind the two found
at L40.

Kept because the SQLite default still has this property, and a run without
`TEST_DATABASE_URL` still cannot express a race — the two concurrency tests
skip rather than pass vacuously.

**The original limitation, which still applies to the SQLite default:**

### 2 (original) · SQLite shares one connection

`test_integration.py` runs the webhook burst **sequentially**, and the reason is
the harness rather than a choice.

The suite uses in-memory SQLite with `StaticPool` so that a test can read back
what a request wrote. That gives every request in a test the *same connection*,
and therefore the same transaction — so two concurrent requests roll back each
other's work and the test fails for a condition that does not exist on
Postgres, where each request has its own connection.

**What is tested instead:**

* Repeated sequential delivery of an identical payload — one signal, no 5xx.
* Concurrent duplicates at the *pipeline* level
  (`test_a_concurrent_duplicate_produces_one_order`), which uses no database
  session and so is genuinely concurrent.
* The presence of a race handler on **both** unique keys, asserted against the
  source (`test_the_duplicate_race_is_answered_rather_than_raised`).

That third one is explicitly a weaker test than running the race, and it says
so in its own docstring.

**To close it**, run against Postgres:

```bash
docker compose up -d postgres
DATABASE_URL=postgresql+asyncpg://trade:trade@127.0.0.1:5440/trade \
  python -m pytest tests/test_integration.py -k concurrent
```

This is not a hypothetical gap. Two real defects were found at L40 in exactly
this area (see `INTEGRATION_TEST_REPORT.md` §Critical bugs), and the second one
appeared only after the first was fixed. There may be a third behind it that
one connection cannot reach.

## 3. The frontend is not tested against the backend

All 32 frontend test files mock `services.ts`. They prove that a component
renders what the service returns and calls what it should; they do not prove
that the backend returns that shape.

The contract between them is checked in two weaker ways: `tsc --noEmit` over
hand-written types, and backend response-model tests. **A field renamed in a
Pydantic model would break the running application and no test would fail.**

No Playwright/Cypress layer exists. Adding one is a real piece of work and was
not in L40's scope.

## 4. ~~The full backend suite has never completed~~ — RESOLVED at L40

It has now, three times: **2338 passed** (L40), **2367 passed, 1 failed** (L41,
the flake in §5b). Earlier runs were killed by the OS for low memory; what
changed was starting the datastores, not the code.

Kept rather than deleted because the aggregate figures in older reports were
assembled from per-module runs and said so. Those figures were honest and are
now superseded by observed ones.

## 5. Four modules need Redis and are skipped without it

`test_health.py`, `test_realtime.py`, `test_errors.py`, `test_cors.py`. The
cause is `Hub.start()` in the lifespan and it predates L34. See
`TEST_ENVIRONMENT.md`.

## 5b. ~~`test_realtime.py` is flaky~~ — RESOLVED at L43

**Root cause: a database connection shared across two event loops.** The
`app` fixture is async (pytest-asyncio's loop); `ws_client` is sync and
drives `TestClient`, which runs its own loop in a portal thread. `StaticPool`
handed the same `aiosqlite` connection -- which owns a thread and a queue
bound to its creating loop -- to both.

Fixed by using a **file-backed** SQLite database under `tmp_path`. Each loop
opens its own connection; only bytes on disk are shared.

**Before: 51, 50, 50. After: 51, 51, 51, 51, 51.**

**The original finding:**

### 5b (original) · the flake as first measured

Measured at L41 over four consecutive runs of the module alone: **51/51, then
50/51, then 50/51**, with a *different* test failing each time. In a full-suite
run it cost one failure (2367 passed, 1 failed).

The failure is always a WebSocket test and always the same symptom: the
handshake closes with 4401 "not signed in" even though the preceding
`/auth/register` returned 201 and set a cookie.

**What is established:**

* It is **not** a regression from L39 or L41. It was measured before and after.
* The L39 security-headers middleware made it substantially **worse** — 8 flaky
  failures with `BaseHTTPMiddleware`, 2 without. Rewriting it as pure ASGI
  removed that contribution; see `INTEGRATION_TEST_REPORT.md` bug 7.
* It reproduces only under pytest. A standalone harness built the same way ran
  6/6 clean.
* The module has needed Redis since L07, so it had **never run on this machine**
  before L40 — which is why a long-standing flake was only discovered now.

**What is not established:** the cause. The strongest hypothesis is the
interaction between `test_realtime.py`'s *async* `app` fixture and its *sync*
`TestClient` fixture: `TestClient` runs its own event loop through a portal, so
the SQLite `StaticPool` connection created in one loop is used from another.
That is a test-harness defect rather than an application one, and it was not
fixed at L41 because diagnosing it properly is its own piece of work.

**Do not treat a green `test_realtime.py` as proof, and do not treat one red
test in it as a regression.** Re-run the module before concluding either.
Recorded in `FINAL_RISK_REGISTER.md` as a MEDIUM issue.

## 6. No load or stress testing was performed

Step 46 asks for it "where practical". Nothing here measures throughput,
latency under load, queue depth under burst, or memory growth over hours. The
resource limits that exist — 50 subscriptions per socket, 8 sockets per user,
4 KB frames, 64 KB webhook bodies, 200 metric series, 500 notification
recipients, 50 deliveries per pass — are **caps that are tested to hold**, not
figures derived from measured capacity.

Saying "the platform handles N webhooks per second" would require a load test
that has not been run.

## 7. Migrations — mostly RESOLVED at L40 and L41

`test_migrations.py` now runs against **real PostgreSQL** and includes
`test_round_trip_downgrade_then_upgrade`, a full upgrade, downgrade and
re-upgrade of the whole 25-migration chain. It found that migrations 0024 and
0025 **could not be applied to PostgreSQL at all**; those are fixed.

L41 went further and applied the chain to a database holding real history:
**0021 -> 0025 with 252 trades preserved**, verified by query.

**What remains untested is RESTORE, not migration.** There is no automated
backup and no restore has ever been performed, so `BACKUP_RESTORE.md` keeps its
honest RPO of *"since the last manual dump"*. That is the first blocker in
`FINAL_RISK_REGISTER.md`.

## 8. AI behaviour is a seat, not a model

Every AI test drives a stub. No provider is called, no model is loaded, and the
"AI approves / AI declines / AI is unavailable" cases are three fakes. What
*is* genuinely tested is the property that matters most — that an approving AI
cannot override a risk veto — because that is a property of the pipeline's
ordering rather than of the model.

## 9. Coverage is not measured

No `--cov` run was performed and no percentage is claimed anywhere. Test counts
are counts of tests, not of covered lines.

---

## What is NOT a limitation

Worth stating, because these look like gaps and are not:

* **`Outcome.execution_rejected` reports `created_an_order == True`.** That is
  correct: the question is "may something exist at the venue?", and the
  conservative answer for anything the platform cannot rule out is yes.
* **Safe mode parks a signal rather than vetoing it.** The signal stays `new`
  and is reoffered; if safe mode outlasts its validity window the staleness
  check expires it. That is the honest outcome.
* **`hsts` and `cookie_secure` report as not-in-force outside production.**
  They cannot be set over plain http, and reporting the correct behaviour as a
  degraded control would make the dashboard amber on every developer machine.
