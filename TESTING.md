# TESTING.md

What is tested, how to run it, and what each level must add. Updated at every
level.

## Running the suites

```bash
# Research toolkit (unchanged)
python -m pytest tests -q          # 34 tests, ~2 s, no network, no MT5
python tests/test_indicators.py    # each file also runs standalone, as CI does

# Platform backend (L02+)
cd backend
ruff check . && ruff format --check . && mypy && pytest -q   # 150 tests, no services

# Platform frontend (L02+)
cd frontend
npm run lint && npm run typecheck && npm test && npm run build   # 52 tests
```

Counts at the end of L04 (2026-09-02): research **34**, backend **174**,
frontend **55**, all passing.

**Verified fully green, 2026-09-04**, with PostgreSQL and Redis actually
running: research **34**, backend **1417 passed, 0 failed, 0 skipped**,
frontend **95**. That is the first run in this project's history with no red
and nothing skipped, and it took getting the services up to achieve — see
"Running everything" below for what that found.

## Counts at the end of L40 (2026-09-05)

**The full backend suite completed on this machine for the first time:
2338 passed, 0 failed, 1 skipped, in 23 minutes.** Frontend 247/247 across 32
files. `tsc --noEmit`, `eslint`, and `ruff check app tests` clean.

What changed to make it possible was not code: `docker compose up -d postgres
redis`. Four modules (`test_health`, `test_realtime`, `test_errors`,
`test_cors`) need Redis, and three tests in `test_migrations.py` need
`TEST_DATABASE_URL`:

```bash
docker compose up -d postgres redis
docker compose exec postgres psql -U trade -d postgres -c "CREATE DATABASE trade_migrationtest;"
cd backend
TEST_DATABASE_URL="postgresql+asyncpg://trade:trade@127.0.0.1:5440/trade_migrationtest" \
  python -m pytest tests -q > run.txt 2>&1
```

**Run it that way.** Those three migration tests had been skipping since L02,
and when they were finally pointed at PostgreSQL they found that migrations
0024 and 0025 **could not be applied to it at all** -- see
`INTEGRATION_TEST_REPORT.md` bug 6. A skipped test reports as a pass.

### Two files added

| File | Tests | Level |
|---|---|---|
| `tests/test_security.py` | 44 | L39 |
| `tests/test_integration.py` | 35 | L40 |

### Fourteen tests updated, none weakened

L39's step-up gate broke 11 tests in `test_admin.py` and 3 in
`test_recovery.py`. Each was updated to **obtain a real grant** and proceed,
rather than to bypass the gate -- a test that skipped it would stop being
evidence that a legitimate operator can still get through.

### Seven real defects, listed in INTEGRATION_TEST_REPORT.md

The three worth remembering here: a concurrent duplicate webhook answered 500
(and TradingView retries on 5xx); a webhook took **2.06 seconds** when Redis was
down, bounded to 0.53s by giving the client a connect timeout; and the Alembic
migrations could not be applied to PostgreSQL.

### The middleware lesson

The L39 security-headers middleware was first written with
`app.middleware("http")`. Measured against `test_realtime.py`: **8 flaky socket
failures with it, 2 without.** `BaseHTTPMiddleware` wraps every request in an
anyio task group, and this application already had two. Rewritten as pure ASGI
-- no task group, no stream, and non-HTTP scopes returned immediately -- the
module went to 51/51 repeatably.

---

## Counts at the end of L38 (2026-09-05)

Frontend **247 passed, 0 failed** across 32 files; `tsc --noEmit` and `eslint`
clean; `ruff check` clean over `app` and `tests`.

**No single full backend run completed on this machine, and that is a machine
condition rather than a code one.** Two attempts were killed by the OS for low
memory -- 2.6 GB free of 15.3 -- which is the same failure this document already
records for L32 and L33. The chunked workaround below still crawls under that
pressure. What was verified instead:

| Suite | Result |
|---|---|
| `test_notifications.py` (L34) | **74 passed** |
| `test_discord.py` (L35) | **54 passed** |
| `test_admin.py` (L36) | **40 passed** |
| `test_observability.py` (L37) | **50 passed** |
| `test_recovery.py` (L38) | **44 passed** |
| `test_notifications.py` + `test_observability.py` together | **124 passed** |
| `test_admin` + `test_notifications` + `test_discord` + `test_auth` + `test_api_v1` | **346 passed, 2 failed** -> both fixed, see below |
| Regression subset: `test_execution`, `test_oms`, `test_bots`, `test_models`, `test_events`, `test_settings`, `test_errors` | **174 passed, 7 failed** -- all seven are the Redis condition below |
| `test_execution.py` after the fix below | **51 passed** |
| First 30% of `test_ai` + `test_ai_integration` + `test_analytics` + `test_api_v1` + `test_auth` + `test_auth_hardening` + `test_backtest` + `test_bots` + `test_brokers` | 0 failures before the run was stopped |

**The regression subset was chosen deliberately**: it is every module that the
five levels' changes can reach -- the execution pipeline (a new gate and a new
`Outcome`), the OMS, the bot supervisor, `EXPECTED_TABLES`, the event
catalogue, `Settings` (twelve new fields) and the error envelope.

**To run the whole thing**, on a machine with headroom and the services up:

```bash
docker compose up -d postgres redis
cd backend && python -m pytest tests -q > run.txt 2>&1   # redirect; do not pipe
```

Piping to `tail` buffers everything and hides progress until the end, which is
what made two killed runs look like hangs.

### Three Redis-dependent modules, not two

`tests/test_health.py`, `tests/test_realtime.py` and -- newly identified --
`tests/test_errors.py` all fail without Redis on 6390. The failure was read from
the traceback rather than assumed:

```
app/main.py:241: in lifespan
    await app.state.hub.start()
redis.exceptions.ConnectionError: Error 22 connecting to 127.0.0.1:6390
```

`Hub.start()` is L07 code and `TestClient(app)` as a context manager runs the
lifespan, so `test_errors.py` has needed Redis since L07. The notification
consumer and the monitoring worker start *after* line 241 and are never
reached, so none of L34-L38 is implicated.

### One defect the regression subset caught

`test_every_settled_outcome_maps_to_a_signal_status` failed because L38 added
`Outcome.safe_mode` to the enum without deciding what a signal refused by it
becomes. It is now **parked** -- `status_for` returns None, the signal stays
`new`, and a later pass reoffers it -- for the same reason `no_venue` is
parked: safe mode is a latched condition somebody resolves, and marking the
signal `vetoed` would discard it for a reason that goes away. If safe mode
outlasts the signal's validity window, the staleness check expires it, which is
the honest outcome and is what makes reoffering safe.

This is the third defect in five levels found by a completeness test rather
than by a behavioural one, and all three were the same shape: **a value added
to an enum without adding it to the table that must cover the enum.**

### Five existing assertions were updated, not worked around

Each was a status document that had drifted from the code:

  * `test_every_promised_table_exists` -- 53 to 55 (`notification_deliveries`,
    `notification_preferences`).
  * `test_every_produced_type_has_a_real_producer` -- `NOTIFICATION_CREATED`
    joins `PRODUCED_NOW`, because L34 is the producer L07 catalogued it for.
  * `test_a_full_run_persists_alerts_without_secrets` -- L29's monitoring rows
    now carry L34's severity vocabulary and a category. One column with two
    vocabularies is one column nobody can filter.
  * `test_no_route_can_create_a_notification` -- two write-shaped POSTs after
    L35, neither of which creates a notification.
  * `test_channel_status_says_configured_without_saying_how` -- Discord reports
    DISABLED rather than NOT_CONFIGURED once `DISCORD_ENABLED` exists, and the
    two are different operational facts.

### Two defects the new tests found while being written

  * **L37 `Tracker.observe`** updated its recorded state on every healthy
    observation, erasing the "it was down" that the recovery threshold counts
    against. **No recovery could ever have been announced.**
  * **L38 `Outcome.safe_mode`** was added to the enum and not to `NO_ORDER`, so
    `created_order` reported True for a refusal that sent nothing.

### Nine structural tests, asserting source rather than behaviour

A convention is forgotten; a signature is not.

  * `app/notifications` imports no risk engine, order manager, adapter, sizer
    or position manager, and its router has no POST that creates a
    notification.
  * The Discord module is imported by exactly one file: the channel registry.
  * `app/admin` imports none of the trading services and writes only user
    access; no module anywhere deletes or updates an `audit_logs` row.
  * `app/observability` imports none of them and never calls `connect`,
    `disconnect`, `reconnect`, `restart`, `start`, `stop` or an order verb.
  * `app/recovery` imports none of them, never calls `submit`, `cancel`,
    `modify`, `close` or `close_now`, never assigns `live_trading` or
    `trading_mode`, never contains `DROP TABLE` or `DELETE FROM`, and never
    names an email or Discord provider.

---

Counts at the end of L33 (2026-09-04): research **34**, frontend **168**,
backend **1995 passed, 0 failed, 1 skipped** -- 1926 passed + 1 skipped from the
main chunk and 72 from `test_webhooks.py` + `test_workers.py` +
`test_migrations.py`, with `test_migrations.py` counted in both (3 tests). Both
chunks were run after the seven fixes below, with PostgreSQL and Redis up.

**That full run is the reason to run one.** Seven failures surfaced that no
targeted run would have shown, and every one was real:

  * Four in `test_realtime.py`. The FRONTEND keeps its own copy of the event
    catalogue (`frontend/src/lib/realtime.ts` and its test), and the ten types
    added across L30, L31 and L33 were never synced to it. Those two tests exist
    for exactly this and caught it. **The two catalogues must be edited
    together.**
  * `test_every_promised_table_exists` -- 52 to 53 for `trade_reviews`. L30, L31
    and L32 added none.
  * `test_unbuilt_groups_name_their_level[/v1/portfolio/summary]` -- the 501
    stub is built now.
  * `test_realized_today_counts_only_trades_after_the_boundary` -- it built two
    trades on ONE position, which L31's `UNIQUE(position_id)` correctly forbids.
    The test was right when written; the shape it constructs became impossible
    when "one journal row per position episode" became a database guarantee. L33 added 57 review tests and 10 frontend tests. Migration 0023
was run against a real PostgreSQL, including the round-trip downgrade.

**The L32 and L33 runs were split into two chunks**, and the reason is worth
recording: on 2026-09-04 the single full run was killed twice by the OS for low
memory (3.1 GB free of 15.3, with many browser and app processes resident). It
was not a test failure either time. Splitting into
`--ignore=tests/test_webhooks.py --ignore=tests/test_workers.py` and then those
two lowers peak usage enough to finish. **Redirect pytest's output to a FILE
rather than piping it through `tail`** — a pipe buffers until the process exits,
so a killed run leaves nothing to read, while a file shows exactly how far it
got.

Counts at the end of L32 (2026-09-04): research **34**, backend **1863 + 72 across two chunks**,
frontend **158**. L32 added 91 analytics tests and 10 frontend tests, and shipped
**no migration** -- every figure is aggregated at read time.

The L32 refactor is worth its own line. `app/analytics/metrics.py` extracted the
metric definitions that existed three times, and `backtest/runner.py` and
`training/metrics.py` now call it. **98 backtest and training tests passed
unchanged**, which is the evidence the extraction moved no number -- and the
reason `compute_metrics` still returns its own `NOT_AVAILABLE` sentinel rather
than being harmonised to `INSUFFICIENT_DATA`.

Counts at the end of L31 (2026-09-04): research **34**, backend **1842 passed,
0 failed, 0 skipped** with the services up, frontend **148**. L31 added 62
journal tests and 9 frontend tests. Migration 0022 was run against a real
PostgreSQL, including the round-trip downgrade -- which is where the fourth
pre-prefixed-constraint-name defect surfaced, this time on a DROP.

Counts at the end of L30 (2026-09-04): research **34**, backend **1781 passed,
0 failed, 0 skipped** with the services up, frontend **139**. L30 added 90
portfolio tests, 8 API tests and 7 frontend tests, and shipped **no migration** —
`portfolio_snapshots` already had every column it writes.

Counts at the end of L29 (2026-09-04): research **34**, backend **1688 passed,
0 failed, 0 skipped** with the services up, frontend **132**. L29 added 42
monitoring tests and 10 frontend tests. Migration 0021 was run against a real
PostgreSQL, including the round-trip downgrade.

*(The 1700 previously recorded for L29 was an estimate written before the run
finished. The completed run reported 1688. Corrected rather than quietly
adjusted, because a test count nobody checks is exactly the kind of figure this
project treats as generated.)*

Counts at the end of L28 (2026-09-04): research **34**, backend **1646 passed,
0 failed, 0 skipped** with the services up, frontend **122**. L28 added 59
registry tests, 17 API tests and 11 frontend tests. Migration 0020 was run
against a real PostgreSQL, including the round-trip downgrade.

Counts at the end of L27 (2026-09-04): research **34**, backend **1574 passed,
0 failed, 0 skipped** with the services up, frontend **111**. L27 added 59
integration tests, 13 API tests and 9 frontend tests. Migration 0019 was run
against a real PostgreSQL, including the round-trip downgrade.

Counts at the end of L26 (2026-09-04): research **34**, backend **1502 passed,
0 failed, 0 skipped** with the services up, frontend **102**. L26 added 75
validation tests, 11 API tests and 7 frontend tests. The three PostgreSQL
migration tests were run against a real database and pass, including the
round-trip downgrade of migration 0018.

Counts at the end of L25 with the services DOWN (2026-09-04): backend **1398
passed, 15 failed, 3 skipped**. The difference is 15 Redis-dependent tests, the
3 migration tests, and one new guard test.

Counts at the end of L24 (2026-09-04): research **34**, backend **1335 passed,
15 failed, 3 skipped**, frontend **91**.

Counts at the end of L23 (2026-09-04): research **34**, backend **1269 passed,
15 failed, 3 skipped**, frontend **87**.

Counts at the end of L22 (2026-09-04): research **34**, backend **1187 passed,
15 failed, 3 skipped**, frontend **84**. The 15 are `redis.exceptions` on a
machine without Docker running and are the same 15 that failed on the pre-L18
and pre-L22 baselines — environmental, and reported as such rather than
counted as passes. Lint, format and type checks are clean on both stacks
(`ruff`, `mypy` over 202 files; `eslint`, `tsc --noEmit`).

**Run the backend checks from `backend/`, never from the repository root.**
Ruff's configuration lives in `backend/pyproject.toml`, so a run started at
the root reformats `tools/` and `tests/` as well — working research code that
is deliberately outside its scope. That happened once during L11 and was
reverted with `git checkout -- tools tests`.

## Running everything

```bash
docker compose up -d postgres redis
docker exec tr-postgres psql -U trade -d postgres -c "CREATE DATABASE trade_test"
cd backend
TEST_DATABASE_URL="postgresql+asyncpg://trade:trade@127.0.0.1:5440/trade_test" pytest -q
```

**Do this before closing any level.** Running without the services is not a
smaller version of the same check — it is a different check, and four real
defects hid in the gap between them until 2026-09-04:

1. **`positions.status` was `VARCHAR(8)`** while L21 added `partially_closed`
   (16 characters) and `reconciling` (11). SQLite ignores `VARCHAR(n)` and
   PostgreSQL enforces it, so every test passed and a partial close would have
   raised `StringDataRightTruncationError` in production.
2. **`training_runs.status` was `VARCHAR(16)`** against L25's
   `validation_pending` (18). Same defect, same cause.
3. **Migrations 0014 and 0015 passed pre-prefixed names to
   `op.drop_constraint`**, so their downgrades asked Postgres to drop
   `ck_training_runs_ck_training_runs_status`. Every migration from 0007 onward
   passes the bare name; the naming convention adds the prefix.
4. **Three `Money` columns were created `Numeric(18,8)`** where their models
   declare `Numeric(18,4)` — schema drift the `zero drift` assertion exists to
   catch, and which had never run.

`tests/test_models.py::test_every_status_value_fits_its_column` now closes the
first class permanently: it walks every `col IN (...)` CHECK against its
column's declared length, on every table.

With the services up the whole suite takes about **9m40s**, and
`test_webhooks.py` is fast because its rate limiter actually works.

**`test_webhooks.py` takes about five minutes on its own** when Redis is DOWN. Its rate-limiting
test retries against Redis, and with Docker down each attempt waits for a
connection that will not come. That is why a full run appears to hang at 95%;
check the process's CPU before concluding it stalled, and note that pytest's
stdout is block-buffered when redirected to a file, so a run between flushes
looks frozen too. `python -u -m pytest` fixes the second half of that.

Migration tests need a disposable PostgreSQL and are skipped without one:

```bash
docker compose up -d postgres
docker exec tr-postgres psql -U trade -d trade -c "CREATE DATABASE trade_test"
cd backend
TEST_DATABASE_URL="postgresql+asyncpg://trade:trade@127.0.0.1:5440/trade_test" pytest -q
```

They drop and recreate the `public` schema of that database on each run, so
point them only at a throwaway one. They assert that every promised table is
created, that models and migrations agree (**zero** autogenerate drift), that
roles are seeded before the `users.role` foreign key is added, and that
base→head→base→head round trips. Auth tests run the real routers over an
in-memory SQLite database (`StaticPool`) through `httpx.ASGITransport`, so
cookies, dependencies and role checks are exercised without a service. Frontend tests cover navigation completeness,
the API client's refusal to default on error, the mode badge's four states,
the disabled-fieldset contract of `Unavailable`, the sidebar, the dashboard's
system status against a mocked API, and the terminal page with the chart
library replaced by an inert double (jsdom has no canvas). CI runs all three: the research files on Python
3.10 / 3.12 / 3.14, the backend on 3.12 with ruff + mypy, the frontend on
Node 24 with eslint + tsc + vitest + next build.

Lint and type checking apply to `backend/` and `frontend/` only. `tools/` is
deliberately outside the ruff/mypy scope until a level moves a file; running
a linter over working research code and "fixing" it is the rewrite-for-
appearance this migration forbids.

## Backend test conventions (L02)

- `tests/conftest.py` clears `ENVIRONMENT`, `TRADING_MODE`, `LIVE_TRADING`,
  `DATABASE_URL`, `REDIS_URL` for every test and builds `Settings` with
  `_env_file=None`, so a developer's shell or `.env` cannot change what a
  test measures.
- Readiness checks are injected into `create_app()`. Tests use doubles whose
  `detail` says "test double", and two tests run the real checks against
  `127.0.0.1:1` to prove they report unavailable rather than success.
- `test_no_live_gate_is_built_at_this_level` fails if anyone flips an entry
  in `LIVE_GATES` without doing it in the level that builds the gate.

## What is covered

| File | Covers | Why it matters |
|---|---|---|
| `test_indicators.py` | RSI, MACD, ATR, ADX, Bollinger, beta, swings, Fibonacci against independent calculations | the "computed, never generated" claim |
| `test_verify.py` | the sourcing gate against a fixture snapshot | a report cannot state a number the data never produced |
| `test_cache.py` | cache sweep removes only what is past its age | bounded disk growth |
| `test_edgar.py` | fiscal period alignment against faked SEC responses | the 570% gross margin bug |
| `test_rule_backtest.py` | vectorised signals match live rules bar-for-bar; order construction; filling-mode translation; bracket sanity; fill and slippage recording; `lot_for_risk` refusals and step rounding; history windows — all against `FakeMT5` | every order-construction bug the project has had |
| `test_swap.py` | swap unit conversion, night counting, plausibility fence | financing is charged in the right units or not at all |

## What is not covered

`take_profit.py` (harvest predicate, deadline), `run_overnight.py`
(`minutes_until`), `track_record.py` (merge idempotency, regime fence,
R-multiple), `tv_webhook.py` (auth, redaction, IP filter, body cap),
`tv_import.py` (column detection, refusal without P&L), `trade_stats.py`,
`score.py` (coverage renormalisation), `patterns.py` (BH-FDR), `snapshot.py`
assembly, `market.py` fetch and validation, `crypto_market.py` unit
conversion. No integration test of place → confirm → close → merge. No test
runs on a non-UTC machine for `server_day_start`.

## Rules for every level

1. Run the suite before touching anything; record the count in
   `PROJECT_PROGRESS.md`.
2. Any change to `assert_demo`, `bracket_is_sane`, `lot_for_risk`,
   `filling_for`, `simulate` or `server_day_start` starts with a failing test.
3. New modules ship with tests against `FakeBroker`; nothing that needs a
   live terminal runs in CI.
4. Fakes are labelled as fakes in their output; a test must never be able to
   pass by a fake pretending to be the real broker.
5. Run the suite after; record the count. A drop is a stop.

## Target stack

pytest (backend, workers), Vitest and React Testing Library (frontend
units), Playwright (end to end through the API and UI). Frontend tooling is
introduced at L03; Playwright at L40.
