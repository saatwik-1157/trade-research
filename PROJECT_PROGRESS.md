# PROJECT_PROGRESS.md

Running log of level work, newest entry last. `MIGRATION_STATUS.md` holds the
per-level status table; this file holds the narrative.

---

## Overall state (re-audited 2026-09-02)

**Levels complete: 11 of 43.** Partially complete: 21. Not started: 11.
Nothing is blocked from starting.

| Suite | Tests | Result |
|---|---|---|
| Research toolkit | 34 | pass, untouched by the migration |
| Backend | 121 | pass, ruff + mypy clean |
| Frontend | 31 | pass, eslint + tsc clean, production build succeeds |

| Area | State |
|---|---|
| Foundation, database, authentication | complete |
| Symbol mapping, position management, model monitoring | complete |
| Backend API | 7 endpoints of ~25 route groups |
| Execution path | **none** — the platform cannot place an order |
| Realtime, workers, bots | not started |
| AI models | none exist; monitoring and validation are ready for one |

**The one sentence that matters:** the six pipeline stages between a signal
and a broker — Webhook Gateway, Signal Engine, Strategy Engine, AI Layer,
Risk Engine, OMS, Broker Adapter — do not exist, so no order can be placed by
the platform. The research toolkit still trades the demo account from the
command line exactly as it did before.

**Earliest level to work next: 06 (Backend API).** The keystone is **10
(Broker Adapter)**, which seven levels wait on.

**Uncommitted:** every platform directory and document. `tools/` and `tests/`
are clean.

---

## Level 01 — Audit (2026-09-02)

- **Inspected:** every file in `tools/`, `tests/`, `.claude/`, `.github/`,
  `data/` schemas, `reports/` inventory, `CLAUDE.md`, `README.md`,
  `NIGHTLY.md`, git history, installed packages, running processes.
- **Produced:** `PROJECT_AUDIT.md`, `ARCHITECTURE_MIGRATION.md`,
  `MIGRATION_STATUS.md`, `IMPLEMENTATION_PRIORITY.md` with a 37-area
  scorecard.
- **Tests:** 34 passed before; 34 passed after (no code changed).
- **Lint / type check:** no tooling present (`ruff`, `mypy` not installed,
  no config). Recorded as L02 work.
- **Application run:** not applicable; no code changed. MT5 terminal was
  running; no trade loop was running.
- **Not done, by instruction:** no code modified, nothing deleted, no level
  implemented.

## Level 00 — Master context (2026-09-02)

- **Inspected:** the master-context brief against the audit findings.
- **Produced:** `ARCHITECTURE.md` (target flow, invariants, DEMO / PAPER /
  LIVE definitions, stack adaptations, module map, known execution-safety
  gaps), `PROJECT_PROGRESS.md`, `SECURITY.md`, `DEPLOYMENT.md`,
  `TESTING.md`; a pointer section in `README.md`; `ARCHITECTURE_MIGRATION.md`
  §1 updated for the three-mode distinction and the PostgreSQL / Redis /
  worker stack.
- **Decision recorded:** the existing "paper" (MT5 demo account) is the
  platform's DEMO mode; PAPER becomes the internal simulator; LIVE does not
  exist and stays refused in code.
- **Tests:** 34 passed (unchanged).
- **Not done, by instruction:** no code modified.

## Level 02 — Foundation (2026-09-02)

- **Inspected:** audit findings for foundation (no package, no settings, no
  API, no DB, no Redis, no Docker, print logging, no lint/type tooling); the
  development machine (Node 24, Docker 29, ports 5432–5434 and 6379–6380
  already taken by other projects).
- **Preserved:** everything under `tools/`, `tests/`, `data/`, `reports/`,
  `.claude/` untouched. Root `requirements.txt` untouched. The six original
  test scripts still run as before (34 passed).
- **Added:** `backend/` (FastAPI app, `Settings` with fail-closed
  `TRADING_MODE=paper` / `LIVE_TRADING=false` / `ENVIRONMENT=development`,
  `LIVE_GATES`, JSON logging, `/health`, `/health/ready`, async SQLAlchemy
  base, Alembic async env, ruff + mypy + pytest config, Dockerfile);
  `frontend/` (Next.js 16 + TS + Tailwind scaffold, Vitest, `lib/config.ts`,
  Dockerfile); `docker-compose.yml`; `nginx/nginx.conf`; `.env.example`;
  CI jobs for backend and frontend; `.gitignore` entries.
- **Found by running it, not by the tests:** psycopg's async driver refuses
  the Proactor event loop uvicorn forces on Windows, and `localhost` hangs
  against Docker's IPv4-only publish. Switched to asyncpg and `127.0.0.1`;
  psycopg uninstalled so one driver exists.
- **Tests:** research suite 34 → 34. Backend 0 → 16 (settings, logging,
  health; readiness proven honest against a closed port). Frontend 0 → 4.
- **Lint / types:** ruff clean, ruff format clean, mypy clean (15 files);
  eslint clean, tsc clean.
- **Application run:** uvicorn against Compose postgres/redis: `/health` 200
  in paper mode with live blocked and all ten gates listed; `/health/ready`
  200 "ready" with both checks ok. Same through the built `tr-api` container
  on host port 8000. Frontend `next build` succeeds. Compose datastores and
  API stopped afterwards; nothing left running.
- **Not done, by design:** no models, no migration, no auth, no UI beyond the
  scaffold, no worker. `tools/` not moved. Frontend image not built locally
  (npm install inside Docker; deferred to L41).

## Level 03 — UI (2026-09-02)

- **Inspected:** the L02 scaffold (one placeholder page, Geist fonts,
  Tailwind v4). Nothing else existed, so nothing was rebuilt; the fonts and
  root layout were kept and extended.
- **Added:** dark terminal theme from the validated data-viz palette
  (`globals.css`); `AppShell` with `Sidebar` (18 routes in five groups, each
  tagged with the level that makes it real and an availability dot) and
  `TopBar` (API status, `ModeBadge` PAPER / DEMO / LIVE·BLOCKED / LIVE·ARMED /
  MODE UNKNOWN, UTC clock); `Panel`, `StatTile`, `Unavailable`, `PageHeader`,
  `PlannedPage`; TanStack Query provider and `useHealth`; `SystemStatus`
  and `ConfigView` driven by `GET /health`; the Trading Terminal with
  Watchlist, Chart (lightweight-charts surface, empty), Order panel, Positions,
  Orders, P&L / Equity / Margin / Drawdown / Exposure tiles and Bot status;
  18 pages. Backend: CORS for the dev UI origin, with tests.
- **Honesty rules applied:** every figure with no measurement renders as an
  em dash with an accessible "no data" label, never a placeholder number.
  Every absent capability renders an `Unavailable` notice naming its level,
  the reason, and the CLI that does the job today. Controls under a notice
  sit in a real disabled `<fieldset>`; the order ticket cannot submit. The
  watchlist shows symbols with no prices. The chart draws no data. The mode
  badge shows MODE UNKNOWN when the API is down rather than a default.
- **Found by building it:** the order form had an event handler in a server
  component (build error; fixed with the client directive) and the disabled
  fieldset used `display: contents`, so its dimming never applied (fixed).
- **Tests:** frontend 4 → 22 (nav completeness, API client, ModeBadge,
  Unavailable, Sidebar, SystemStatus with mocked fetch, Terminal page with
  the chart library mocked). Backend 16 → 18 (CORS). Research suite 34.
- **Lint / types:** eslint clean, tsc clean; ruff and mypy clean.
- **Application run:** API on 8000 against Compose datastores, production
  `next start` on 3000; all 19 routes return 200; headless Chrome screenshots
  of the dashboard and terminal reviewed (real mode badge and gate list from
  the API; every panel labelled). Servers and containers stopped afterwards.
- **Not done, by design:** no WebSockets (L07), no charts with data (L08),
  no auth (L04), no mobile sidebar (the sidebar hides below `md`; a drawer
  is L03 polish once there is content worth it).

## Level 04 — Authentication (2026-09-02)

- **Inspected:** no authentication existed beyond the TradingView webhook's
  body secret (unchanged). Nothing to reuse; nothing rebuilt.
- **Added (backend):** `users` and `auth_sessions` tables with Alembic
  migration `0001_auth`; Argon2id hashing; server-side sessions delivered in
  an HttpOnly SameSite=Lax cookie (Secure in production); `/auth/register`,
  `/auth/login`, `/auth/logout`, `/auth/me`; `require_role()` dependency and
  a single permission table; `/admin/users` and role changes (ADMIN only,
  last admin protected); protected stubs for brokers, strategies, bots,
  orders and AI models that require TRADER and answer 501 until their level;
  `python -m app.auth.bootstrap` for the first admin; settings for cookie
  name, TTL, secure flag and registration.
- **Added (frontend):** `/login` and `/register`; `RouteGuard` on every app
  page using the role table in `lib/nav.ts`; session and sign-out in the top
  bar; API client sends credentials; `next=` redirects are same-origin only.
- **Tests:** backend 18 → 36 (register, duplicate, weak password, login
  parity for wrong password vs unknown email, logout, expired and forged
  sessions, role gates on every protected route, admin routes, role changes,
  last-admin protection, bootstrap, cookie flags by environment; all through
  the ASGI app on in-memory SQLite). Frontend 22 → 31 (route guard redirect
  and role refusal, auth form posting with credentials, off-site `next=`
  rejected, API error surfaced, weak password blocked client-side, nav role
  table).
- **Lint / types:** ruff, mypy, eslint, tsc clean.
- **Application run:** `alembic upgrade head` on Compose Postgres, admin
  bootstrapped from the environment, API started, register → me → USER
  refused on `/brokers` (403) and `/admin/users` (403) → logout → me 401 →
  admin login → promote alice to TRADER → alice on `/brokers` gets 501 (not
  built) → wrong password 401; Set-Cookie carries HttpOnly and SameSite=Lax
  without Secure in development. Downgraded to base and stack stopped.
- **Not done, by design:** rate limiting, CSRF token, password reset, email
  verification (listed in `SECURITY.md`); admin UI for roles (L36).

## Level 05 — Database (2026-09-02)

- **Inspected first:** the two tables from L04 (`users`, `auth_sessions`) and
  the three JSONL ledgers that are the real system of record —
  `paper_trades.jsonl` (497 rows: 269 order, 228 close), `track_record.jsonl`
  (252 closed trades), and `alerts.jsonl` (absent; the receiver has never run
  here). Reference figures came from `reports/track_record.json`.
- **Preserved:** `users` and `auth_sessions` keep their L04 shape. The only
  change to a pre-existing table is one **added** foreign key,
  `users.role → roles.name`. No column was dropped, renamed or retyped; no
  row was modified. The JSONL files were not written to, and their size and
  mtime are unchanged after two full import runs.
- **Added:** all 37 remaining tables from the brief in migration
  `0002_platform`, modelled under `backend/app/models/` in nine modules.
  Conventions: UUID string ids, naive UTC timestamps, `Numeric` for every
  price and quantity (never float), JSONB on PostgreSQL, and a `mode` column
  constrained to paper/demo/live on every execution-side table so simulator,
  demo and live results can never be pooled by accident. `orders.intent_id`
  is unique (the OMS idempotency key) and `orders.status` includes `unknown`
  for the IPC-timeout case whose only legal exit is reconciliation.
- **The brief's `sessions` is `auth_sessions`,** kept under its existing
  name. A rename would be a destructive change to a working table with
  nothing gained.
- **Two ordering defects found by running the migration, not by reading it:**
  the `users.role` foreign key was generated *before* `roles` had any rows,
  which fails against every existing user, so the migration now seeds the
  three roles first; and the `webhook_events ↔ signals` cycle was omitted by
  autogenerate entirely, so it is created explicitly after both tables and
  dropped first on downgrade.
- **Importer:** `python -m app.db.import_ledgers` maps the ledgers into
  `orders`, `order_events`, `executions`, `trades` and `symbols`, keyed so a
  second run changes nothing. Every imported row carries `source=jsonl_import`
  and `mode=demo`, because that is what those trades are.
- **Reconciliation, exact.** Imported 252 trades, 269 orders, 485 order
  events, 208 executions. Against `reports/track_record.json`: trades 252,
  R-multiples 248, mean R −0.0058, net −22.37 — all four match. A mismatch
  exits non-zero rather than warning. The second run imported 0 and skipped
  521.
- **Noted for L11:** the order log contains `DE40` (1 digit) beside 5-digit
  FX pairs. That is the unit-mismatch hazard the research harness already
  fences; the symbol mapping level must carry point size and unit class, and
  `symbols` now has both columns.
- **Tests:** backend 36 → 45. New: every promised table exists and metadata
  matches; roles seeded and referenced; order/execution/trade round trip;
  importer mapping and idempotency; reconciliation passes on a matching
  reference and fails on a mismatched one; and three migration tests against
  real PostgreSQL (upgrade creates all tables with **zero** autogenerate
  drift, roles precede the users foreign key, and base→head→base→head round
  trips cleanly). Migration tests skip unless `TEST_DATABASE_URL` is set.
- **Lint / types:** ruff, ruff format and mypy clean (47 files).
- **Not done, by design:** no API reads these tables yet (L06); no seeded
  symbols beyond those the import created; `alerts.jsonl` has nothing to
  import.

## Level 11 — Symbol mapping (2026-09-02)

Taken out of order at the owner's request. Levels 06–10 remain unstarted.

- **Inspected first:** `tools/rule_backtest.SYMBOLS` (a seven-name string),
  `tools/universe.txt`, the MT5 spec fields the toolkit already reads
  (`trade_tick_value`, `trade_tick_size`, `volume_step/min/max`, `point`,
  `digits`), and `tools/swap.py:value_per_point`, which is the existing proof
  that a tick is not a point. Nothing was duplicated; the L05 `symbols` and
  `symbol_mappings` tables were extended.
- **Decision worth stating:** contract specs are **broker**-specific, not
  properties of the instrument, so they live on the mapping row rather than
  on the shared symbol. Two brokers quote different tick values and different
  minimum lots for the same pair, and one may not list it at all.
- **Three names, none derived from another:** `source_symbol` (what a
  provider sends), `internal_symbol` (canonical), `broker_symbol` (what the
  broker lists). Resolution is always a table lookup; a miss raises rather
  than falling back to the input string.
- **Added:** migration `0003_symbol_specs` (all additive: twelve nullable
  columns plus `is_active`, added with a server default then dropped so
  existing rows backfill safely); `app/symbols/` with typed errors, a
  resolution and spec service, an idempotent seed, and `sync_mt5` to read
  specs from a live terminal.
- **Specs are refused, never defaulted.** `contract_spec()` names exactly
  which fields are missing and tells the caller to sync rather than assume.
  This is `lot_for_risk`'s existing rule moved to where the data lives.
- **Real specs, read from the running terminal** (nine symbols, none
  invented). They justify the whole level: `DE40` has contract size 1 against
  100,000 for the pairs, minimum volume 0.1 against 0.01, and tick value
  0.0116 against 1.0. A flat 0.01 lot on DE40 is **below the broker's
  minimum** and would be rejected. Tick values differ across the majors too
  (USDJPY 0.6297, USDCAD 0.7213, USDCHF 1.2301), so a "point" is not one
  quantity even within FX. My seeded guess of 1 digit for DE40 was wrong; the
  terminal says 2, and the synced value is authoritative.
- **Trading hours are null, and that is a finding.** MetaTrader5 build
  5.0.6090 exposes no per-weekday session API, and the `session_*` fields on
  `symbol_info` are today's turnover statistics rather than a schedule. The
  sync reports this as a note; `read_sessions` returns None, which means "no
  schedule known" and never "open around the clock". A broad `except` had
  been hiding the difference between an absent API and an absent session.
- **Found by a failing test:** a bare `{{ticker}}` could not reach an
  exchange-qualified mapping. The resolver now falls back from `EURUSD` to
  `OANDA:EURUSD`, but only when exactly one instrument matches; two exchanges
  listing the same ticker raise `AmbiguousSourceSymbol` rather than routing an
  order to whichever sorted first.
- **Tests:** backend 45 → 73. Unknown source symbol, unknown internal symbol,
  missing broker mapping, invalid and empty codes, unknown provider, unknown
  mapping field, inactive symbol and mapping, duplicate mapping refused,
  exact match beating the stripped fallback, ambiguity refused, spec refused
  before sync, partial spec naming only what is missing, spec per provider,
  zero read as missing, and session hours absent versus present.
- **Lint / types:** ruff and mypy clean. Numeric columns are now annotated
  `Decimal` rather than `object` across every model; schema drift stayed zero.
- **Mistake made and corrected:** one command ran from the repository root
  instead of `backend/`, so ruff reformatted 30 files under `tools/` and
  `tests/` — code that is deliberately outside its scope. The changes were
  cosmetic only and were reverted with `git checkout`; the research suite
  passes at 34 and the working tree there is clean.

## Level 21 — Position management (2026-09-02)

Taken out of order at the owner's request. Levels 06–10 and 12–20 remain
unstarted.

- **Dependency stated up front:** there is no broker adapter (L10) and no OMS
  (L19), so nothing here can reach a real venue. What this level delivers is
  the decision engine, the paper executor, and the close contract a broker
  adapter will have to satisfy. Demo and live are wired to an executor that
  returns REJECTED with a reason and can never return a confirmation.
- **Inspected and adapted, not duplicated:** `tools/take_profit.net_floating`
  (floating P&L must include swap, or an overnight loser books as a win) and
  `rule_backtest.simulate()`'s rule that a bar covering both the stop and the
  target books the loss. A live monitor sees the same ambiguity on every
  poll, so it resolves it the same way.
- **Seven exit types**, evaluated in a fixed priority: emergency, risk, stop
  loss, take profit, trailing, time, strategy. Emergency and risk outrank
  price because they are about the account surviving rather than this trade's
  merits, and the stop precedes the target for the both-hit rule.
- **The trailing stop never closes anything.** It moves the stop and lets the
  stop policy fire, so exactly one place decides a stop exit. It ratchets only
  and refuses to act without an ATR when configured in ATR multiples. Its
  docstring records that this repository measured trailing exits at D1 as
  *worse* than a fixed bracket, so its presence is not a recommendation.
- **Nothing is closed without confirmation.** The executor contract is
  CONFIRMED, REJECTED or UNKNOWN. Only a confirmation carrying a fill price
  closes a position. A confirmation *without* a fill price is downgraded to
  unknown. An unknown parks the position in `status='unknown'`, records
  "reconcile; do not retry", and the manager refuses to touch it again — a
  retry could double-close, and treating it as failed could leave a position
  nobody is watching.
- **Every modification recorded** to `position_events`: stop moves, exit
  decisions, closes, rejections and unknowns, with the decision's reference
  price stored separately from the venue's fill.
- **Monitoring runs in the backend**, not the browser, with an injected clock
  and stop event so tests are deterministic. A failing pass is logged and the
  loop survives, because a monitor that dies stops watching.
- **A schema change with justification**, which the rules otherwise forbid:
  `position_events` was recreated with an autoincrementing integer primary
  key. A UUID orders nothing and `occurred_at` cannot order it either, since
  a decision and its fill share the same instant by construction. A log that
  cannot be replayed in write order is not an audit log. The table was empty
  in every environment, the migration **refuses to run if it ever is not**,
  and the downgrade restores the old shape exactly.
- **Tests:** backend 73 → 98. Every policy for long and short, both-levels-hit
  booking the loss, risk exit refusing to act on an unknown P&L, emergency
  outranking a profitable target, trailing ratchets that never loosen in
  either direction, ATR trailing without an ATR, confirmed/rejected/unknown
  close handling, confirmed-without-fill downgraded, no retry after unknown,
  sweeps skipping positions without a quote and respecting mode, and a
  monitor that survives a failing pass.
- **Two defects found by running the tests:** event timestamps were being
  written into the JSON payload where they cannot serialise (they belong in
  the column), and SQLite only autoincrements a column declared exactly
  `INTEGER PRIMARY KEY`, so the key needed a dialect variant.
- **Verified:** zero schema drift, migration round trip clean, and the L05
  imported data intact (252 trades, 269 orders, 9 symbols, 18 mappings).

## Level 29 — Model monitoring (2026-09-02)

Taken out of order at the owner's request.

- **The honest starting point:** no model exists. Levels 23–28 are unstarted
  and this project has never had one, so there is nothing live to watch.
  `ModelMonitor.run` says exactly that and returns an empty report whose note
  reads "this is a statement about the registry, not a clean bill of health".
  Nothing invents a prediction, a feature or an outcome.
- **Eight checks**, each returning a finding: feature drift, prediction drift,
  prediction distribution, feature distribution, performance, calibration,
  trade outcomes, market regime.
- **`insufficient_data` is its own severity**, distinct from `ok`, and it is
  never actionable. "We did not measure this" and "this is fine" are different
  claims and only one is safe to act on. Every check refuses below 100
  observations, the floor this project already uses for win rates.
- **Multiple testing is corrected.** Monitoring twenty features is twenty
  hypotheses, so about one clears p<0.05 per run with nothing happening.
  Benjamini-Hochberg is applied across features and a feature that fails
  correction is demoted to `ok` with the reason in its summary.
- **No second implementation.** The BH-FDR procedure already exists in
  `tools/patterns.py`. The backend restates it (it cannot import the toolkit's
  dependencies) and a test imports both and asserts they agree over 50 random
  inputs, so the copies cannot drift apart silently.
- **PSI bands are labelled as convention.** The 0.10 and 0.25 thresholds were
  chosen, not measured here, and every summary line says so.
- **The escalation ladder tops out at "validate".** A serious model-side check
  escalates to retrain and revalidate; a regime shift does not, because that
  is the market moving rather than the model breaking. `FORBIDDEN_ACTIONS`
  lists promote, deploy, replace and rollback, and a test enumerates every
  check and severity to prove none of them can be emitted. Every alert body
  states that no model is promoted, deployed or replaced.
- **Alerts persist** to the `notifications` table from L05, grouped by
  severity band, carrying the findings and their FDR verdicts.
- **Tests:** backend 98 → 121. PSI zero on identical samples and rising with a
  shift, PSI catching an abandoned tail, KS separating shifted from identical,
  calibration separating calibrated from overconfident, every check refusing a
  tiny sample, FDR leaving exactly the one genuinely shifted feature flagged,
  a collapsed prediction distribution caught as critical, performance flagging
  a fall but not an improvement, a losing run surfaced on a smaller floor, the
  ladder's ordering, and the no-replacement guarantee.
- **Also written:** `COMMANDS.md`, the permanent command reference you asked
  for, including a table of the safety invariants and which are still unbuilt.

## Next

Level 06 — Backend API. Unstarted: 06–10, 12–20, 22–28, 30–42.

## Level 01 — Audit, re-run (2026-09-02)

- **Scope:** full re-audit of the repository as it now stands, superseding the
  original Level 01 audit which described it before any platform code existed.
- **Read-only.** No application code was modified. Verification runs only:
  research 34, backend 121, frontend 31, plus ruff, ruff format, mypy, eslint,
  tsc and a production build, all clean.
- **Prompt files still absent.** `claude-prompts/00_MASTER_CONTEXT.md` and
  `claude-prompts/01_AUDIT.md` are not in the repository or anywhere on this
  machine. The audit is written against their pasted contents, recorded in
  `ARCHITECTURE.md`.
- **Rewritten:** `PROJECT_AUDIT.md` (30 audit areas, technology stack,
  modules, integrations, database, problems, duplicates, security, trading
  safety, recommended approach), `ARCHITECTURE_MIGRATION.md` (current and
  target architecture, per-component decisions with reasons, architectural
  decisions, dependency graph), `MIGRATION_STATUS.md` (all 43 levels with
  status, existing, missing, required changes, dependencies, priority),
  `IMPLEMENTATION_PRIORITY.md` (critical/high/medium/low), and this file's
  overall-state header.
- **Headline findings:** the platform cannot place an order, because six
  pipeline stages between signal and broker do not exist. Four `connect()`
  implementations are the worst duplication. Redis runs and nothing uses it.
  No component is scheduled for removal.
- **Not done, by instruction:** no level implemented.

## Level 02 — Foundation, verified and extended (2026-09-02)

- **Decision on which level to work.** The audit marked L02 complete against
  the nine-item checklist pasted earlier. The fuller foundation prompt lists
  eighteen items, and seven were genuinely absent: error handling, request
  ids, API structure conventions, background worker infrastructure, realtime
  infrastructure, development scripts and three-state health checks. So
  Foundation was **not** complete against this specification. Nothing was
  rebuilt; only the gaps were closed.
- **Baseline before changes:** research 34, backend 118 passed with 3 skipped
  (migration tests need a database URL), frontend 31.
- **Verified as already complete and left alone:** frontend application,
  backend application, PostgreSQL integration, Redis container, Docker and
  Compose, environment configuration, centralised settings, structured JSON
  logging, testing infrastructure, documentation.
- **Added:** `core/errors.py` (exception hierarchy, one response envelope,
  request-id middleware, four handlers); `core/events.py` (event envelope with
  validation and size cap, Redis and in-process buses behind one protocol,
  each labelled by kind); `workers/base.py` (worker loop with heartbeat,
  cooperative shutdown, registry); `/health/live`.
- **Modified:** `core/health.py` now returns three states with criticality per
  check, so the system is never healthy while a critical dependency is down;
  `main.py` wires the handlers, middleware, event bus and worker registry, and
  shuts them down in order; `settings.py` gained `events_enabled`.
- **Refactored, removing a duplicate:** `PositionMonitor` carried its own copy
  of the worker loop from L21. It now subclasses `Worker`. Its 25 tests pass
  unchanged.
- **Contract change, deliberate:** errors are now `{"error": {code, detail,
  request_id}}`. Three auth assertions were updated to the new shape; no
  behaviour changed.
- **Tests:** backend 118 → 150 without services, 153 with PostgreSQL. New:
  8 error tests, 10 event tests, 7 worker tests, 6 health tests.
- **Lint / types:** ruff, ruff format and mypy clean across 73 files; eslint,
  tsc clean; frontend build succeeds.
- **Application verified by killing dependencies, not by mocking them.** With
  everything up: healthy, HTTP 200, all three checks reporting. Redis stopped:
  **degraded, HTTP 200**, `critical_unavailable` empty, because the API works
  without it. PostgreSQL stopped: **unavailable, HTTP 503**, database named.
  Request ids echoed, including a caller-supplied one. Redis event bus
  round-tripped a real message through the running server.
- **Security checks:** no credential in any source file, no `.env` present and
  it is gitignored, zero credential-bearing log lines, and validation errors
  do not echo submitted values.
- **Trading safety:** `TRADING_MODE=paper` and `LIVE_TRADING=false` unchanged,
  all ten live gates still false, `/health` lists 12 blockers. No broker was
  contacted and no order path exists.
- **Deferred with reason:** a development task runner (no `make` on this
  Windows host; `COMMANDS.md` documents the commands) and the dev/prod Compose
  split, both of which belong to L41. The WebSocket layer and the event
  catalogue are L07; this level built only the transport they need.

## Next

Level 06 — Backend API.

## Level 03 — UI, completed (2026-09-02)

- **Audit first.** The shell, routing, theme, auth UI, `Unavailable`,
  `ModeBadge`, `Panel`, `StatTile`, `PageHeader`, `PlannedPage`, the chart
  surface and the TanStack Query setup all existed from the earlier L03 pass
  and were **kept unchanged**. lightweight-charts was already a dependency, so
  no second charting library was added.
- **Added, as one implementation each:** a design system barrel
  (`components/ui/`) with Button, Badge, Input, Select, Field, DataTable,
  ConfirmDialog, StatusDot and the loading/empty/error states. The barrel
  re-exports the pre-existing components rather than replacing them, so a
  second Button cannot quietly appear beside the first.
- **Service layer (`lib/services.ts`).** Components no longer call `fetch`.
  Every domain the platform will have is declared with its real shape, and a
  domain whose backend does not exist returns an explicit refusal naming its
  level. It never returns empty data, because "no positions" and "no positions
  endpoint" must not look the same on screen. `orderService.submit()` has no
  success path at all — a test asserts the returned shape contains nothing
  that could be read as a fill.
- **Realtime abstraction (`lib/realtime.ts`).** All 15 event types, a typed
  subscription API, exponential backoff capped at 30 s, and frame validation
  that drops an unknown type rather than coercing it. It starts in state
  `unknown`, not `disconnected`: no connection has been attempted, so claiming
  one is down would be a claim we cannot make. The backend endpoint is L07 and
  `connect()` is never called automatically.
- **Terminal upgraded:** symbol and timeframe selectors, a watchlist with
  bid/ask/last/change/spread/market-status columns, an order ticket with
  market/limit/stop, risk-reward and estimated risk, and a confirmation dialog
  that states plainly that nothing will be transmitted. Positions and orders
  tables carry every field the brief lists, with all eight order statuses
  rendered as badges.
- **Pages added:** Markets, Positions, Orders, System Monitoring, Strategy
  Builder. 24 routes now build.
- **Service health across eight services** with CONNECTED / DEGRADED /
  DISCONNECTED / UNKNOWN. A service the backend does not report is UNKNOWN,
  never DISCONNECTED, and when the API itself is unreachable every dependency
  below it reads unknown rather than healthy.
- **Found by testing:** the watchlist was hiding its rows behind a loading
  state, but the instruments are static and only prices load — it now always
  shows the symbols with em-dash prices. The confirmation dialog needed a
  jsdom fallback because jsdom implements `<dialog>` but not `showModal`.
- **Tests:** frontend 31 → 52. New: realtime catalogue and backoff, service
  refusals, the design system primitives, and five terminal assertions
  including "never claims a price it does not have".
- **Verified running:** all seven new and changed routes return 200 against
  the live API. An unauthenticated request to `/terminal` redirects to sign-in,
  which is the route guard working; terminal content is verified by the
  component tests because the guard is client-side.
- **Security:** no browser storage is used anywhere (zero files reference
  `localStorage` or `sessionStorage`), the only public variable is
  `NEXT_PUBLIC_API_URL`, and no credential appears in frontend source.
- **Trading safety:** paper mode and live-trading-false unchanged, ten gates
  still false, no order path exists, and nothing in the UI can claim a fill.
- **Remaining:** panels bind to real data as their levels land; no mobile
  drawer; Playwright is L40.

## Next

Level 06 — Backend API.

## Level 04 — Authentication, hardened (2026-09-02)

- **Audited before building.** One implementation of each auth function, no
  duplicates, no JWT, no OAuth. Cookies already carried HttpOnly, SameSite=Lax
  and Secure-in-production; CORS already listed explicit origins with no
  wildcard; no hardcoded credential anywhere in source. All of that was
  **kept unchanged**.
- **Four genuine gaps closed**, and nothing working was replaced: rate
  limiting, CSRF, audit writes, and password reset. The `audit_logs` table had
  existed since L05 with nothing writing to it.
- **Added:** `auth/ratelimit.py` (two backends, in-process and Redis),
  `auth/csrf.py` (double-submit middleware), `auth/reset.py` (token
  mechanics plus a delivery port), `core/audit.py` (scrubbing writer), 15
  named permissions, and migration `0005_auth_hardening`.
- **Found by testing, and it mattered:** the CSRF middleware initially blocked
  sign-in whenever a stale session cookie was present, which breaks the
  ordinary act of logging in again. Session-*establishing* endpoints now sit
  on an explicit exempt list with the tradeoff written down.
- **Reset delivery is not built and says so.** The only `ResetDelivery`
  implementation raises, naming L34. A token is created and nothing is sent,
  so a user cannot currently complete a reset. The token is never returned in
  a response and never logged, because a delivered-by-logging token is a token
  in a log file.
- **Migration:** `users.updated_at` added with a backfill default that is then
  dropped, `users.last_login_at`, and `password_reset_tokens`. Zero drift,
  clean round trip, and 252 trades / 269 orders / 9 symbols preserved.
- **Tests:** backend 150 → 174, frontend 52 → 55. New coverage: no dangerous
  permission for a new account, role cumulativity, limiter blocking with a
  retry, brute force throttled end to end, CSRF refused without the header and
  accepted with it, safe methods exempt, every security action audited, the
  scrubber at any depth, a failed login never recording the password, reset
  indistinguishable for unknown accounts, token absent from responses,
  single-use with all sessions revoked, expired and forged tokens refused,
  last login on success only, and the password reveal toggle.
- **Verified against the live API:** logout without the header 403 and with it
  204; ten failed logins then 429; reset 202 for both known and unknown
  addresses with no token in the body; audit trail showing 10 failed logins,
  2 rate-limit trips, register, logout and 2 reset requests; **zero audit rows
  and zero log lines containing a password**.
- **Trading safety:** paper mode and live-trading-false unchanged, ten gates
  still false, no order path exists, no broker contacted.

## Level 06 — Backend API, completed (2026-09-03)

- **Audited before building.** One entry point, one error envelope, one
  request-id middleware, one session mechanism, no duplicate route. All
  **kept**. The existing surface was `/health` x3, `/auth/*` x7,
  `/admin/users` x2 and five stubs answering 501. Nothing was rebuilt.
- **Versioning: `/v1` on the application, `/api/v1` in the browser.** nginx
  serves the API under `/api/` and strips the prefix, and
  `NEXT_PUBLIC_API_URL` has always been the API root, so mounting `/api` in
  the application too would mean `/api/api/v1` behind the proxy or a rewrite.
  Neither buys anything. **No nginx or Compose change was needed.**
- **Nothing was broken to do it.** `/auth/*` and `/admin/users` are mounted at
  both their original paths and under `/v1` — the *same router objects*, so
  there is one implementation behind two paths, with the originals hidden from
  the schema and marked deprecated. A test asserts both paths give the same
  answer, so an alias cannot quietly grow a second implementation.
- **`/health`, `/health/live` and `/health/ready` deliberately stay at the
  root.** They are an infrastructure contract shared with the Compose
  healthcheck and nginx. `/v1/system/health` reads the same check functions:
  two doors, one answer, asserted by a test.
- **What is served is what exists.** 269 orders, 208 executions, 252 trades
  and 9 symbols were already in the database from L05, so orders, executions,
  trades, positions, symbols, contract specs, accounts and the audit trail are
  **real reads**. Verified live: `/v1/orders` returns 269, `/v1/trades?result=win`
  returns 219 of 252, `/v1/symbols` returns all 9, and `/v1/symbols/DE40/spec`
  returns contract size 1 and minimum volume 0.1 against EURUSD's 100,000 and
  0.01 — the measured difference the audit records.
- **The other 15 route groups are declared, gated and 501.** Each names the
  level that builds it, from behind its real authorization gate, because a 501
  behind a 401/403 is honest and a missing route teaches a caller nothing.
  25 tag groups are now documented in OpenAPI against 5 stubs before.
- **`/orders` is the one path that moved.** It answered "not built until level
  19", and that stopped being true: the order record is real. What is still
  unbuilt is order *submission*, which is `POST /v1/orders` and says so there.
  The only consumer was the test suite; nothing in the frontend called it. The
  other four aliases stay because their groups are still entirely unbuilt.
- **Found by testing, and it was a real latent bug.** `app/core/errors.py` has
  documented since L02 that `app.symbols.errors.SymbolError` is "mapped, not
  replaced" — and no handler was ever registered. It was unreachable because
  nothing called the symbol layer over HTTP. The first route that did turned a
  deliberate refusal ("this spec is missing tick_value") into a 500, which
  reports the layer working correctly as a fault in the server. A handler now
  exists with stable per-class codes, and `IncompleteContractSpec` answers the
  409 `app.symbols.errors` always specified.
- **Pagination refuses rather than truncates.** `limit` defaults to 50 and
  caps at 200, and a caller asking for 10,000 gets a 422 naming the maximum —
  not a page of 200 that reads as "that is all there is", which for a trade
  history is a false statement about the record. `total` is computed over the
  filtered statement, so it describes the set asked for rather than the table.
- **No query string can reach SQL.** Sort and filter fields are keys into a
  dict of ORM columns; an unknown key is a 422 that names what is sortable.
  Verified live: `?sort=id;drop table orders--` returns 422 listing the four
  sortable fields.
- **Refusals are specific.** Filtering on an unknown symbol is a 404, not an
  empty page: "no trades on XYZUSD" and "there is no such symbol" are
  different statements and the caller is told which one they hit.
- **Idempotency is a contract, not a pretence.** `Idempotency-Key` is
  validated for shape now and recorded at L19, where `orders.intent_id` — which
  is unique and still unused — makes a replay a conflict rather than a second
  order. A malformed key is refused now, because accepting a key the OMS could
  not store would let a caller believe in replay protection that does not
  exist. Nothing is stored today and the code says why.
- **Permissions became the gate.** `require_permission` joins `require_role`,
  which is what `app/auth/permissions.py` was designed for ("a route asks for
  a permission, not a role"). Refusals name the permission, so the message
  stays true when the table changes.
- **Security:** no hardcoded secret, no raw SQL, no string-built ORDER BY, no
  eval/exec/pickle, no debug endpoint, CORS still an explicit origin list with
  no wildcard. `BrokerAccountOut` omits `login` — an account number is a
  credential-shaped identifier and the cheapest way to never render it is to
  leave it out of the shape. A test walks every module under `app.api` and
  asserts none reaches MetaTrader5 or the order sender.
- **Trading safety verified live.** `TRADING_MODE=paper`, `LIVE_TRADING=false`,
  all ten gates false, `/health` lists 12 blockers. `POST /v1/orders` with a
  valid key and the `submit_orders` permission returns 501; sent twice it
  returns 501 twice; and the row counts afterwards are unchanged at 269 orders,
  208 executions, 0 positions, 252 trades. No broker was contacted.
- **Tests:** backend 174 → 227 (224 passed, 3 skipped), frontend 55 unchanged,
  research 34 unchanged. New file `tests/test_api_v1.py` with 47 cases plus 4
  in `test_auth.py`, covering L06's own list: valid request, invalid request,
  missing authentication, insufficient permission, invalid resource id,
  duplicate submission, error handling, pre-v1 compatibility, health, and a
  duplicate-route assertion over the whole route table.
- **Verified running:** every figure above was read from a live API on
  127.0.0.1:8011 against the real PostgreSQL, not from a test double.
- **Deferred with reason:** binding the UI panels to the new endpoints. The
  frontend types are camelCase and the API is snake_case, so binding needs a
  mapping layer — that is L03/L19/L21/L31 panel work, not API architecture.
  The service layer's reason strings were corrected instead, since two of them
  had become false. Async job endpoints (backtests, training) are L14/L25;
  they have their route and their 501.

## Level 07 — Realtime, completed (2026-09-03)

- **Audited before building, and most of it existed.** `core/events.py` (the
  Event envelope, `RedisEventBus`, `InMemoryEventBus`, the 256 KB size cap),
  `core/redis.py`, `workers/base.py` (loop, heartbeat, registry, cooperative
  stop) and the frontend `lib/realtime.ts` (backoff, frame validation, typed
  handlers) were all **kept**. No second bus, no second worker loop, no second
  client. What was missing was the catalogue, the channels, the server and the
  authorization.
- **The event catalogue is a contract, not an implementation.** 29 types with
  a scope each. Most have no producer — there is no OMS to emit ORDER_FILLED —
  and `producing_now` says so per type, because "subscribed and silent" and
  "subscribed to something that cannot happen yet" look identical from a
  browser. **Only `SYSTEM_ALERT` is produced today.** Publishing a fabricated
  ORDER_FILLED to demonstrate the transport would put a fill on the bus that
  no venue reported, which is the confusion the architecture exists to prevent.
- **The catalogue decides the channel, not the publisher.** Every ORDER_*,
  POSITION_* and RISK_* type is account-scoped by definition, so a private
  fill cannot be routed onto `system` by a caller who passed the wrong
  channel. A test asserts the scoping across all of them.
- **Channel authorization asks the database, never the frame.** Changing the
  id in a subscribe frame is the cheapest attack on a system like this. An
  unowned account and a non-existent one return *the same message*, because
  telling an unauthorized caller that an id exists is a membership oracle. A
  refusal is per channel: asking for four and holding three grants three and
  names the one refused, rather than closing the socket.
- **Ownership is re-read on every subscribe, never cached on the connection.**
  A socket outlives a role change, and a long-lived connection must not
  outlive the permission that opened it.
- **Authentication runs before `accept()`.** The same session cookie the REST
  surface uses — no second token mechanism. Measured both ways: a real client
  sees the handshake rejected with HTTP 403; the test client surfaces close
  code 4401. Accepting first so the 4401 were visible would create exactly the
  unauthenticated window the design forbids, so the handshake rejection stands
  and the docstring now says which client sees what.
- **There is no publish verb.** Subscribe, unsubscribe and ping. A browser
  that could publish onto the bus could publish ORDER_FILLED, and something
  downstream would eventually believe it. A test sends a publish frame and
  asserts the socket closes.
- **Found by testing, and it was a real bug in L02 code.** `Hub.start()`
  created the reader task and returned; the bus subscription was only
  registered on the generator's first `__anext__`. Anything published in that
  gap was silently dropped — and a dropped event is indistinguishable from an
  event that never happened. Both buses now register **before** `subscribe()`
  returns (which also matches the `EventBus` Protocol, which always declared a
  plain `def`), and `Hub.start()` awaits it. `tests/test_events.py` updated
  for the changed call shape.
- **A slow client is dropped, not buffered.** 256 frames of slack, then the
  connection is closed with a reason. An unbounded queue behind a stalled
  socket is a memory leak that ends the process, and taking the API down to
  keep one browser tab updated is the wrong trade.
- **Idempotency on both sides.** `SeenEvents` in Python and TypeScript, both
  bounded and both documented as the cheap first line rather than the only
  one: an id that ages out would be reprocessed, so a consumer that must never
  act twice also checks its own domain state. Redis pub/sub can redeliver to a
  reconnecting subscriber, and the client replays its subscriptions on
  reconnect, so redelivery is expected rather than exceptional.
- **Redis became critical, as `core/health.py` said it would at L07.** The bus
  now carries the events that say an order changed state, and a process that
  cannot publish them has observers who are silently blind. The criticality is
  about *reporting*: nothing downstream treats a bus failure as permission to
  trade, and the OMS will reconcile against the broker, never against the bus.
- **Frontend:** 15 → 29 event types (a backend test asserts the two lists are
  the same set, so a type added on one side is a failure rather than a frame
  silently dropped), plus `subscribe`/`unsubscribe`, control-frame parsing, a
  client heartbeat, dedupe, and replay of subscriptions on reconnect. The
  requested set is explicitly a request, not a grant.
- **Observability:** connections opened and closed, events published,
  delivered and dropped, publish failures and subscribe refusals. Counts only,
  never payloads. `bus_healthy` reports what the last publish actually did
  rather than whether a URL is configured.
- **Verified live**, not only in tests, against the running API on
  127.0.0.1:8011: anonymous handshake refused (HTTP 403); ping answered;
  `system` granted and `account:not-mine` refused **on the same frame**;
  unsubscribe honoured; a publish frame closing the socket with 4400; and the
  hub reporting `bus=redis, reader_running=true` with one connection and one
  recorded subscribe refusal. **Cross-process delivery was proved separately**:
  a second Python process published a `SYSTEM_ALERT` onto Redis and the exact
  event id arrived on the socket held by uvicorn — which the in-process tests
  cannot show.
- **Trading safety:** `TRADING_MODE=paper`, `LIVE_TRADING=false`, ten gates
  still false. A test walks every module in `app.realtime` and asserts none of
  them reaches MetaTrader5, the order sender, `OrderRequest` or
  `BrokerAdapter`. A bug in the hub can drop a message or deliver it twice; it
  cannot trade.
- **Tests:** backend 227 → 277 (274 passed, 3 skipped), frontend 55 → 66,
  research 34 unchanged. New file `tests/test_realtime.py` with 50 cases.
- **Remaining:** no producer for 28 of the 29 types until their levels land;
  no UI panel is bound to the socket yet (L03 binding work); connection limits
  are per-connection rather than per-user, which needs a shared counter once
  more than one API process runs (L41).

## Level 08 — Market data, completed (2026-09-03)

- **Audited first; every fetcher was kept and none was copied.**
  `tools/market.get_ohlcv` (+ its `validate_ohlcv` and 7-day disk cache),
  `tools/crypto_market.fetch_ohlcv` / `to_market`, and
  `tools/rule_backtest.fetch_rates` / `trim_to_years` are **called** by the new
  adapters, not reimplemented. `fetch_rates`' stepping-down retry exists
  because the terminal rejects an over-large request with "Invalid params"
  rather than returning what it has; reimplementing it would have lost that.
- **One normalized model**, `app/marketdata/types.py`: `Quote`, `Bar`,
  `Series`, `SeriesQuality`, eight timeframes, five providers. The rule
  throughout is **a value that was not supplied is None and stays None** —
  never zero, never the previous close. `Availability` makes the distinction
  explicit per field: AVAILABLE, NOT_AVAILABLE, DERIVED.
- **The spread is where that rule earns its keep.** MT5 records it per bar,
  Yahoo has no concept of it, and `ask - bid` is DERIVED rather than observed.
  A recorded 0 is an *unrecorded* spread, not a free trade —
  `cost_profile.py` measured that averaging those zeros in halves the apparent
  cost of trading — so it is stored NULL with its availability beside it.
- **Validation flags; it never silently repairs.** `inspect_series` measures
  and changes nothing; `clamp_ohlc` repairs one bar and is only called by a
  caller that has read the report. The measurement carried forward from
  `validate_ohlcv` is the one that matters: Yahoo's FX series puts the close
  outside the day's range on 2-6% of bars and synthesises ~40% of its opens,
  which produced a t-statistic of 28 in this repository's own forex pattern
  study. `ohlc_trustworthy=false` now travels **with the bars**, and its note
  says what to do: use the close, never the body or the shadows.
- **Staleness is derived from the timeframe, not fixed.** A D1 bar twenty
  minutes old is fresh and an M1 bar twenty minutes old is not, so one
  universal threshold would be wrong for every instrument but one. A closed
  market is not a stale feed; an *unknown* session is treated as open, because
  an unnoticed dead feed is worse than a false alarm. **A stale feed is
  reported and never acted on** — deciding what to do about one belongs to the
  strategy and the risk engine.
- **Idempotent ingestion and a new table.** Migration `0006_market_bars` adds
  `market_bars`, unique on (provider, provider_symbol, timeframe, bar_time).
  Purely additive: no column altered, no row rewritten, zero drift, and the
  252 trades / 269 orders / 9 symbols verified untouched after it ran. The
  OHLC relationships are enforced as a CHECK constraint as well as in Python,
  because a bar that reaches the table by another path is still a bar.
  `provider` is part of the key so two providers' "EURUSD H1" sit side by side
  and are never averaged.
- **Failover is controlled, never automatic.** `fallbacks` are consulted only
  when the caller passes them, the substitute is named in the response
  (`provider_used`, `fell_back`), and bars from two providers are never merged
  into one series. Quietly swapping one for the other would put another
  venue's prices behind a broker symbol.
- **The simulator is labelled and fenced.** Every bar and quote carries
  `provider="simulator"` all the way to the response; it refuses to construct
  in production; it is deterministic; and it reports **no** spread rather than
  a fabricated one.
- **Found by testing, twice, and both were real.**
  1. `MarketBar.id` used `BigInteger`, which SQLite will not autoincrement —
     the same variant `PositionEvent` already uses fixed it.
  2. A symbol with no provider mapping was being reported as a **provider
     outage (503)** by the failover loop, which would send an operator to look
     at a terminal that is working fine. Mapping failures are now tracked apart
     from wire failures and answer 404.
- **Found by running it against the real terminal, and this is the important
  one.** The first live quote came back flagged by this level's own validator
  as *"stamped in the future"* — by exactly 3h00m. MT5 encodes a stamp as the
  broker's **wall clock** rendered as a UTC epoch, so reading it back as UTC
  shifts every timestamp by the broker's offset, and every staleness check
  with it. The adapter now **measures** the offset against this machine's UTC
  clock and rounds to the nearest half hour (broker offsets are whole or half
  hours; the rounding is what separates a 3h offset from a seven-minute-old
  tick), and refuses rather than assuming one when no liquid symbol ticks.
  The limitation is written down in the code: a tick more than fifteen minutes
  stale has part of its age absorbed into the offset.
  After the fix: `broker server clock is UTC+3h, measured`, the quote is one
  second old with an empty `problems` list, and H1 bars land on 02:00, 03:00
  and 04:00 UTC.
- **A second live finding:** the first quote read had bid == ask == 1.15966,
  a zero spread. That is not a free trade, it is a book that is not two-sided,
  and costing a trade from it understates the spread to zero — the direction
  that flatters every result. `check_quote` now flags it.
- **Verified live against the running terminal**, not only in tests: providers
  reporting real usability; a fresh EURUSD quote with a 1-point spread; five
  H1 bars fetched and stored; a re-fetch of the same window storing **0**;
  the cache read labelling itself "not a live provider read"; a clean quality
  report; and four refusals — unknown timeframe 422, unknown provider 422,
  unmapped symbol 404, and a quote requested from history-only yfinance 422.
- **TradingView is deliberately not a market-data provider here.** It stays a
  webhook/alert path (L09). Its payloads are not an unrestricted substitute
  for a normalized feed.
- **Trading safety:** `TRADING_MODE=paper`, `LIVE_TRADING=false`, ten gates
  still false. A test walks every module in `app.marketdata` and asserts none
  reaches `order_send`, `place_order`, `OrderRequest`, `BrokerAdapter` or
  `RiskEngine`. No order was submitted; the terminal was read only.
- **Tests:** backend 277 → 329 (326 passed, 3 skipped), frontend 66 unchanged,
  research 34 unchanged. New file `tests/test_marketdata.py` with 56 cases.
- **Remaining:** no ccxt adapter yet (the toolkit fetcher is ready to wrap and
  no consumer needs it); no live streaming subscription — the L07 catalogue
  declares `MARKET_UPDATE` and nothing publishes it until a poller or a tick
  worker exists; the H4 timeframe is deliberately absent from yfinance rather
  than resampled from H1, which would produce bars no venue printed.

## Level 09 — TradingView webhook, completed (2026-09-03)

- **Audited first.** `tools/tv_webhook.py` already had the two hard parts
  right: a constant-time secret compare, and redaction that strips the secret
  from keys *and* from string values at any depth, because a plain-text alert
  carries it inline. Both are **kept** — the gateway's `redact` is that rule
  moved, not rewritten — and the CLI receiver is unchanged and still runs.
  What was missing were the gate stages: schema validation, timestamp
  validation, replay protection, idempotency, symbol resolution, strategy
  mapping and Signal emission.
- **One endpoint, `POST /v1/webhooks/tradingview`.** No second receiver was
  created. The CLI tool stays as the standalone-tunnel path it always was.
- **The pipeline, and the order is deliberate:** body cap → parse →
  authenticate → validate → age check → idempotency → symbol → strategy →
  Signal → commit → publish. Authentication runs *before* validation so a
  caller without the secret learns nothing about the schema. The commit
  happens *before* the publish so an event never announces a row a subscriber
  cannot yet read.
- **An unset secret refuses everything.** This is the one place the platform
  deliberately diverges from the CLI tool, which warns and continues. A tool an
  operator is watching may run unauthenticated; a server endpoint that accepted
  anything because it was misconfigured would be an open write path into the
  signal table. A field match is recorded as `strong`, an inline substring
  match in a plain-text alert as `weak`, so a later policy can require the
  first.
- **Only listed actions are executable words.** BUY, SELL, LONG, SHORT, CLOSE,
  EXIT, ALERT. Anything else is rejected, never interpreted. CLOSE and EXIT
  normalize to `flat`, not `sell`: they say "be out", and turning a close into
  a sell would open a short on a flat account.
- **Quantity, stop-loss and take-profit from the payload are recorded and
  never obeyed.** They land in `metadata.advisory_ignored` — a name chosen so
  no consumer picks them up by accident — and the platform recomputes size
  through Risk → Sizing. An alert that could set its own lot size is an alert
  that can set its own risk limit.
- **Idempotency: one alert, one signal, however many times it arrives.** The
  key is the sender's own id when it gives one, otherwise a SHA-256 of ticker,
  action, the alert's *own* timestamp, strategy and timeframe. The received
  time is deliberately excluded — including it would make every retry unique,
  which is the same as having no idempotency at all. `webhook_events.idempotency_key`
  and `signals.signal_key` are both unique, and a race between two identical
  alerts resolves to a duplicate rather than an error.
- **A missing timestamp is refused, not defaulted to now.** Defaulting would
  make every replayed alert look fresh, which is exactly what the age check
  exists to catch. Alerts older than 120s or stamped more than 30s ahead are
  refused; both bounds are configurable.
- **An unmapped ticker is refused, never guessed.** Resolution goes through the
  L11 mapping table. "EURUSD" on TradingView and "EURUSD" at the broker are two
  strings that happen to look alike.
- **A named strategy must exist.** A payload that could select an arbitrary
  strategy could select a privileged one. An alert with *no* strategy is the
  external-alert case and is supported explicitly: the signal is recorded with
  `strategy_version_id` NULL and its source says where it came from.
- **Rejections are recorded too.** "We never received it" and "we received it
  and would not act on it" are different answers to the same question, and only
  one of them means the sender should look at its own configuration.
- **Nothing is executed, and the response cannot say otherwise.** `status` takes
  one of four values — accepted, duplicate, rejected, unauthorized — none of
  which describes a trade, and every response carries the note "a recorded
  signal, not a trade; nothing was executed". A test asserts no module under
  `app.webhooks` reaches MetaTrader5, `mt5_paper`, `order_send`, `place_order`,
  `BrokerAdapter`, `OrderRequest` or `RiskEngine`.
- **`SIGNAL_CREATED` is published** onto the L07 bus, scoped to the strategy
  channel when the alert names one and to the symbol channel otherwise. Nothing
  consumes it: L12, L17, L18 and L19 do not exist. A publish failure leaves the
  signal durable and still answers 200, because it *is* recorded — reporting
  failure for a stored alert would invite a retry of something that succeeded.
- **Found while wiring:** the rate limiter's `Decision` exposes `retry_after`,
  not `retry_after_seconds`; the route was reading a field that does not exist
  and would have raised on the first throttled request.
- **Two of my own tests were wrong, not the code:** a fixed alert timestamp
  aged past the 120-second window while the suite ran, and a substring check
  for "executed" matched the response's own honest disclaimer. Both corrected
  to assert the claim rather than the characters.
- **Security:** no secret in source, none in `.env.example` (the variable is
  documented empty), none returned by any endpoint — the status route reports
  `secret_configured: true` and nothing more — and none stored. Verified
  against the live database: `select count(*) from webhook_events where
  payload::text like '%secret%'` returns **0**. The payload is never logged,
  because it carried the secret a moment earlier and a log line is the easiest
  place to leak one. CSRF does not apply (no session cookie); rate limiting is
  the protection, 120 requests per minute per source.
- **Verified live** against the running API with a real secret set: a valid
  alert accepted with a signal id; **the identical alert twice more returning
  `duplicate` with the same signal id**; a wrong secret 401 with a body that
  reveals nothing about the schema; an unknown action 422 naming the seven that
  are allowed; a stale alert 422 saying it was 189,139s old against a 120s
  maximum; an unmapped ticker 422. The database then held **1 signal from 3
  identical alerts**, 1 accepted and 3 rejected webhook events, and the signal
  itself: source `tradingview`, direction `buy`, mode `paper`, status `new`,
  auth `strong`, confidence NULL.
- **Trading safety:** `TRADING_MODE=paper`, `LIVE_TRADING=false`, ten gates
  still false. After every one of those live alerts the row counts were
  unchanged — 269 orders, 0 positions, 208 executions, 252 trades. **No order
  was submitted and no broker was contacted.**
- **Tests:** backend 329 → 394 (391 passed, 3 skipped), frontend 66 unchanged,
  research 34 unchanged. New file `tests/test_webhooks.py` with 62 cases.
- **Remaining:** the IP allowlist is off by default and only meaningful when
  the port is exposed directly (behind a tunnel the source is the tunnel's);
  no signed-request scheme, because TradingView cannot send headers and a body
  signature would need the secret in the body anyway; nothing consumes
  `SIGNAL_CREATED` until L12/L16.

## Level 10 — MT5 / broker adapter, completed (2026-09-03)

- **The audit changed what this level was.** `BrokerAdapter` (12 methods),
  `MT5Adapter` (429 lines) and `FakeBroker` (285 lines) already existed and are
  well built — three-state `OrderResult`, `assert_demo` called on connect,
  order construction delegated to the toolkit's `place()` rather than copied.
  All **kept unchanged**. What they did not have was **a single test**, a
  `health()`, a `reconcile()`, a registry, or any route. 963 lines of the most
  execution-critical code in the repository were unverified. That is what L10
  actually needed to be.
- **No second MT5 connection was created.** `MT5Adapter.connect` still calls
  the toolkit's `connect()`, and the L08 market-data adapter calls it too.
  Merging the four remaining copies stays on the list; this level added none.
- **`health()` and `reconcile()` are concrete on the base class**, written in
  terms of the abstract read methods. One implementation means every venue
  reports the same way and a new adapter cannot implement health
  optimistically or forget it.
- **`health()` calls `get_account()`, because the only way to know a link works
  is to use it.** A terminal that is running but not logged in returns cleanly
  from connect and fails there. Connected-but-unreadable is `degraded`.
  **Connected with trading disabled is also `degraded`, not healthy** — an
  order attempted on such an account cannot succeed, and reporting it healthy
  would be a lie the OMS acts on.
- **`degraded` and `reconnecting` were added as distinct states.** Degraded
  means the link answers badly and reads are still worth something;
  reconnecting means it is gone. Collapsing them would let a caller treat a
  half-working venue as a dead one, or the reverse.
- **Reconciliation reports and never repairs.** A test reads the module's own
  source and asserts it contains no `db.add`, no `db.commit`, no
  `place_order(`, no `close_position(`, no `cancel_order(`. The reason is that
  both instincts are wrong: closing an unexpected position closes somebody's
  manual trade, and re-sending a missing one is how a crash between send and
  log becomes two positions. Seven findings are named —
  `unexpected_at_broker`, `missing_at_broker`, `volume_mismatch`,
  `price_mismatch`, `bracket_mismatch`, `unexpected_order_at_broker`,
  `unresolved_unknown` — and each carries what we believe against what the
  venue says.
- **`safe_to_trade` blocks on an unresolved UNKNOWN and on nothing else.** An
  unexpected position at the venue is a fact to investigate, not a reason to
  halt: a rule that stopped the platform on any mismatch would be switched off
  the first time somebody opened a trade by hand.
- **Order validation refuses; it never rounds.** A volume off the venue's step
  is rejected with the nearest valid value **below** it named in the message.
  Rounding up would risk more than was budgeted and silently undo
  `lot_for_risk`. Decimal arithmetic throughout, because a float remainder on
  0.1 steps gives 0.09999999999999998 and rejects a valid order — the same
  float-floor class of defect the sizing module already paid for. A missing
  `SymbolInfo` is itself a refusal: an order for a symbol whose terms could not
  be read is an order for the wrong amount.
- **`BrokerRegistry` is per account, with no default.** A module-level "the MT5
  connection" is what makes two accounts share credentials, positions and risk
  state by accident. An unknown account raises rather than falling back, and
  there is a lock per account because MT5's Python API is one global connection
  per process.
- **The broker API surface is read-only, and a test enforces it.** Seven paths,
  **GET only** — verified against the live OpenAPI document, which reports
  `write methods: NONE`. That is not a temporary convenience: a read-only route
  cannot grow a write by accident, because the write has to be added in the
  level that also adds the veto in front of it. `POST /v1/orders` remains the
  single submission door and still answers 501 naming L19.
- **`/brokers` left the legacy alias list**, for the same reason `/orders` did
  at L06: the group is built and readable, so a root path reporting it missing
  would be a false statement.
- **Credentials:** the platform holds none. A grep across `app/brokers/` for
  login, password or MT5 credential handling returns nothing; the adapter takes
  a terminal path at most and the terminal owns the session. `describe()` and
  every route therefore carry nothing to filter, and the account **number** is
  omitted from responses because an account number is a credential-shaped
  identifier nothing in the UI needs. Encrypted broker credentials remain L39.
- **Verified live** against the running API: an empty registry at boot (an
  adapter is constructed deliberately, never as a side effect of the API
  booting); `/v1/brokers/health` empty and not guessing; an unknown account
  404 naming the account rather than serving another one; seven broker paths
  with **no write method**; `POST /v1/orders` 501; and `/v1/system/safety`
  reporting paper mode, live false, **no gate true**, 12 blockers.
- **Trading safety:** `TRADING_MODE=paper`, `LIVE_TRADING=false`, all ten gates
  still false — `live_broker_adapter` and `broker_sync_on_connect` explicitly
  asserted false by a test. **L10 builds the adapter; it does not open the
  door.** No real order was submitted, no live account was contacted, and the
  row counts across all six levels of work are unchanged where they should be:
  252 trades, 269 orders, 208 executions, 0 positions.
- **Tests:** backend 394 → 456 (453 passed, 3 skipped), frontend 66 unchanged,
  research 34 unchanged. New file `tests/test_brokers.py` with 64 cases,
  covering connection, disconnect mid-run, health in all four shapes, accepted
  / rejected / **unknown** execution, close, validation against two genuinely
  different symbol specs, all seven reconciliation findings, the registry, the
  routes, and the safety assertions.
- **Remaining:** four `connect()` copies still to merge (a shim release, not a
  rewrite); no automatic reconnect loop — the states exist and the counter is
  reported, but the supervised runner that would drive it is L22; startup
  reconciliation is L38; `place_order` is reachable only from Python, and stays
  that way until Risk, Sizing and the OMS exist.

## Level 11 — Symbol mapping, verified and extended (2026-09-03)

- **The core was already right and was not touched.** Three-name resolution
  (source → internal → broker), exact-match-first with an
  exchange-prefix fallback, ambiguity refused rather than guessed, contract
  specs on the *mapping* row because terms belong to the broker and not the
  instrument, `IncompleteContractSpec` naming exactly which fields are absent,
  `DuplicateMapping` refusing a repoint. All **KEEP**, unchanged, and its 25
  tests still pass unmodified. No second mapping system was created.
- **Verified against the real table**: 9 symbols, 18 mappings, and the case
  that justifies the whole layer — `DE40` internally is **`XETR:DAX`** on
  TradingView. Seeding those identical would hide the failure the table exists
  to catch.
- **Found by testing, and it was a real bug:** `GET /v1/symbols/{code}/mappings`
  had raised a 500 **since L06**. `MappingOut.has_complete_spec` is filled in
  after validation by `model_copy`, and the field had no default, so
  `model_validate` failed on the ORM row every time. No test covered that route,
  which is why five levels went by without anyone noticing. Fixed with a `False`
  default — the safe one, since a mapping is not assumed to carry a usable spec
  — and a regression test now lives in `tests/test_api_v1.py` beside the surface
  it belongs to. Verified live: the route returns 200 with `mt5` complete and
  `tradingview` not.
- **Found by audit: the lot-size rounding had two implementations.**
  `app/brokers/validation.py` carried its own `floor_to_step` and a `%`-based
  step check; `mt5_paper.lot_for_risk` had the canonical one with the
  float-floor guard. Two functions that round a lot size differently is how the
  same signal sends two different volumes depending on which path reached the
  venue. There is now **one**, in `app/symbols/precision.py`, and the broker
  layer imports it — asserted by a test comparing the function objects. The
  broker layer's old `%` check also lacked the float-floor guard, so this is a
  correctness fix as well as a deduplication.
- **Quantity normalization (Steps 9–10), new.** The default **refuses** a
  quantity off the venue's step grid and names the nearest valid value *below*
  it. `allow_floor=True` exists for position sizing, which computes a raw
  figure from a risk budget — and even then the result records that it floored
  and from what, so no adjustment is invisible. A quantity below the venue
  minimum is **never** raised to it: when the minimum lot exceeds the budget the
  correct answer is "this trade cannot be taken at this size", not "take a
  bigger one".
- **Price normalization (Step 11), new.** `normalize_price` snaps to the tick
  grid with banker's rounding, which does not drift over many calls, and works
  on non-decimal grids — an index quarter-point tick is real and 0.01-rounding
  would produce prices the venue rejects. It deliberately does **not** pick a
  risk-safe direction, because none exists for a price in general. Two
  direction-aware helpers do: `snap_stop_loss` rounds toward the entry on both
  sides (tightening risk is the safe direction to be wrong in), and
  `snap_take_profit` rounds away, because a target snapped closer would book a
  win the instrument never reached.
- **Symbol status vocabulary (Step 12), new.** `active`, `inactive`,
  `not_found`, `not_tradable`, `mapping_error`, `unknown` — and **only `active`
  is tradable**, asserted across the whole enum. `status_of` never raises, so a
  UI listing instruments gets a row for each one including the broken ones, and
  it never reports a symbol it could not resolve as active. Errors map to
  statuses through an explicit table rather than by class name, so adding an
  error forces a decision about tradability instead of defaulting to something
  benign.
- **One pre-trade gate (Step 7), new.** `validate_for_trading` checks internal
  symbol exists → active → mapped at this provider → mapping active → spec
  complete, and returns the spec, because every caller that needs the gate also
  needs the numbers and a second lookup is a second chance to disagree. It
  exists so L18 and L19 do not each assemble their own subset of those checks.
  **Passing it is not authorization** — a test walks `app.symbols` and asserts
  no module reaches `place_order`, `OrderRequest`, `BrokerAdapter`, `RiskEngine`
  or `order_send`.
- **Admin mapping API (Steps 15–16), new.** `/v1/admin/symbols` — create or
  update a symbol, upsert one provider mapping, enable or disable a mapping,
  and read a symbol's status. Reading needs `view_markets`; **changing needs
  `manage_system_settings`**, the strictest gate the table offers, because a
  mapping edit decides which instrument an order reaches. Every write goes
  through the existing service functions, so the conflict and validation
  refusals apply unchanged, and every change is audited with its before value.
- **There is no DELETE route, deliberately.** A mapping used to place an order
  is part of the record of why that order went where it did; removing it would
  make a historical trade unexplainable. Disabling takes it out of every trading
  path — `resolve_source` already refuses an inactive mapping — while leaving
  the history readable. Asserted by a test and confirmed against the live
  OpenAPI document.
- **Contract specs are not settable over HTTP**, on purpose. They come from the
  terminal via `app.symbols.sync_mt5`, because a spec typed by hand and a spec
  read from the venue are not the same evidence and `spec_source` has to stay
  truthful.
- **Verified live** against the real database: the broken mappings route
  returning 200; status `active` for EURUSD, DE40 and XAUUSD and `not_found`
  for an unknown code; a repoint attempt answering **409** naming the instrument
  the symbol is already mapped to; an unknown provider **422**; disabling DE40's
  mt5 mapping flipping its status to `inactive` and its spec route to 409; a
  re-enable restoring it with nothing deleted; and the audit trail showing both
  changes with `from` and `to`.
- **Deliberately not added:** a Redis mapping cache (Step 13). The table is 9
  rows behind an indexed unique constraint, the prompt itself warns against
  making a cache authoritative, and a cache with no measured pressure is
  infrastructure that can go stale for no gain. Also no `display_name` column —
  the code already serves that role for these instruments and a migration for a
  cosmetic field is schema churn.
- **Known limitation, written down rather than papered over:** `resolve_source`
  falls back to stripping an exchange prefix and looking the remainder up in the
  table. That is a second lookup key, not a fuzzy match, and it exists because
  TradingView's `{{ticker}}` sends prefixed or bare depending on how the alert
  was written. The residual risk is narrow: a mapping stored *bare* would match
  a prefixed alert from any exchange. Every seeded mapping is exchange-qualified
  so the path is unreachable today, and tightening it would break the documented
  case it exists for. Recorded here rather than changed on a judgment call.
- **Security:** no credential in any mapping record or response — grepped; the
  admin surface is gated on `manage_system_settings`; user input reaches the
  provider field only through an allow-list; and a mapping cannot authorize a
  trade. Verified live: `POST /v1/orders` still 501, paper mode, all ten gates
  false, 12 blockers, and 252 trades / 269 orders / 208 executions / 0 positions
  unchanged.
- **Tests:** backend 456 → 506 (503 passed, 3 skipped), frontend 66 unchanged,
  research 34 unchanged. New file `tests/test_symbol_mapping.py` with 49 cases
  covering the prompt's 21-case list, plus one regression test in
  `tests/test_api_v1.py`. The 25 existing symbol tests were not modified.

## Level 12 — Strategy engine, completed (2026-09-03)

### The audit, and what each existing strategy became

| Implementation | Where | Decision | Why |
|---|---|---|---|
| `rule_sma_cross`, `rule_rsi_reversion`, `rule_random` | `tools/mt5_paper.py` (RULES) | **KEEP + WRAP** | These produced the 252 recorded demo trades. Called, never copied |
| `signals_sma_cross`, `signals_rsi_reversion`, `signals_random` | `tools/rule_backtest.py` (SIGNALS) | **KEEP, untouched** | The vectorised twins the backtester uses; `test_indicators_match_live` already pins them to the live rules |
| `make_rsi`, `make_ma_cross`, `make_donchian`, `make_bollinger`, `make_momentum` (41 candidates) | `tools/rule_search.py` | **KEEP as research-only** | A parameter search's output is not a strategy. Registering them would invite running the top of a table |
| `tools/indicators.py` (pandas: SMA, EMA, RSI, MACD, ATR, ADX, Bollinger, …) | equity snapshot pipeline | **KEEP** | Different data shape, different pipeline, parity-tested |
| `wilder_rsi`, `atr_series`, `sma` (numpy) in `rule_backtest`; `ema` in `rule_search` | FX rule pipeline | **KEEP** | Not accidental duplication: numpy arrays vs pandas Series, two pipelines. Merging them would rewrite tested code for style, which Step 1 forbids |

**Nothing was replaced and nothing was removed.**

### What was built

- **A Strategy contract** — `metadata()`, `required_data()`, `validate_config()`,
  `generate_signal()`. Strategies are stateless: they hold a validated config
  and nothing else, so two instances on different symbols cannot interfere and
  the same object runs live, in a backtest, in replay and in a test.
- **The input is L08's normalized `Bar`**, never an MT5 recarray or a
  TradingView payload. A strategy does not know which source it is reading.
- **Seven signal types.** ENTRY_LONG/SHORT, EXIT_LONG/SHORT, CLOSE, HOLD,
  NO_SIGNAL. ENTRY and EXIT stay separate because "get out of a long" and "go
  short" are different instructions; HOLD and NO_SIGNAL stay separate because
  "I have a view" and "I could not form one" are different answers.
- **A registry and factory** that resolve a key to a class from a dict
  populated at import. No `eval`, no `exec`, no `importlib` on a caller-supplied
  string — asserted by parsing each module's AST rather than grepping its
  prose, since a docstring saying "there is no eval here" contains the string.
- **A tier gate.** All three built-ins are `research_only`, which is a
  measurement and not caution. Asking the factory for `live_approved` refuses
  every one of them, which is the correct answer.
- **The Strategy Engine**: load → validate config → check warm-up → check
  staleness → run inside a boundary → validate the output → publish
  SIGNAL_CREATED → count. It imports no broker adapter, no risk engine, no
  sizing and no OMS.

### No look-ahead, enforced by construction

`Candles.of` drops any bar whose `complete` flag is false **before a strategy
sees the data**, so a strategy physically cannot read a forming bar — it is not
in the object. It also sorts, so two runs over the same set agree. Timing is
declared per strategy (`bar_close` for all three) rather than assumed, because
mixing candle-close with intrabar silently makes a backtest describe something
the live tool does not do.

### The regression result, which is the point of the level

`test_the_wrapper_matches_the_toolkit_rule_exactly` runs each wrapped rule and
the `tools/mt5_paper` function it wraps over the same bars, across four
generated series (rising, falling, crossing, up-then-down), and asserts they
agree on **every one**. Behaviour did not move.

**One deliberate adapter detail, documented rather than hidden.** The toolkit
rules drop the forming bar themselves with `rates["close"][:-1]`. `Candles.of`
has already dropped it, so passing the candles straight through would drop a
second, *real* bar and shift every signal back one. The adapter appends a
duplicate of the last closed bar as a stand-in, so the rule's own slice removes
the stand-in and sees exactly the history it would have seen live. A test pins
that the rule's post-slice view equals the closed set exactly.

**`tools/` is unchanged.** `python tests/test_rule_backtest.py` — the toolkit's
own suite, including `test_indicators_match_live` — still passes end to end.

### Determinism, and the one behaviour that was changed on purpose

`mt5_paper.rule_random` uses the global `random` module and is therefore not
reproducible. Step 27 requires randomness to be explicit and controllable, so
the wrapper seeds a private generator from the strategy's `seed` and the bar
time: **the same bar always draws the same way**, which is what backtesting and
replay need. The *distribution* is unchanged — buy, sell, None, None — so the
benchmark still measures what it measured, and a test asserts the proportions.
This is the only behavioural change in the level and it is a strict improvement
in reproducibility.

### Failure handling

- **A strategy error is never a neutral signal.** A crashing strategy that
  returned HOLD would read as a quiet market, so the engine returns an `error`
  outcome with the reason and publishes nothing.
- **One bad strategy stops itself**, not the process.
- **A malformed signal is an error**, not a signal — a strategy that answered
  about a different symbol would otherwise travel downstream in a valid
  envelope.
- **Stale data produces no signal**, measured with L08's timeframe-derived
  rule. A rule fired on a bar that closed an hour ago is a decision about a
  market that has moved. A *closed market* is not stale.
- **Too few bars is NO_SIGNAL, not HOLD.** An RSI over three bars is a number
  and it is not an RSI.

### Found by running it live

`/v1/strategies/{key}/evaluate` returned **200 with `outcome=error`** for a
typo'd strategy key, because the engine catches its own errors by design and
the route's handler therefore never fired. An unknown key is a missing
resource, not a strategy that ran and failed. The route now resolves the key
**before** fetching market data — which also avoids a pointless provider call —
and answers 404 naming the registered strategies. A test covers it.

### Verified live against the real MT5 terminal

Both rules were run over **300 live H1 EURUSD bars**: `sma_cross` and
`rsi_reversion` each returned HOLD on the bar closing 05:00 UTC at 1.15963,
with `stale: false`, `ohlc_trustworthy: true` and `confidence: null`. Also
verified: an unknown strategy 404 naming the three that exist; an unsupported
timeframe 422; an unmapped symbol 404; and `limit=10` reporting
`insufficient_data | 10 closed bars; sma_cross needs 61` rather than a hold.

### Security and trading safety

- A test walks every module in `app.strategies` and asserts none reaches
  MetaTrader5, `place_order`, `order_send`, `BrokerAdapter`, `OrderRequest`,
  `RiskEngine` or `MT5Adapter`.
- A second parses each module's AST and asserts none calls `eval`, `exec`,
  `compile` or `__import__`, and none imports `pickle`.
- The engine's imports are checked for `app.risk`, `app.sizing` and
  `app.brokers` — by import statement, not by text.
- Every `/v1/strategies` path is **GET only**, confirmed against the live
  OpenAPI document. Running a strategy needs `create_strategies` (TRADER and
  above), because it fetches data and publishes an event.
- `TRADING_MODE=paper`, `LIVE_TRADING=false`, all ten gates false. After every
  live evaluation: 252 trades, 269 orders, 208 executions, 0 positions —
  unchanged. **No order was submitted.**

### Tests

Backend 506 → 563 (560 passed, 3 skipped), frontend 66 unchanged, research 34
unchanged and the toolkit's own `test_rule_backtest.py` still passes. New file
`tests/test_strategies.py` with 60 cases.

### Remaining

- No exit-rule interface yet: the three rules are entry-only, which is what
  they always were. `generate_exit` arrives with the position manager wiring.
- Nothing consumes `SIGNAL_CREATED` — L17 (Risk) and L19 (OMS) do not exist.
- The 41 `rule_search` candidates are not registered. Registering a parameter
  search's output as a named strategy is exactly how a table's top row gets
  run, and none of them cleared its null.
- Backtest integration is L14: the engine is deterministic and connection-free,
  which is what that needs, but the runner is not built.

## Level 13 — Strategy builder, completed (2026-09-03)

### The audit

`src/app/strategy-builder/page.tsx` was a `PlannedPage` placeholder — 17 lines
describing what the builder would be. **KEEP** its description (it was
accurate) and **REPLACE** the placeholder with a working editor. The L05
tables `strategies`, `strategy_versions` and `strategy_parameters` already
existed and are used as they stand: **no migration was needed**. The L12
`Strategy` contract, registry and engine are reused unchanged — a built
strategy implements the same interface as a hand-written one, and the engine
cannot tell them apart.

### The security model, which is the whole point

**A definition is data.** It is parsed into frozen dataclasses, validated, and
walked by a fixed evaluator in `app/strategies/built.py`. At no point does any
part of it become source, get compiled, get imported or get executed. The set
of things a user can express is exactly the set the evaluator implements, and
nothing else can be smuggled through.

`code_ref` on every built version is the same string —
`app.strategies.built:BuiltStrategy` — because there is one interpreter and it
is not chosen by the user. A test asserts that constant, and another parses the
AST of all four builder modules to assert none of them calls `eval`, `exec`,
`compile` or `__import__`. Four hostile operands (an `__import__` payload, a
path traversal, a dunder attribute, a code string as a period) are each
refused.

### The definition schema

`indicators`, `conditions`, `AND`/`OR`/`NOT` groups, `entry_rules` and
`exit_rules`, over a symbol and a timeframe. Seven comparisons including
`CROSSES_ABOVE` and `CROSSES_BELOW`, which read bar T and T-1 — a cross is
defined by two bars.

**Two validations.** Structural: required fields, types, no unknown keys
(refused rather than dropped, because a silently ignored field means the user
is running a strategy they did not write), bounded nesting and bounded rule
counts so a hostile definition cannot exhaust the stack. Semantic: the
indicator exists, its parameters are in range, the symbol is mapped through
L11, the timeframe is one of L08's, and entry rules may not emit `CLOSE` — an
entry rule that could would let the builder close positions it never opened.

### The check the brief names, and why it matters here

`PRICE > RSI` is **refused**. Every operand declares a `Unit` — price,
oscillator, volatility, volume, ratio, scalar — and two operands may only be
compared when their units match. A constant is compatible with anything
because it takes its meaning from what it is compared against.

Verified live: the message is *"cannot compare CLOSE (price) with RSI(14)
(oscillator). They are different quantities, and a comparison between them
means nothing"*. `ATR > close` is refused for the same reason — an average
true range is a price **distance**, not a level. This is the metals-points
error in miniature, and the project has paid for that class of mistake twice.

### Four indicators, and no more

SMA, EMA, RSI and ATR — because four is what the numpy rule pipeline actually
has, and they are **called** from `tools/rule_backtest.py` and
`tools/rule_search.py` rather than reimplemented, so a built strategy and a
searched candidate compute the same RSI. `tools/indicators.py` has MACD,
Bollinger and ADX, and they are deliberately **not** exposed: they are pandas
functions over a different data shape for the equity snapshot pipeline, and
offering them would mean a second implementation.

An indicator that has not warmed up returns `None`, never `0`. A condition
against a substituted zero fires for the wrong reason.

### Versioning

**A validated version is immutable.** Editing one answers 409 and points at
`POST /versions`, because a signal recorded against version 2 must always be
explainable by reading version 2, and that is easy to lose by making PUT
convenient. A **draft** may be edited in place, since nothing has run against
it. Duplication creates a new strategy id, a fresh version 1, and records
`cloned_from` beside the definition rather than inside it — so the stored
payload stays round-trippable — and leaves the original untouched.

### The lifecycle refuses states nothing enforces

`draft`, `validated` and `retired` are reachable. `backtested`, `paper`,
`active` and `paused` are **refused with the level that builds them**.
Verified live: marking a version `active` answers *"a version cannot be
'active' yet: the machinery that would enforce it is built at level 17.
Marking it so would let a strategy claim a state nothing checks"*. That is why
no migration was needed to widen the status constraint — the states the
database allows are exactly the states the platform can honour.

### Found by testing, and it was a real bug

`strategy_versions.config` was storing `definition.as_dict()`, which includes
derived fields (`indicators`, `warmup_bars`, `summary`). `parse_definition`
refuses unknown keys — correctly — so **a saved version could not be read
back**. A strategy the platform wrote and could not then parse is a strategy
that becomes unreadable the moment anyone tries to use it. Split into
`to_payload()` (canonical, round-trippable, what is persisted) and `as_dict()`
(payload plus derived views, for an API response), with a preview test on both
an ordinary and a cloned version.

### The builder UI

A structured form editor, not drag-and-drop — Step 13 says not to add
drag-and-drop complexity where a simpler and more reliable editor will do, and
correctness matters more than visual complexity. Every control maps to exactly
one field of a validated definition.

The right-hand column shows the **server's** validation, not the browser's.
The only client-side check is a unit-compatibility hint, and it is a hint: a
builder that validated only in the browser would be one anyone could bypass
with curl. The backtest panel says **NOT RUN** with the reason, because the
runner is L14 and a plausible-looking number would be worse than nothing. The
save panel says a saved strategy is research-only, because building one is not
evidence that it works.

### Authorization

Every strategy is owned, and ownership is scoped **in the query**
(`WHERE owner_user_id = :me`) rather than by filtering a list afterwards — a
filter has bugs and a WHERE clause does not. A strategy belonging to someone
else answers exactly as one that does not exist, because telling a caller that
a key is taken by another user is a membership oracle. Tested: user B gets 404
on read, on creating a version, and on a status change of user A's strategy,
and sees an empty list. There is **no DELETE route** anywhere on the surface.

### Verified live

The catalogue returning four indicators with their units and seven
comparisons; a good definition validating with a 150-bar warm-up and a
readable three-line summary; `PRICE > RSI` refused with the exact field path;
create returning version 1 `validated`; a second version created rather than
overwriting; an in-place edit of a validated version refused 409; `active`
refused naming level 17; the preview reporting `NOT RUN`; and both versions
persisted with `code_ref = app.strategies.built:BuiltStrategy`.

### Trading safety

`TRADING_MODE=paper`, `LIVE_TRADING=false`, all ten gates false. A built
strategy is `research_only` with the evidence field reading *"Built, not
measured. No backtest has been run and no validation report is attached."* A
test asserts `app.strategies.definition` and `app.strategies.built` import
nothing from `app.brokers`, `app.risk`, `app.sizing`, `MetaTrader5` or
`subprocess`. **No order was submitted.**

### Tests

Backend 563 → 625 (622 passed, 3 skipped), frontend 66 → 77, research 34
unchanged, and the toolkit's own suite still passes. New files
`tests/test_strategy_builder.py` (62 cases) and
`frontend/src/lib/builder.test.ts` (11 cases).

### Remaining

- Nested groups are edited in the read-only definition view rather than in the
  form. The schema, the validator and the evaluator all support arbitrary
  nesting; only the editor's controls are flat, which is an honest UI
  limitation rather than a capability gap.
- No import/export route yet. The validator would accept an imported
  definition unchanged, so it is a route away, not a design question.
- Backtest integration is a button that says NOT RUN until L14.
- No strategy parameters table use: a built strategy's parameters live in its
  definition, which is where the version already records them.

## Level 14 — Backtesting engine, completed (2026-09-03)

### The audit: the engine already existed

`tools/rule_backtest.simulate()` **is** the backtesting engine, and it was
**KEPT unchanged**. It already implements every execution semantic this level
asks for, and `tests/test_rule_backtest.py` pins each one:

| Semantic | Pinned by |
|---|---|
| entry at the **next bar's open**, from a signal reading bars up to i-1 | `test_entry_uses_closed_bars_only` |
| a bar covering both stop and target books the **loss** | `test_straddled_bar_books_the_loss` |
| the spread charged **once** per round trip | `test_spread_is_charged_once_per_trade` |
| **flat before the next entry** | `test_no_overlapping_positions` |

Rewriting that would mean re-deriving four constraints that took real trades to
learn. A test asserts the runner **calls** `toolkit.simulate(` and defines no
function named `simulate` of its own. `stats()`, `trade_stats.py`,
`atr_series`, the L08 provider and validator, the L12 Strategy interface and
the L05 `backtests` / `backtest_trades` tables were all reused as they stand —
**no migration was needed**.

### What was added

`app/backtest/`: a validated configuration whose every assumption is a field,
a runner that turns an L12 Strategy into the signal vector `simulate()` eats,
metrics, an async job service on the existing asyncio model, and
`/v1/backtests`.

### The look-ahead guarantee, and how it is proved

**The signal vector is built from prefixes.** At index i the strategy is handed
`Candles.of(bars[:i+1])` and physically cannot read further, because bar i+1 is
not in the list it was given. `simulate()` then reads `sig[i-1]` and enters at
`o[i]`.

That prefix walk is O(n × strategy cost). Computing indicators once over the
whole array and slicing is faster, and it is **exactly how look-ahead gets
introduced**, so the cost is the point.

Three tests prove it, and `test_future_bars_cannot_change_an_earlier_decision`
is Step 36's explicit proof: take a dataset, replace **only** the bars after
T with something wildly different, and the decision at T is identical. A third
test asserts on the object the strategy is handed rather than on its output —
at index i it saw exactly i+1 bars, never more.

### Costs are never assumed to be zero

`spread_points` has **no default**. A caller states it or the request is
refused with 422 — verified live. Cost drag is the only effect this repository
has measured large enough to see (the `random` rule loses at t = −3.60 because
it pays the spread every time), so a run that assumed zero would be measuring
something else. Tests assert a wider spread never improves a result, slippage
is always adverse, and commission moves money but not points.

**Financing is off unless both swap figures are given.** Every figure in this
repository was measured without it, and a default that silently restated them
would make the history unreadable.

### Metrics report absence rather than filling it

Below 20 trades, Sharpe and Sortino return **`NOT_AVAILABLE`** with the count.
A ratio from six trades is noise wearing a decimal point. They are also **per
trade, not annualised**: annualising needs a trades-per-year figure a fixed
historical window does not supply, and inventing one turns a Sharpe of 0.3 into
a Sharpe of 2. Drawdown is reported in money **and** percent separately, which
is the confusion the brief names. No trades is reported as a result about the
rule, not a failure.

Every metrics block carries a note saying a single t-statistic on one window is
the weakest of this project's three gates — the era blocks, the walk-forward
and the permutation null are in `tools/rule_search.py` and none of this
replaces them.

### Reproducibility

`fingerprint()` hashes the engine version, strategy, config, symbol, timeframe,
window, capital, sizing, bracket, costs and seed. Verified live: two runs of
the same configuration produced the **same fingerprint, the same 27 trades, the
same −0.0081 net points and the same t = −1.0785**.

### Jobs

Queueing returns 202 immediately; the run happens in a background task bounded
by a semaphore (2 concurrent) and a per-user queue limit (5). **A failed run is
never reported as finished** — the status is written before the work starts and
the failure path records `failed` with the reason, so a crashed run can never
be mistaken for one still waiting. An unfinished run returns `curve: null` with
the reason rather than an empty list, because "no curve yet" and "a flat curve"
are different statements.

The status vocabulary is L05's existing one — `queued`, `running`, `finished`,
`failed`, `cancelled`. `finished` is what the brief calls COMPLETED; using the
existing word beat widening a check constraint to add a synonym.

### Found by testing

- The `engine` and `status` columns are **constrained vocabularies**, and
  `simulate/1.0.0` and `completed` were not in them. Corrected to
  `rule_backtest` and `finished` — the engine version lives in `params`, so
  traceability is kept without widening a constraint.
- My own test fixture set `open == close` on every bar, and **L08's validator
  correctly flagged the series as synthetic**: a high open-equals-close rate is
  the signature of a vendor filling opens in rather than observing them. The
  validator was right, so the fixture changed to give the bars a real body.
- The isolation test queued a real run and then registered a second user; the
  background task and the second client share SQLite's single StaticPool
  connection in this harness and the interleaving destroyed the session row.
  The test is about authorization, so the row is now created directly.

### Verified live against the real MT5 terminal

A backtest over **1200 live H1 EURUSD bars** at a 0.00012 spread: 28 signals,
**27 trades**, 44.4% win rate, net −0.0081 points, profit factor 0.64,
expectancy −0.0003, Sharpe −0.21, Sortino −0.19, t = −1.08, exits 15 stop / 12
target / 0 timeout. 480 gaps reported — weekend closures over 1200 H1 bars,
which is correct and is why gaps are counted rather than filled.

**That result is the point.** It reproduces what CLAUDE.md already records:
`sma_cross` does not separate from a coin flip and is net negative at this
broker's real spreads. An engine that produced a flattering number here would
have been the thing to distrust.

### Trading safety

`TRADING_MODE=paper`, `LIVE_TRADING=false`, all ten gates false. A test parses
the AST of every module under `app.backtest` and asserts none imports
`app.brokers`, `MetaTrader5` or `mt5_paper`. After both live runs the row
counts were unchanged — 269 orders, 0 positions, 252 trades — with 2 backtests
and 54 backtest_trades added, which are simulation rows and are stored apart
from the execution record. **No broker was contacted and no order was
submitted.**

### Tests

Backend 625 → 664 (661 passed, 3 skipped), frontend 77 unchanged, research 34
unchanged, and the toolkit's own suite still passes. New file
`tests/test_backtest.py` with 39 cases.

### Limitations, stated rather than papered over

- **The bracket comes from ATR multiples, not from a strategy's suggested stop
  or target.** `simulate()` owns the bracket, and a second bracket path would
  be a second engine.
- **A built strategy's exit rules are not executed**; positions exit on the
  stop, the target or the hold limit. Entries are what the signal vector
  carries.
- **One symbol per run.** Multi-symbol and multi-timeframe are not implemented
  and are not faked.
- **Bid/ask is not modelled**: only OHLC is available, so the spread model
  stands in for the book, and the report says so.
- Position sizing is `fixed_quantity` only. `fixed_risk` and `percent_equity`
  are declared and **refused** with the reason, because reporting a result for
  a run nobody configured is worse than refusing to run it.

All five are listed at `GET /v1/backtests/engine/assumptions`.

## Level 15 — Market replay engine, completed (2026-09-03)

### The audit: three pieces already existed

| Piece | Where it was | What happened to it |
|---|---|---|
| the bar walk | `tools/rule_backtest.simulate()` | **kept**, still the reference |
| the execution semantics | L14's `app/backtest/` config and metrics | **reused**, not restated |
| the session table | `replay_sessions` from L05 | **reused**; one migration widened a CHECK |

`compute_metrics` is L14's. `BacktestConfig`, `CostModel` and `ExecutionModel`
are L14's. `CostsIn` is L14's request model, imported by the replay route
rather than copied. The `Strategy` contract is L12's, the bars and their
validation are L08's, and the event bus is L07's. What L15 adds is the part
that genuinely did not exist: a **clock**.

### The one real risk, and the test that answers it

A backtest hands the whole signal vector to `simulate()` and gets a trade list
back. A replay cannot do that — it has to stop between bars so a user can
pause, step and watch. So `app/replay/engine.py` is an **incremental**
implementation of the same rules, and that creates the risk this level is
mostly about: **two financial engines that disagree**.

`test_replay_matches_the_backtest_exactly` runs the same strategy over the same
bars through both and asserts the trade lists are identical — same entries,
same exits, same reasons, same net points — over three dataset lengths.

It earned its place immediately. It failed on the first run and caught two
genuine divergences:

1. **Rounding.** `_close()` rounded net points to 8 decimals where L14's
   `_enrich()` rounds to 6, so the two engines stored `-0.00919786` and
   `-0.009198` for the same trade.
2. **The end of the data.** `simulate()`'s inner loop runs
   `range(i, min(i + max_hold, n))` and falls through to
   `exit_px, exit_reason = c[j], "timeout"` — so a position still open when the
   **bars run out** is booked as a timeout at the last bar. The incremental
   engine only closed on `held >= max_hold`, so it left that position open and
   reported one fewer trade.

The second is the interesting one, because the incremental behaviour is
arguably the more realistic reading: a position open at the end of a replay is
still open. It was made to match `simulate()` anyway, in `finalise()`, and the
reason is written where the code is. A replay and a backtest over the same bars
reporting different trade counts would surface as an unexplainable discrepancy
rather than as a design choice, and the backtester's answer is the one already
measured.

A fourth test then found a third divergence that no run had exercised:
**financing**. L14 charges swap when both figures are given; the replay engine
charged none, silently, so `costs` meant two different things on the two paths.
Replay now counts nights with `swap.nights_between` — the same function
`simulate()` calls, **not** a second implementation — and
`test_financing_is_charged_identically_by_both_engines` pins it, including that
the charge is non-zero so the test cannot pass on a no-op.

### One configuration, two endpoints

The live run found the same class of problem at the API. `/v1/backtests` took
`{name, costs: {spread_points}}` and `/v1/replay/sessions` took a flat
`spread_points`, and dropped the two swap fields entirely. "A replay and a
backtest of the same configuration agree" was therefore a claim no caller could
actually express. The replay route now imports L14's `CostsIn`, and a test
asserts every `BacktestIn` field except `name` exists on `CreateIn`.

### Simulated time is the data, never the wall clock

`ReplayClock.now()` is the timestamp of the bar last revealed. That is the only
time a strategy sees during replay, which is what makes a replayed decision the
decision that would have been made then. The machine's clock is consulted in
exactly one place — `pacing_delay()`, how long to sleep so playback is
watchable — and it cannot reach a trading decision from there.

That separation is why **speed is provably free**: `advance()` returns the next
bar without consulting speed at all, so speed cannot skip, reorder or merge an
event. `test_speed_does_not_change_the_result` runs the same session at 1x and
100x and asserts the trades are identical.

### No look-ahead, on the same terms as L14

At bar i the strategy is handed `Candles.of(bars[:i+1])` and physically cannot
read further, because bar i+1 is not in the list it was given. ATR is computed
once, and `test_an_indicator_at_t_does_not_move_when_the_future_does` proves
that is safe rather than assuming it — replace the tail, and the values before
the cut are unchanged. `test_future_bars_cannot_change_an_earlier_replay_decision`
does the same for the decisions.

### Replay cannot reach a broker, structurally

The prompt asks that `execution_mode == REPLAY` force
`execution_provider = SIMULATED_EXECUTION_ONLY`, and that no configuration
override it. It is not implemented as a check, because a check can be wrong:
`ReplayEngine` holds **no broker adapter and imports none**. There is no
setting that could point a replay at a venue because there is no venue
reference in the package to point. A test parses every module under
`app.replay` with `ast` and asserts the absence of any import of
`app.brokers`, `MetaTrader5` or `mt5_paper` — parsed, not grepped, so a
docstring saying "no broker here" cannot satisfy it.

Confirmed live: a full 400-bar replay of EURUSD H1 against the real MT5
terminal left `trades` at 252, `orders` at 269, `executions` at 208 and
`positions` at 0 — **unchanged**. Every trade the replay produces carries
`execution_mode: REPLAY`, so a replay result cannot be pooled with a live,
paper or backtest record by accident.

### Verified live, not only in tests

Against 400 real EURUSD H1 bars, with the spread set to the **measured** median
of 3.0 points (`reports/cost_hurdle.json`) rather than assumed:

| Check | Result |
|---|---|
| step advances exactly one bar, simulated time from the data | 3 distinct ascending timestamps |
| the cursor does not move while paused | 70 bars, still 70 after 1.5 s |
| runs to completion in the background | 400/400, `finished` |
| **replay metrics == backtest metrics** | trades 7, win rate 0.5714, net points 0.0017, profit factor 1.6097, exit mix identical |
| another user's session | 404 on read, drive and trades |
| a finished session is terminal | 409 on start, resume and step |

### The database

Migration `0007_replay_paused` widens the `replay_sessions.status` CHECK to
admit `paused`. It is the only schema change, applied with zero drift and all
252 trades / 269 orders / 208 executions preserved.

`ReplayState` maps onto the vocabulary the table already had rather than
inventing a parallel one: `created = "queued"`, `completed = "finished"`,
`stopped = "cancelled"`. A second vocabulary would need translating at every
boundary, and the translation is where a state gets lost.

### Stated limitations

- **Risk is not enforced** — the risk engine is level 17. Every entry records a
  `not_enforced` decision rather than a fabricated approval, because a recorded
  approval that nothing performed would be worse than a recorded absence.
- **One symbol per session.** Multi-symbol and multi-timeframe are not
  implemented and are not faked.
- **Candle replay only.** Tick replay needs tick data the platform does not
  store, and interpolating ticks from OHLC would be fabricating market data.
- **The bracket is ATR multiples**, as in L14; a strategy's suggested stop is
  not used.
- **Fixed quantity only** until L18.

All five are listed at `GET /v1/replay/assumptions`.

### The frontend

`/replay` is still the L03 placeholder, matching L14's `/backtesting`. The
frontend is not built per level in this migration except where the level's own
subject is the UI, as L13's was.

## Level 16 — Paper trading engine, completed (2026-09-03)

### The audit found more than expected, and two dead modules

The schema was already built for this. `orders`, `positions`, `trades`, `bots`
and `portfolio_snapshots` all carry `mode IN ('paper','demo','live')`;
`orders.paper_account_id` and `positions.paper_account_id` already existed;
`orders.intent_id` was already `unique=True`; `paper_accounts`, `bot_runs`,
`bot_events`, `risk_events` and `signals.signal_key` were all there from L05.
So L16 took the **unified model** the schema had already chosen — one `orders`
table with `mode='paper'` — rather than a parallel set of `paper_*` tables.

Two modules were the surprise:

| module | state before L16 | what happened |
|---|---|---|
| `app/risk/engine.py` (439 lines) | complete, **imported by nothing, tested by nothing** | L16 is its first consumer, and brings its tests |
| `app/sizing/calculator.py` (187 lines) | complete, **imported by nothing, tested by nothing** | same |

Both were dead code. The only references to them anywhere were docstrings and
two *negative* tests asserting the strategy layer does **not** import them.
`MIGRATION_STATUS.md` described L17 as "all inline in `cycle()`", which was
stale — the lifted engine already existed. L16 wires both in and adds the
coverage they never had. That is why this level's test count is large.

`app/positions/` (executor, manager, monitor, policies) was already tested and
is untouched; it is exit-policy machinery for a later level.

### The safety boundary is a function signature, not a check

The brief asks for::

    if execution_mode == PAPER:
        execution_provider = PAPER_EXECUTION_ONLY

A check written that way can be skipped, short-circuited, or handed a mode
something mutated on the way in. So it is not a check. It is a **total function
whose only parameter is the mode**:

```python
def provider_for(mode: ExecutionMode) -> ExecutionProvider:
    return PROVIDER_FOR_MODE[ExecutionMode(mode)]
```

No settings parameter, no account, no credentials, no override. *There is no
parameter through which a configuration mistake could arrive*, which is what
makes "configuration must never override this" true rather than asserted. A
test reads the signature and asserts it is exactly `["mode"]`; another walks
every `ExecutionMode` member so a mode added later cannot fall through to a
default — and a default here would be a default execution path.

`app/paper/` imports no broker adapter, and `test_no_module_in_the_paper_package_can_reach_a_broker`
parses every module with `ast` to prove it. Parsed, not grepped: searching the
source text for `MetaTrader5` matches the docstrings saying there is none.

### Risk is not bypassable, and the type system is what enforces it

`PaperOMS.submit(approval, ...)` takes a `risk.Approval` as its **first
positional argument**, and `RiskEngine.approve` is the only thing in the
codebase that constructs one. So "nothing reaches the OMS without passing Risk"
is not a rule somebody has to remember — there is no way to call `submit`
without holding the Risk Engine's own token.

Three tests hold that line: calling `submit` with no approval is a `TypeError`;
calling it with an impostor (`None`, a string, a dict, a bare object) is
refused; and a **hand-built `Approval` wrapping a veto verdict** is refused
too, so the token cannot be forged by constructing the dataclass directly.

### The AI seat can only subtract

`AiFilter` sees a signal and returns `AiVerdict(accept, confidence, reason,
model)`. It runs **before** risk and cannot see the risk engine, the kill
switches, the account or the OMS. There is no field by which it could raise a
limit, and no order of operations in which its opinion could overturn a veto.
`test_the_ai_cannot_overturn_a_risk_veto` runs an AI that approves everything
against a risk engine that vetoes everything, and asserts no order exists. A
fourth test asserts `AiVerdict`'s field set, so a future field that could
approve something would fail the build. No model is wired in; that is L24.

### The mandatory live-execution test

`test_paper_execution_never_reaches_a_broker` runs a complete pipeline pass
with `MetaTrader5`, `MT5Adapter`, `BrokerAdapter` and `mt5_paper`'s
`place`/`close_position`/`connect`/`assert_demo` all replaced by a tripwire
that **raises on any call** — not a mock that records for later inspection, so
a pipeline that reached a broker cannot produce a passing test with an
unchecked assertion. It then asserts paper execution *was* called and produced
a real fill with `fill_source="simulator"`.

The configuration matrix is separate: `paper`/`demo` x `LIVE_TRADING` true and
false, plus MT5 credentials in the environment. `LIVE_TRADING=true` does not
promote paper to live; it is a gate on the live route, never a promotion of
another one.

### What live verification caught that the tests did not

Two real bugs, both found by running it against the real MT5 terminal:

1. **The pipeline used a naive clock.** `app.auth.models.utcnow()` returns a
   naive datetime *on purpose* — the `DateTime` columns are naive. The
   market-data layer is timezone-aware. Mixing them raised
   `TypeError: can't subtract offset-naive and offset-aware datetimes` on the
   first bar. Every test passed `now=` explicitly, so nothing exercised the
   default. Fixed with `app/paper/clock.py`: **domain time is `now_utc()`,
   storage time is `utcnow()`**, converted explicitly at the persistence
   boundary — plus a test that calls `process()` with no `now` at all.
2. **A global kill switch stopped running bots but did not stop new ones.**
   `start_bot` checked the emergency stop and the bot-scope kill, never the
   global/account/strategy switches. Every order would still have been vetoed
   by risk, so nothing unsafe could execute — but a kill switch that looks off
   when it is on is worse than one that refuses loudly.

A third finding is a real interaction rather than a bug: `RiskLimits`
defaults `max_signal_age_seconds` to 300, and an H1 signal is inherently up to
an hour old, so **every H1 trade is vetoed under the default**. Live, the
engine correctly reported `signal_freshness: signal is 1146s old, limit 300s`.
It is settable per bot and the live run sets it to 7200. The default belongs
to `app.risk` and should be timeframe-aware; that is L17's to fix.

### Money

`PaperPortfolio` does the accounting: open, add, reduce, close and **reverse**,
with the average entry recomputed on adds and a reversal closing the old side
before opening the remainder at the fill price. Quantity is checked with
`<= 0`, not `== 0`, so a rounding artefact can never leave a negative position
in the book.

**Value per price unit is measured, never assumed**: `tick_value / tick_size`
from the broker's contract spec, and a missing figure refuses the calculation.
This repository has already paid for the alternative — a lot sized from an
absent tick value is a real order for the wrong amount.

A position with no price contributes **0 to unrealised P&L and is reported in
`marks_missing`**, because an unpriced position is unknown, not flat.

The P&L test checks against arithmetic written out in the test, not against the
engine's own output: buy 2 at 1.10000, sell 2 at 1.10500, tick value 1 and tick
size 0.00001 gives `0.005 x 2 x 100000 = 1000` gross, two commissions of 7 off
the balance, and 986 realised.

### Execution

**The spread is charged by filling at the right side of the book**, not by
subtracting a number: a buy lifts the ask and a sell hits the bid, so a round
trip pays it exactly once — the same total L14 charges with its single
deduction, arrived at the way a venue arrives at it. Charging both would double
it, and a test pins the round trip.

Where the feed carries no bid/ask, the spread is **modelled** from the
`CostModel` and the fill is stamped `bid_ask=not_available`, so a number that
came from a model can never be read as one that came from the book. Slippage is
always adverse. An inverted book is refused rather than filled. A reference
with no price at all is refused rather than guessed.

### Idempotency, and what survives a restart

The signal key is `sha256(account | strategy | symbol | timeframe | bar_time |
signal_type)` and **is** the order's `intent_id`, which is `unique=True` on the
table. So a duplicate signal and a duplicate order are the same fact rather
than two mechanisms. A restart rebuilds `seen_signals` from the persisted
intent ids and reopens the positions the database says are open; if that set
were ever wrong, the unique index would still refuse the insert. Two
independent mechanisms, one of which survives a `kill -9`.

One pass is **one transaction**: the order, its events, its execution, the
position, the trades and the account balance are written together or not at
all. A fill with no position row would be a corruption nothing downstream could
detect.

### Verified live against the real MT5 terminal

| Check | Result |
|---|---|
| account starts `created` and untradeable | ✓ |
| lifecycle activate → pause → resume | ✓ |
| bot runs in a background task | 3 passes, no browser involved |
| pause holds the cursor | 2 passes, still 2 after 8 s |
| **signal → risk → sizing → OMS → fill → position** | **sell 0.01 @ 1.16084, risk decision `approve`** |
| the fill is a simulator fill | 1 simulator fill, **0 broker fills** |
| kill switch stops the bot AND blocks a restart | 409, "a kill switch is engaged" |
| another user | 404 on account, pause, trades and bot |
| reset requires confirmation, archives rather than deletes | 422 then `reset_count: 1` |
| the pre-existing demo record | **252 trades, unchanged** |

`trading_mode=paper`, `live_trading=false`, `live_execution_allowed=false`,
12 blockers — before and after.

### The database

Migration `0008_paper_accounts` adds `status`, `realized_pnl`,
`unrealized_pnl`, `commission_paid`, `reset_at` and `reset_count` to
`paper_accounts`. Purely additive, applied with zero drift and all 252 trades /
269 orders / 208 executions preserved.

`is_active` is **KEPT** and maintained in step with `status`. Dropping it would
break readers written against it for the sake of tidiness, and the brief is
explicit that working functionality is not removed. It is also insufficient on
its own: `created`, `paused` and `disabled` all read false while authorising
different operations.

A reset **archives in place**. Nothing is hard-deleted, because a trading
record that can vanish cannot be audited.

### Stated limitations

- **One symbol per bot.** Multi-symbol bots are not implemented and not faked.
- **Market orders only.** Limit and stop are in the schema; the engine submits
  market orders.
- **Trailing stops** are carried on the position but not advanced.
- **Financing** is charged by the backtester but not yet by the paper engine: a
  paper position's night count needs the bot to run across one.
- **Notifications (L34) and monitoring (L37) are not wired.** The events are
  published on the L07 bus and nothing subscribes yet.
- **Margin and leverage** are not modelled; the account has no margin figure to
  check against, and inventing one would be worse than reporting its absence.
- **The MT5 provider is synchronous**, so a slow fetch blocks the event loop.
  Pre-existing, and visible under a paper bot for the first time.

All are listed at `GET /v1/paper-trading/execution-model`.

### The frontend

`/paper` is still the L03 placeholder, matching `/backtesting` and `/replay`.
The frontend is not built per level in this migration except where the level's
own subject is the UI, as L13's was.

## Level 17 — Risk management & risk engine, completed (2026-09-03)

### The audit: the engine existed, and L16 had already found that

`app/risk/engine.py` was 439 lines of complete, well-designed implementation
that **nothing imported and nothing tested** — `MIGRATION_STATUS.md` still
described it as "inline in `cycle()`", which had been stale for some time. L16
became its first consumer and brought its first tests. So L17 did not build a
risk engine; it built everything a pure function cannot have.

| what | before L17 | after |
|---|---|---|
| the checks | 13, in one method | 26, modular by outcome, all **reported** |
| `market_open` | **declared as a `LimitKind`, never evaluated** | evaluated |
| nine limits when unset | absent from the decision entirely | reported `enforced=False` |
| decisions | a verdict object, not persisted | `risk_events` with id, codes, version, hash |
| a breach | a failing check, recomputed each time | a **latched state** that survives restart |
| configuration | constructor arguments | four scopes, most restrictive wins, versioned |
| concurrency | none | one lock per account plus reservations |
| failure | an exception | a refusal |
| replay | recorded `not_enforced` | evaluates, and a veto stops the entry |

### The defect at the centre: absent is not the same as unenforced

Nine limits — `max_daily_loss`, `max_drawdown`, `max_exposure`, `max_leverage`,
`max_risk_per_trade`, `max_trades_per_day`, `spread`, `duplicate_signal`,
`stop_loss_required` — produced **no record at all** when they were not
configured. A reader of a decision could not tell "this limit passed" from
"this limit does not exist here", which is this repository's founding doctrine
violated inside its own safety authority: *not measured is not the same as
fine*.

Fixed with a **completeness pass** rather than nine else-branches: after the
checks run, every `LimitKind` that produced no record gets one with
`enforced=False`. One place, so a limit added later is covered without anyone
remembering. A bare configuration now reports 26 checks, 23 of them not
enforced — which is a true and useful statement, where silence was neither.

`LimitKind.market_open` is the sharper version of the same fault: an enum
member with no check behind it reads, from outside, exactly like an enforced
limit. It is evaluated now, and an unknown market status **vetoes** rather than
assuming the market is open.

### Both defects L16 flagged, fixed

1. **`RiskEngine.evaluate` defaulted `now` to the naive `utcnow()`.** Every
   time it compares against is aware — a bar time, a signal time, an approval
   expiry. It raised the moment approvals gained an expiry, and the L16 paper
   tests caught it immediately. The engine's clock is `datetime.now(UTC)` now.
2. **`max_signal_age_seconds = 300` vetoed every H1 strategy.** A signal is at
   most one bar old by construction, so a fixed five-minute ceiling is
   unusable above M5 — live at L16 it reported `signal is 1146s old, limit
   300s`. The default is now `None` and `RiskService` derives it from the
   timeframe (two bars of slack). A **manual** order keeps no derived limit at
   all: it has no signal, and vetoing it for having no timestamp is a different
   thing from vetoing it for having a stale one.

### The approval is now bound to the order it approved

This was the real hole. `Approval` proved *that* risk agreed; it did not prove
*what* it agreed to. So risk could approve a 1-lot order and something could
submit a 10-lot order with the same token.

`Approval` now carries a `request_hash` — a digest of account, symbol, side,
volume, order type, price, stop, target, strategy and mode — and an
`expires_at`. `PaperOMS.submit` checks both. Changing **any** bound field
invalidates it, which a parametrised test asserts field by field, and an
approval computed against a portfolio a minute ago cannot be replayed against a
book that has moved.

### A breach is a state, not a check

The distinction is the whole of §19–22. A check is recomputed from a portfolio
snapshot, and a snapshot moves: a losing day followed by an unrealised bounce
would let trading resume on a limit that was **already breached**. So a
breached daily loss or drawdown **latches**:

| lock | clears when |
|---|---|
| `DAILY_LOSS_LOCKED` | the configured trading-day boundary passes, and only then |
| `DRAWDOWN_LOCKED` | an authorised human clears it. A drawdown is still there tomorrow |
| `EMERGENCY_STOP` | an authorised human clears it |
| `DISABLED` | an authorised human re-enables the account |

`refresh()` is the *only* automatic release in the module. A release always
records who authorised it — `clear(force=True)` without a name is refused. A
lock with no trading day recorded never clears on its own, because it cannot be
shown to have ended. And a weaker lock cannot displace an emergency stop.

The trading day is **explicit UTC** with a configurable boundary hour, never
the machine's local midnight. This project has already shipped that bug: a
daily limit that counted from 18:30 on a UTC+5:30 laptop and from midnight on a
UTC one.

Locks and kill switches are persisted in `risk_rules` and read back by
`load()` **before the service evaluates anything** — a process that died
holding a lock comes back holding it, and a test drives exactly that with a
second `RiskService` instance.

### Configuration: most restrictive wins, never most specific

Four scopes, and the combination is declarative per field: a cap combines by
**minimum**, a floor by **maximum**, a restricting boolean by **OR**.

The distinction matters, and it is why this is not a dictionary merge. Under a
merge, a strategy setting `max_risk_per_trade = 3%` would *override* an account
limit of 1%, because it is more specific. Here 1% wins because 1% is smaller —
verified live: global 2%, account 1%, strategy asks 3%, effective **1%**,
attributed to `paper_account:live-acct`. A more specific scope can tighten a
limit and can never loosen one, and a bot's own frozen configuration goes in as
a *layer* for exactly that reason.

Because min, max and OR are associative and commutative, "most restrictive
wins" is a property of the function rather than of the caller's ordering — a
test shuffles the layers and asserts the same answer.

An **unknown** limit name is refused rather than ignored, because an ignored
limit reads, to whoever set it, as an enforced one.

### Validation refuses; it does not clamp

A percentage above 100, a negative cap, a zero where zero would disable a check
— each is an error. Silently normalising an unsafe configuration produces a
system trading under limits nobody chose. Every problem is reported at once
rather than one per round trip, and cross-field coherence is checked too
(`max_trades_per_minute` above `max_trades_per_hour` is refused).

### The race condition

Two orders that are each individually safe can be collectively unsafe. With 40%
exposure and a 50% limit, two 8% orders evaluated against the same snapshot
both see headroom and both pass, landing at 56%.

An approval now **reserves** its exposure until the order fills, is cancelled,
or the approval expires, and the next evaluation counts the reservation. The
evaluation and the reservation happen under one lock per account, so they
cannot interleave. `test_two_concurrent_orders_cannot_both_take_the_same_headroom`
runs the brief's exact example through `asyncio.gather` and asserts exactly one
is approved.

The lock is honest about its scope, and the status endpoint says so: it
serialises **within this process**, which is where the bots run. It is not a
distributed lock. A second backend process would need row-level locking, and
this deployment does not have one.

### Fail closed

A check that raises, a configuration that will not load, and a decision that
cannot be persisted all produce a refusal with `outcome=ERROR` and
`RISK_ENGINE_ERROR`. There is no path through the service that approves an
order because something went wrong, and a test drives each fault. **An approval
nobody can audit is not an approval this platform makes** — so a persistence
failure is a refusal, not a logged warning.

### Risk runs in every mode

Simulation is not an exemption. Replay used to record `not_enforced`; it now
evaluates every entry through the same engine, and a veto stops the entry.

The subtlety is the clock: `now` is the **bar's** time, not the wall clock.
Measured against the machine, every signal in a historical replay is years
stale and nothing would ever trade. Simulated time is the only time the
strategy sees, and it is the only time risk sees too.

Replay's **default** limits are permissive, and that is a deliberate choice
rather than a weak one: `test_replay_matches_the_backtest_exactly` compares a
replay against `simulate()`, which has no limits at all, so imposing some by
default would stop replay reproducing the backtest — the guarantee L15 exists
to make. Pass `risk=` to replay a strategy under real limits and watch the
vetoes; a test does exactly that and asserts no trade survives a kill switch.

**Backtesting is deliberately different, and this is the documented
difference:** `tools/rule_backtest.simulate()` applies no risk limits. It is a
measurement instrument, and a backtest that silently refused trades under
today's limits would not be measuring the rule — it would be measuring the
rule *and* a configuration that did not exist when the data was recorded.

### Verified live

| Check | Result |
|---|---|
| checks the engine can enforce | 29 kinds, 20 configurable limits, 36 rejection codes |
| a preview evaluates and executes nothing | 26 checks run, 22 reported not enforced |
| an explicit reason, never "rejected" | `STOP_LOSS_REQUIRED`, with the detail |
| **most restrictive wins** | global 2%, account 1%, strategy 3% → **effective 1%** |
| an unsafe configuration | 422, "is a percentage and cannot exceed 100" |
| a breached daily loss | `HALTED`, `MAX_DAILY_LOSS_EXCEEDED` |
| a kill switch | blocks; **1 persisted row**; releases cleanly |
| a plain user changing risk policy | 403 on config, kill switch and decisions |
| the record | 252 demo trades, unchanged |

`trading_mode=paper`, `live_trading=false`, `live_execution_allowed=false`,
12 blockers — before and after.

### The database

Migration `0009_risk_decisions`, purely additive:

- `risk_rules.rule_type` gains `limits`. The table already had the scope
  hierarchy and a priority; what it lacked was room for a *layer* — the several
  limits one scope sets. The existing narrow types are KEPT and still valid.
- `risk_events` gains `decision_id` (unique), `paper_account_id`, `mode`,
  `request_hash` and `configuration_version`. The full decision still lives in
  `snapshot`; these are the fields a caller filters or joins on, because
  "this account's rejections today" over a JSON field is a scan.

Applied with zero drift; 252 trades / 270 orders / 209 executions preserved.

`decision_id` is nullable because rows recorded before L17 have none, and a
backfilled value would be an invented identifier for a decision nobody made.

### Stated limitations

- **Margin and leverage are checked only where the account reports them.**
  Paper accounts do not carry margin, so the check reports itself unenforced.
  Fabricating a margin model would be worse than reporting its absence.
- **No currency conversion.** Exposure is summed per currency and never
  converted; a multi-currency account would need a rate this platform does not
  have, and inventing one is the metals-points error in a new place.
- **Correlated exposure is not implemented.** The extension point is
  `PortfolioState.exposure_by_currency`, which already groups the seven USD
  pairs that move together, but no correlation matrix is computed and none is
  fabricated.
- **The concurrency guard is per-process.** Documented at `/v1/risk/status`.
- **No risk UI.** `/risk` remains the L03 placeholder, matching `/backtesting`,
  `/replay` and `/paper`.
- **Backtest applies no limits**, by design, as above.

All are listed at `GET /v1/risk/limits` and `GET /v1/risk/status`.

---

# Autonomy audit — TradingView → platform, the master automation brief

2026-09-03. **Audit and design only; no code changed.** The brief's §43 and
§44 are explicit that the autonomous system is not implemented in one
operation: inspect the repository, design the orchestrator, create the
architecture documents, create the plan. That is what this entry records.

## Suites

Run before, and unchanged after, because nothing was modified:

| Suite | Result |
|---|---|
| research (root) | **34 passed** |
| backend (from `backend/`) | **900 passed, 3 skipped** |
| frontend (from `frontend/`) | **77 passed** (15 files) |

## What the audit found

**Sixteen of the twenty capabilities the brief names already exist.** The chain
from a strategy signal to a paper position is complete, and its safety
properties are structural rather than procedural — the OMS requires a
`risk.Approval` whose only constructor is `RiskEngine.approve`; `provider_for`
takes the mode as its sole parameter so configuration cannot reach the route;
`Candles.closed` removes the forming bar before any strategy sees it.

**No TradingView → MT5 shortcut exists.** Verified by import graph, not by
docstring: nothing in `app/paper`, `app/replay`, `app/backtest` or
`app/strategies` imports `app.brokers` or `MetaTrader5`. There is nothing to
unpick, so the work is to build the chain rather than refactor a bypass.

**One real gap, and it is not a TradingView gap.** All three runners resolve
strategies through `registry.create()` — `app/backtest/service.py:180`,
`app/replay/service.py:137`, `app/paper/service.py:351` — which knows only
classes registered at import. A `BuiltStrategy` is constructed from data, so a
definition saved by the L13 builder **cannot be backtested, replayed or
paper-traded today**, and a compiled TradingView strategy could not be either.
It is scheduled as A3, ahead of the compiler: build the runway before the
aircraft.

**What is missing:** Pine parsing (nothing parses Pine), the compiler, the
orchestrator, the build-time LLM interpreter, per-strategy test generation, the
validation harness, Sharpe and Sortino, and the machine-readable state file.

**What is blocked:** the alert → execution bridge (A8). The L09 gateway
publishes `SIGNAL_CREATED` and nothing consumes it, which is correct — brief
§20 wants the 13-state order machine with idempotency and no blind retry out of
`unknown`, and that is L19. Building the consumer first would mean a second OMS
or an execution path predating its own guard.

## The design decisions worth keeping

- **The compiler targets `StrategyDefinition`.** L13's declarative definition
  is already the internal strategy interface brief §11 asks for: validated,
  round-trippable, refusing unknown fields and refusing `PRICE > RSI` on units.
  A second internal shape would be a second strategy engine by another name.
- **`TradingViewSpec` stays separate** because it must record what *cannot* be
  run. A definition has no field for an unsupported feature, and a spec that
  dropped them would let an operator believe a session filter was in force when
  it was not.
- **The Pine subset is declared and small** — four indicators, five price
  fields, seven comparisons, AND/OR/NOT, single-assignment variables. Anything
  else is recorded in `unsupported_features` and the compile is refused.
  `request.security` is refused by name, because it repaints and is the standard
  route to a look-ahead an equity curve cannot show.
- **Brackets leave the definition.** Pine's stop becomes configuration for the
  L21 position manager, not a rule inside the strategy, so there is never a
  second exit authority racing the first.
- **Validation is not "did the backtest make money".** G5 measures, G6 asks
  whether the result separates from the strategy's own permutation null across
  era blocks, a walk-forward and date clustering. TradingView's own Strategy
  Tester figures are treated as an unverified claim and the divergence against
  this platform's measured spread is reported — that divergence is the most
  useful number the whole import produces.
- **Autonomy ends at PAPER.** G1–G8 unattended; APPROVED needs a person.
  `TRADING_MODE=paper`, `LIVE_TRADING=false`, seven of ten `LIVE_GATES` false,
  and nothing in this plan flips any of them.

## Documents written

`TRADINGVIEW_ARCHITECTURE.md`, `TRADINGVIEW_DISCOVERY.md`,
`STRATEGY_SPECIFICATION.md`, `STRATEGY_COMPILER.md`,
`STRATEGY_VALIDATION.md`, `DECISIONS.md`, `CHANGELOG.md`,
`PROJECT_STATE.json`. `MIGRATION_STATUS.md`, `IMPLEMENTATION_PRIORITY.md`,
`PROJECT_AUDIT.md` and `ARCHITECTURE_MIGRATION.md` were appended to, never
rewritten.

## The one input required

Per brief §41: TradingView publishes no public read API, so there is nothing to
connect to. Of the four legitimate sources, only Pine text describes a rule
set — alerts describe a decision, exports describe a result. **A0–A2 can be
built and tested against synthetic Pine fixtures; compiling a real strategy
needs one `.pine` file the user owns or that is published open-source, plus the
symbol and timeframe it ran on.** Nothing else is blocking.

## Next

**Level 18 — Position sizing engine**, unchanged. The autonomy ladder inserts
after it: L18 → L19 → A3 → A0 → A1 → A2 → A4 → A6 → A7 → A9 → A5 → A8 → L38.


# ============================================================ LEVEL 18

# LEVEL 18 — POSITION SIZING ENGINE (2026-09-03)

Full write-up in `LEVEL_18_POSITION_SIZING.md`. The short version.

## What the audit found

The separation of concerns was already right. `app/risk/` computes no
quantity — `approve()` reads `proposal.volume` and vetoes — so brief §12's
"extract sizing from the Risk Engine" had nothing to extract. `app/paper/oms.py`
already took an `Approval` as its first positional argument, `app/brokers/` was
already the only path to a venue, and `ContractSpec` was already the single
source of contract terms. No second engine of any kind existed to merge.

What existed for sizing was `app/sizing/calculator.py`: three modes, one
consumer since L16, and **no tests of its own**. `tests/test_paper.py` proved
the paper engine *called* it. Nothing proved it computed correctly.

## The two defects, which is why this level was not a documentation exercise

**A size below the broker's minimum was raised TO the minimum.** The overshoot
went into a `gap` string and `result.ok` stayed true, so a caller that did not
read `gap` traded a position risking more than its budget — a 1.00 budget on an
instrument whose minimum lot risks 5.00 became a 5.00 trade. It contradicted
the project's own `precision.normalize_quantity`, which already refused that
exact case with the exact reasoning. Now refused, naming what the minimum lot
would have cost.

The maximum is treated the opposite way on purpose: it binds downward, which
can only reduce risk, so it is applied with a warning. That asymmetry is the
rule.

**Stop direction was never validated.** The engine only ever saw
`abs(entry - stop)`, and `abs()` makes a target indistinguishable from a stop,
so a long order with its stop above its entry sized normally. This repository
has already paid for that shape once — position 10200315596, the NZDUSD sell
whose stop and target both ended up above the entry after a 279-point fill
discrepancy, where every branch was a loss and the retcode said DONE. Now
refused, not corrected: flipping the stop and flipping the side both change
what the caller asked for.

## The decision worth arguing about

**Three modes, not seven.** `fixed_lot` IS `fixed_quantity` — on every
instrument this platform trades, MT5 included, the quantity is the lot.
`monetary_risk` IS `fixed_risk`. Stop-loss sizing IS both risk modes, neither
of which will size without a stop. ATR sizing is not a mode at all: it is this
arithmetic over a stop the caller derived from ATR, which is exactly what
`PaperEngine._bracket` and the backtester already produce — a separate mode
would put the bracket calculation in two places. Broker-constrained sizing is
not a mode either; it is applied to every result, because a volume the venue
will not accept is not a size.

The brief's names are accepted as aliases and resolved at the edges, so the
vocabulary is available without a fourth branch. `GET /v1/position-sizing/modes`
documents the mapping rather than hiding it.

## The bug the new tests found

Writing `test_the_step_count_is_rounded_before_flooring` surfaced a real
interaction. `precision.steps_in` treats a step count within `STEP_EPSILON` of
a whole number as that whole number — the guard against the float floor this
repository already documents. That guard can round the count UP, so the final
volume can exceed the budget by at most an epsilon of one step, and the new
"actual risk must not exceed the budget" rule was refusing a valid 5-step order
over 1e-10 of currency. That is the float-floor defect returning wearing a
refusal.

The fix is a tolerance tied to the same constant that creates the discrepancy —
`STEP_EPSILON x volume_step x risk_per_unit`, the exact bound of the guard —
and a comparison on the full-precision figure rather than the four-decimal
reported one. Not a fudge factor: anything above that bound is a real overshoot
and still refuses.

## Backtesting

`fixed_risk` and `percent_equity` went from "declared but refused" to wired,
through **the same engine the paper pipeline uses**. No `BacktestPositionSizer`
was created; a test parses the runner and fails if one appears.

No look-ahead, and both inputs are checked by tests. The stop distance is
`stop_atr x atr[entry_idx - 1]` — the exact figure `simulate()` itself used to
place the bracket, from the bar *before* the entry. The equity is the running
balance from trades that had already closed; the first trade is always sized at
the initial capital exactly.

An entry the engine cannot size is **not a trade**. It is removed from the
result and reported under `sizing_refusals`, because counting it would report a
return the account could not have earned.

One thing this exposed: the runner accumulated unrounded floats for its own
walk while the equity curve accumulated rounded ones, so the two balances
drifted. Both now use the same figure.

## What was NOT done, and why

**Replay stays fixed-quantity.** Its engine carries one quantity on the
portfolio and is proved trade-for-trade identical to `simulate()`. Making the
size vary per position changes that engine rather than configuring it, and the
equivalence proof is worth more than the feature. The replay body still accepts
the same shape as a backtest — a shared shape a test asserts — and **refuses** a
risk mode with that reason. Accepting the field and ignoring it would report a
flat-lot replay the caller believes was risk-sized.

**No per-instrument calculator classes.** `tick_size` and `tick_value` from the
measured spec already express every instrument class traded here; four classes
all computing `distance / tick_size x tick_value` would be the duplication the
brief warns against.

**No new event type.** The L07 catalogue is a fixed vocabulary with asserted
counts. Sizing rides in the order payload, the risk decision and the metrics.

## Tests

Baseline: backend 885 passed / 15 failed / 3 skipped, research 34, frontend 77.
The 15 failures are `redis.exceptions` — Docker was not running on this
machine — and are environmental. After: backend 962 passed / 15 failed (the
same ones) / 3 skipped, research 34, frontend 81. Ruff, ruff-format and mypy
clean.

`tests/test_sizing.py` is 59 tests where there were none, covering the brief's
30 cases plus determinism and the architecture fences. The brief's own worked
example (entry 100, stop 98, tick 0.01/0.01, risk 100 → 50 units) is asserted
literally.

## Next

**Level 19 — the Order Management System.** It is the other half of what
unblocks A8, and the doctrine holds: the veto (L17) and the measurement (L18)
are both built before the path they guard.


# ============================================================ LEVEL 19

# LEVEL 19 — ORDER MANAGEMENT SYSTEM (2026-09-03)

Full write-up in `LEVEL_19_OMS.md`. The short version.

## What the audit found

A working OMS. `app/paper/oms.py` already required an `Approval` as `submit`'s
first positional argument — and `Approval` has no constructor in this codebase
outside `RiskEngine.approve`, so "nothing reaches a broker without passing
Risk" was already a type rather than a check. `intent_id` idempotency, the
transition table, and `unknown` as a state nothing retried out of were all
there. So were the adapter, the fake with its fault injector, the reconciler
that reports and never repairs, and the three tables.

The brief's §12 instruction to refactor MT5 calls out of the OMS had nothing
to refactor: there were none.

## The four gaps, and why each mattered

**The state machine was private to the paper package.** Extracted to
`app/oms/state.py` and re-exported, so nothing downstream changed. A test now
asserts the paper OMS and the broker OMS hold the *same object* — two machines
that disagree about whether `accepted → cancelled` is legal is how a cancel
succeeds in paper and corrupts an order in demo.

**Four real outcomes had nowhere to go.** `submitting` is persisted before the
venue call, which is the only thing that makes a crashed send distinguishable
from a send that never happened. `cancel_requested` separates "we asked" from
"the venue agreed". `expired` is reached only on confirmation. And `failed` is
deliberately **not** a synonym for `rejected`: a failed order never reached the
venue and is safe to re-send, a rejected one is not, and an unknown one must be
reconciled first. Collapsing those three loses exactly the distinction that
decides whether a retry is safe. `SAFE_TO_RESEND` is the one-element set
`{failed}`, and a test walks every state to prove it.

**Requested and filled were the same number.** `FillBook` separates them, and
the average price is quantity-weighted and recomputed from the whole list, so a
restart that reloads fills from the database gets the same figure. An overfill
refuses rather than truncating — truncating would record less than the venue
reported and leave the difference on the book with nothing pointing at it. A
repeated deal id is ignored, because execution reports arrive twice and a deal
applied twice is a position twice the size.

**Ten declared events had no producer, and the audit trail could not be
ordered.** The second was found by a test. Four transitions inside one clock
reading carry identical `occurred_at`, and the primary key is a uuid, so
without a per-order `sequence` the trail is unorderable — which means it is not
an audit trail. That column is the most load-bearing thing in migration 0010.

## The decision worth arguing about

**One lifecycle core, two venue bindings.** `state.py` and `fills.py` are
shared; `OrderManager` binds them to a broker adapter and `app/paper/oms.py`
binds them to the in-process provider. Merging the two into one class means
making the paper path async, which ripples through `PaperEngine` and its 93
tests — a rebuild of working code. What §16 forbids is a second *set of rules*,
and there is one set. Demo and live are the same binding; they differ only in
which adapter an operator registered, which is exactly what "one OMS with
controlled execution modes" asks for.

## `POST /v1/orders` is built, and is not a shortcut

It runs the same gates a bot signal runs, in the same order, through the same
objects — including reaching risk the way the paper service does, so it cannot
hold a looser copy of the limits. The client's quantity is an *input* to
sizing, not the quantity traded: a size below the venue minimum is refused,
never raised to it. And the registry is empty until an operator registers an
adapter, so in this deployment every broker-bound route refuses with that
reason. That is the honest description of a platform in paper mode with ten
live gates false.

## What was NOT done

**Limit and stop orders reach the venue as market orders.** `OrderRequest` has
no price field; MT5 pending orders need one. The type is validated and stored,
and the adapter extension is broker work. Saying "limit orders work" would be
the fake execution result the brief forbids, so it is a stated limitation.

**Startup recovery is built and not called.** `load_unresolved` and `resume`
exist and are tested; wiring them belongs with the supervised runner (L22) and
the startup sweep (L38).

**The live gates stay false.** `order_idempotency` and
`unknown_status_reconciliation` are built here and held false exactly as L17's
and L18's were. Flipping a gate is an operator decision taken in one reviewed
pass, not a side effect of the level that wrote the code.

## Next

**Level 20 — the automated execution engine**, the orchestration layer over
everything L17–L19 built.


# ============================================================ LEVEL 20

# LEVEL 20 — AUTOMATED EXECUTION ENGINE (2026-09-03)

Full write-up in `LEVEL_20_AUTOMATED_EXECUTION.md`. The short version.

## What the audit found

Almost the whole pipeline, and one missing link. The gateway records signals,
the strategy engine produces them, and the AI seat, risk, sizing, the OMS, the
adapter, the position manager and the supervised worker base all exist. The
paper engine is already a complete pipeline for one of the two signal sources,
and the paper service already runs it in background tasks that survive a closed
browser.

**`SIGNAL_CREATED` had no consumer.** Two producers published it and both said
so in their own docstrings. A TradingView alert became a row and stopped there.
That is precisely the A8 gap the priority document recorded as blocked on the
order state machine — which L19 built.

## The decision worth arguing about

`PaperEngine` could have been widened to take an external signal. It was not.
The two pipelines answer different questions: one PRODUCES a signal from bars,
the other CONSUMES a signal produced elsewhere. A class doing both is two jobs
wearing one name, and the externally-arriving path has failure modes the
strategy path cannot have — a malformed payload, an unknown strategy, a late
alert, an unauthenticated source.

What they share is every gate, and those are called rather than copied. The one
thing shared by extraction is the `Outcome` vocabulary, moved out of the paper
engine and re-exported from it. That is not tidiness: a paper bot reporting
`risk_vetoed` and an orchestrator reporting something else cannot be added
together, and the first dashboard built over them would silently under-count
one of the two.

## The distinction the level turns on

`NO_ORDER` and `ORDER_UNSETTLED` are deliberately separate sets. **"Nothing was
sent" and "something was sent and we do not know what happened" are the two
facts an operator must never confuse**, because only the first is safe to
retry.

That distinction decides what happens to the signal row. Most refusals consume
it — leaving a vetoed signal in `new` would re-run the same veto every two
seconds forever, and a disabled strategy's backlog would all fire the moment it
was re-enabled. But `execution_unknown`, `no_venue`, `spec_incomplete` and
`strategy_error` do NOT consume it: those are conditions that can clear, and
marking them finished would throw a signal away because a dependency was
briefly down. `status_for` returns `None` for exactly those four, and a test
pins the set.

## Browser independence, and why it is a worker

Brief §20 says not to execute inside the HTTP request. The reason is stronger
than latency: an alert executed inside the request is an alert whose execution
is lost if the connection drops after the gateway wrote the row. The gateway's
job ends at "this signal is recorded"; the worker's starts there, and the
`signals` table is the handover.

The claim and the status change are one transaction with `FOR UPDATE SKIP
LOCKED`, so two workers cannot both take a signal. That is the first of four
guards, and the only one that survives two processes — `seen`, `guard_resend`
and `intent_id UNIQUE` are the other three.

A test drives a recorded signal to a simulated fill with no HTTP request in
flight anywhere.

## What was NOT done

**The pipeline runs on the risk engine's defaults, not the account's stored
configuration.** `RiskService.limits_for` is the right source and the wiring
belongs with the bot manager (L22), which owns per-bot configuration. Defaults
are the more restrictive reading, so this errs safe, and it is stated rather
than hidden.

**The entry price comes from the alert's own `price_reported`.** The correct
source is a live quote at execution time, which needs the market-data
subscription L08 lists as missing. Recorded as a limitation, because the
alternative — inventing a price — is the failure this repository exists to
prevent.

**The worker is registered and not started.** Starting it is an operator action
(`POST /v1/execution/start`), and with no adapter registered every signal parks
as `no_venue`. That is the correct behaviour of a deployment nobody has pointed
at a venue, not a bug.

**`tools/run_overnight.py` and `take_profit.py` are untouched.** They are the
research toolkit's demo loop, not the platform's execution path, and rewriting
them would change the tool every figure in `CLAUDE.md` was measured with.

## Next

**Level 22 — the bot manager.** It owns START/PAUSE/STOP, per-bot
configuration, PID and heartbeat supervision, and it is what will hand the
execution pipeline the account's real risk configuration.



# ============================================================ LEVEL 21

# LEVEL 21 — POSITION MANAGEMENT (2026-09-04)

Full write-up in `LEVEL_21_POSITION_MANAGEMENT.md`. The short version.

## What the audit found

The paper side was already done and already right: seven exit policies with an
explicit priority order, a trail that only ratchets, and a close contract that
refused to mark a position closed without a confirmed fill.
`MIGRATION_STATUS.md` recorded one gap — the demo/live executor — and it was
accurate. L10 built the adapter, L19 built the OMS, so it was buildable.

## The gap, closed

**Demo and live positions could not be closed at all.** Every attempt returned
"no broker adapter is built yet (level 10)". `BrokerExitExecutor` closes
through the account's order manager to the adapter, holds a registry rather
than an adapter (an adapter handed out for the wrong account closes the wrong
position), and imports nothing MT5-shaped — a test parses it to prove that.

Its third outcome is the one that matters: an unclear answer parks the
position and **nothing retries it**. Retrying an uncertain close is how a
position gets closed twice, which on a hedging account opens a new one in the
opposite direction.

## The distinction the level turns on

**What we intend and what the venue holds are two facts, and they now have two
columns.** `stop_loss` is the platform's intent; `broker_stop_loss` is what
the venue last reported. A silent disagreement between them is a position
running unprotected while the screen says protected — the most dangerous thing
this layer can observe.

So it is recorded on every pass it is true, logged at ERROR when the venue
holds no stop at all, and rendered in the table as `1.09000 ✗` rather than as
a number that looks fine. And `broker_synced_at` of None means NEVER READ,
which is not the same as absent: reporting that as a mismatch would cry wolf
on every position before its first sync.

## Four states, and why each had to exist

`opening` — nothing has confirmed, so a position that exists locally may not
exist at all. `partially_closed` — open at a size that is no longer the size
that was risk-sized. `closing` — we asked and the venue has not answered.
`reconciling` — somebody is looking *right now*, which is deliberately
distinct from `unknown`, where nobody is; only the second means an answer is
coming.

A management pass acts on `open` and `partially_closed` and refuses the other
four, each with its own reason.

## Partial exits, and the thing that must not be overwritten

`quantity` is what is open now; `initial_quantity` and `closed_quantity` are
the history it was cut from. Separate columns, because "70 open" and "100
opened, 30 closed" are different facts and only the second can be audited.

A close larger than what is open is **refused, not clamped** — the difference
between closing 30 of 70 and 30 of 100 is a position size nobody chose. That
is enforced in the executor, in the API, and by a database CHECK, so an
over-close cannot become history even if a caller finds a way past the code.

## The defect the constraint caught

Adding `CHECK (closed_quantity <= initial_quantity)` immediately failed four
tests, and it was right to. `initial_quantity` defaulted to 0, so every
position claimed to have opened at nothing — which would have made every
partial rule refuse and every close violate the constraint. The fix is a
backfill from the current quantity at three places: the migration, the paper
service that creates rows, and the manager itself before it books a fill. That
last one is the important one: it means a row created by any writer that
forgets the column is still correct.

## The priority rule worth stating

A trail and a break-even can both propose a stop move on the same tick.
**The most protective proposal wins, not the first.** Taking whichever policy
ran first would make the outcome depend on list order, which the brief
forbids; taking the most protective is deterministic and cannot loosen
anything, because each policy has already refused to propose a loosening of
its own.

## What was NOT done

**`POST /protect` sets the platform's intent; it does not push the level to
the venue.** MT5 attaches SL/TP to the position rather than to separate
orders, so pushing it is real mapping work rather than a call. The
disagreement that creates is exactly what `broker_stop_loss` and
`protection_gap` now make visible, which is the honest intermediate state
rather than a silent one.

**Unrealised P&L is not stored, deliberately.** It is a function of a price
that changes every tick, and a stored one is wrong the moment it is written.

**Hedging vs netting is not modelled.** The platform tracks positions
individually with a broker id, which is the hedging shape and what this
broker's demo account uses. Adopting netting without an account to measure
against would be guessing at a behaviour rather than implementing one.

## Next

**Level 22 — the bot manager.** It owns START/PAUSE/STOP and per-bot
configuration, and it is what will start the position monitor and the
execution worker rather than leaving both registered and idle.



# ============================================================ LEVEL 22

# LEVEL 22 — AUTONOMOUS BOT MANAGER (2026-09-04)

Full write-up in `LEVEL_22_BOT_MANAGER.md`. The short version.

## What the audit found

`MIGRATION_STATUS.md` said **NOT STARTED**, "one loop at a time, manual
launch". Both halves were wrong, and it is worth saying why the status table
was the thing that needed correcting rather than the code.

`bots`, `bot_runs` and `bot_events` have existed since L05, and `bot_runs`
already carried `pid`, `host`, `heartbeat_at` and `stop_reason` — the exact
columns a supervisor needs, sitting there with nothing reading them.
`PaperService` has run a full START/PAUSE/RESUME/STOP lifecycle since L16, in
`asyncio` tasks the backend owns rather than a browser tab. `app/workers/base.py`
has provided a heartbeat, a staleness rule and cooperative shutdown since L02.

So "bots must survive a closed browser" — the reason this item was HIGH
priority — was already true. What was missing was not a runner. It was
something that **checks**.

## The defect

`PaperService.pause_bot` set the in-memory status to `paused` and wrote
`stopping` to the durable row, because `BOT_RUN_STATUSES` had no `paused`.

The in-memory value was right and the persisted one was wrong, which is the
worse of the two arrangements: it looks correct for exactly as long as the
process lives, and stops being correct at the moment the process does. A paused
bot and a bot shutting down need **opposite** treatment after a restart — one
preserved, one treated as an orphan — and one value for both makes that decision
unmakeable. Nothing reported the ambiguity, because from the row's point of view
there wasn't one.

## The rule the whole level turns on

**"The database says RUNNING" is not evidence that a bot is running.**

A status column says what the last process to touch it believed, and a process
that dies mid-run touches nothing. So every crash leaves a row reading
`running` — the one case where the column is both wrong and reassuring. The
heartbeat is written by something alive, so its silence is evidence rather than
the absence of it.

`BotSupervisor` marks a silent run `crashed` and stops there. Restarting is a
**separate pass**, because folding the two together makes the second decision
invisible, and restarting an automated trader is precisely the decision that
must be visible.

## Recovery refuses by default

With no safety check wired, every recovery attempt is refused and names the
missing check as the reason.

The alternative default is "restart unless told otherwise", and it is the most
dangerous line that could be written in this package: it turns an unconfigured
supervisor into one that restarts bots into an account whose kill switch is on,
whose orders are unresolved, or whose broker is disconnected. Absence of a check
is absence of evidence, not permission. The cost is that recovery does nothing
until somebody wires it — paid in a log line rather than in a position.

`halted` is not recoverable at all. A kill switch is a decision somebody made,
and recovering out of it automatically is exactly the bot-level bypass the brief
forbids.

## Limits that can only tighten, by arithmetic

`BotLimits.effective(account)` returns the more restrictive figure in every
field, so the invariant holds by the shape of the function rather than by a
check somebody must remember to run.

Validating at write time and rejecting a loose figure fails silently later: a
limit that was legal when saved becomes illegal the moment the account tightens,
and nothing re-checks it. Combining at read time cannot produce a looser number
than either input no matter what is stored.

`cooldown_seconds` is the one field where the **larger** value wins, for the
same reason the others take the smaller — a longer cooldown is the stricter one.
Getting that backwards would let a bot shorten a cooldown its account imposed.

Counters are read from the database on every check and never cached. An
in-memory counter comes back as zero after a restart, handing a bot that had
spent 9 of its 10 daily trades a fresh ten.

## Three new states, and one deliberately not added

`paused` — see above. `recovering` — a supervisor is acting on this **right
now**, which is distinct from `crashed`, where nobody is; only the first means
an answer is coming. That is the same distinction `reconciling` draws from
`unknown` at L21, adopted on purpose so the two layers read the same way.
`disabled` — barred until explicitly re-enabled, distinct from `stopped`, which
anyone may start.

`created` was considered and rejected. A `bot_runs` row exists because a run was
**attempted**; inventing one for "configured but never started" would make the
table overstate how many times a bot has run.

The brief's `ERROR` was not adopted either. This project has always called that
`crashed`, and `halted` is its word for "a kill switch stopped it" — neither a
crash nor a stop. Renaming existing values to match a document would rewrite
what every historical row means.

## The latent bug this found

Importing `app.paper.service` before anything else raised `ImportError: cannot
import name 'AiFilter' from partially initialized module 'app.paper.engine'`.

`paper.engine` → `app.execution.outcome` → `app/execution/__init__.py` →
`pipeline` → back into a partially initialized `paper.engine`. A genuine cycle,
introduced at L20, invisible only because the test suite's import order never
hit it — which is the kind of defect that surfaces first in production, where
import order is decided by whichever entry point runs.

The fix moves `AiVerdict` and `AiFilter` to `app/execution/ai.py` rather than
reordering imports, which would have hidden it. The seat is not a paper concept:
the orchestrator uses it and a demo bot would, and a module two pipelines depend
on cannot live inside one of them. Re-exported from its old home, so no caller
changed.

## What was NOT done

**Only paper has a runner.** The OMS (L19) and the broker executor (L21) are
built, but nothing drives a *strategy loop* against them, so a demo or live bot
has nothing to start. `preflight` refuses those modes and names what is missing
rather than starting a bot that cannot trade. This is A3's territory, and L22
sharpens the case for it.

**The supervisor worker is registered and not started**, like the execution
worker and the position monitor before it, and for the same reason: beginning to
act autonomously is an operator action. `POST /v1/bots/supervise` runs one sweep
on demand, so the mechanism is usable before anybody turns the loop on.

**No safety check is wired to the supervisor**, so every recovery refuses.
Wiring it means asking the risk service about kill switches and the OMS registry
about unresolved orders — both exist, and connecting them is a deliberate step
because the failure mode of getting it wrong is a bot restarted into an unsafe
account.

**Bot limits gate the API and manager paths, not yet the paper runner's own
loop.** The runner builds a risk engine per bot from the account's limits;
threading the bot's tighter figures into that engine is the next wiring step.
Stated rather than implied.

**Scheduling (§46) is not built.** No start/end time, session or timezone
window. The platform has no scheduler to reuse, and adding one for a feature
nobody has asked to configure is the speculative work this brief warns against.

## What the full run caught

Every targeted run passed and the full suite then failed three tests in
`test_auth.py` that this level never touched — one of them a real
authorization regression I had shipped.

`GET /v1/bots` was a 501 stub gated on `manage_bots`, so a plain USER got 403.
The router replacing it asked only for a logged-in user, so a fresh USER
account could list every bot, its mode, its limits and why it last stopped.
`RESOURCE_MIN_ROLE["bots"]` has said TRADER since L04 and the frontend nav
mirrors it; the backend had quietly stopped agreeing. Reads now ask for the
same permission as writes.

The rule that came out of it is now a test —
`test_building_a_group_does_not_loosen_the_gate_it_replaced`: **a built group
inherits the gate of the stub it replaces.** The other two failures were
stale rather than broken, and they follow the pattern L06, L10 and L12 already
set: a group that becomes real drops its unversioned alias and leaves the
pending-routes list, with a comment recording which level removed it.

Also fixed in passing: five pre-existing `mypy` errors in L20/L21 test files,
and three `nav.ts` rows still calling `/orders`, `/positions` and `/bots`
`planned` — whose documented meaning is "shell only; every control disabled",
which stopped being true at L19, L21 and L22 respectively.

## Test counts

| Suite | Before | After |
|---|---|---|
| Backend | 1140 passed, 15 failed, 3 skipped | **1187 passed, 15 failed, 3 skipped** |
| Frontend | 83 | **84** |
| Research toolkit | 34 | 34, untouched |

The 15 are the same 15 in both columns: `redis.exceptions`, because Docker was
not running here. Environmental, and named as such rather than counted as a
pass. Lint, format and types are clean on both stacks.

## Next

**Level 23 — the AI data pipeline.** The first level of this run that is a
genuine fresh start rather than an audit of something already half-built: the
execution spine 18 → 22 is closed, and 23 begins the data side.


# ============================================================ LEVEL 23

# LEVEL 23 — AI DATA PIPELINE (2026-09-04)

Full write-up in `LEVEL_23_AI_DATA_PIPELINE.md`. The short version.

## What the audit found

Most of this level. Four pieces the brief asks for were already here and
already right, and the useful result of the audit was working out which two
thirds of the work did not need doing.

`app/marketdata/` (L08) is the canonical representation — `Bar`, `Series`,
`Availability`, eight timeframes — and its validator counts invalid OHLC,
duplicates, out-of-order bars, gaps and future stamps while **flagging and
never repairing**, which is the brief's own rule stated as a design.
`market_bars` is the raw layer, keyed so ingestion is idempotent and CHECKed at
the database so a bar that is not a candle cannot be stored.
`app/strategies/indicators.py` is the one indicator engine, with declared units
and warm-ups, calling the toolkit rather than reimplementing it. And
`build_signal_vector`'s prefix walk is the causality discipline this level
extends rather than invents.

Six things were genuinely missing: a feature engine, a label engine, dataset
identity, leakage detection, chronological splitting, and OHLCV resampling.

## The rule that shapes every feature

**Every feature is dimensionless, and no raw price level is in the catalogue.**

This is the repository's own hardest-won lesson applied to machine learning.
The metals run produced +4,236 pooled out-of-sample points across symbols whose
median H1 ATR spans 59x — an arithmetic error, not a finding — and
`rule_search.py` now raises a data gap above 5x. A model trained on `sma_20`
learns the price of the instrument.

So a moving average appears only as `sma_distance_20`, volatility only as
`atr_pct_14`, and the raw ATR — the unit every bracket in this project is
quoted in, and therefore the most natural-looking feature there could be — is
deliberately not offered at all.

## Causality, proved rather than asserted

`no_future_influence` is section 56 run literally: compute over a prefix,
append the rest, recompute, and require **exact** equality on the overlap. Not
a tolerance — a causal calculation over identical input produces identical
output, and calling a 1e-15 difference noise is how a real leak survives its
own test.

The test that matters more is the negative control: a deliberately
forward-reading feature is fed to the same check and must FAIL. A check that
has only ever passed proves that it runs, not that it discriminates. That is
the permutation-null argument from `rule_search.py` applied to a test instead
of a strategy, and the scaler check got the same treatment.

## The normalisation mistake has no spelling

`Scaler.fit()` takes rows **and a split**, and reads `split.train`. There is no
function that takes a whole dataset and returns a scaler, so the leak cannot be
written. A caller who genuinely wants one has to construct a split covering
everything — which is then visible in `fitted_rows` and caught by the check,
which re-fits rather than trusting the claim.

Fitting on the whole dataset is worth naming precisely because it is invisible:
no error, no warning, every metric simply better than it should be.

## A dataset is a recipe, not stored rows

No table holds a training row. If the same bars, feature version, label version
and configuration reproduce the same dataset — which a test checks row for row
— then storing the rows is optional, and storing them would be a second copy of
`market_bars` that can drift from it. That is the argument `market_bars` itself
makes for keeping `provider` in its identity.

The fingerprint covers the configuration **and a digest of the bars**. Without
the second half it would say "same recipe" and be read as "same dataset".

## Two refusals inside the labels

A bracket whose stop and target both sit inside one bar's range is
`AMBIGUOUS`. Bar data records that two prices traded and not which came first,
and the project has a live example of what guessing costs: position
10200315596, an NZDUSD sell whose M1 bar spanned 282 points, whose bracket was
computed from the top of that band, and whose every branch was therefore a
loss.

And `LabelConfig` has no zero-cost default. A WIN computed without the spread
is a label for a market nobody trades in, and cost drag is the only effect this
repository has measured to significance.

## What was NOT done

**Only one dataset has been built from real data** -- `eurusd-h1` v1, READY,
638 rows over January 2026, from one instrument. Every test runs on a seeded
synthetic series, because `market_bars` covers one instrument here — MT5 is not connected
and no ingestion has run. "The pipeline is correct" and "the datasets it
produces from this broker's history are sound" are different claims, and only
the first is supported.

**One symbol and one timeframe per dataset.** Multi-symbol sets are built by
combining single-symbol ones so the instrument stays on the row; the combining
step is not written. Cross-asset features are not built either — they need a
stated alignment rule, and getting it wrong means silently using a later bar
from the other instrument.

**The 252 completed trades are not yet a label source.** Trade-outcome labels
need a join from `trades` back to the bar that opened them, and the imported
ledger carries no bar reference.

**Incremental updates are not built.** A build reads its window and recomputes
it. At these sizes that costs seconds; the honest answer for larger histories
is the stated `MAX_BARS` bound rather than a claim of streaming that is not
implemented.

## Test counts

| Suite | Before | After |
|---|---|---|
| Backend | 1187 passed, 15 failed, 3 skipped | **1269 passed, 15 failed, 3 skipped** |
| Frontend | 84 | **87** |
| Research toolkit | 34 | 34, untouched |

The 15 are the same 15 in both columns: `redis.exceptions`, because Docker is
not running here. Lint, format and types are clean on both stacks.

Run in two chunks. One full run stalled at ~96% with flat CPU while a frontend
production build ran beside it; split, the halves ran clean. `test_webhooks.py`
takes five minutes on its own because its rate-limiting test retries against a
Redis that is not there — which is why every run in this project looks like it
hangs at 95%.

## Next

**Level 24 — AI models.** There is now a versioned, leakage-checked dataset to
fit against and a walk-forward structure to validate on. Read L26's row first:
this repository's measured position is that no rule it has tested clears its own
permutation null out of sample, across five universes and several hundred
candidates. A model has to clear the same gate, and L23 was built so that one
which appears to can be checked rather than believed.


# ============================================================ LEVEL 24

# LEVEL 24 — AI MODELS (2026-09-04)

Full write-up in `LEVEL_24_AI_MODELS.md`. The short version.

## What the audit found

**Nothing.** No AI model exists anywhere in this repository — no `sklearn`,
`torch`, `xgboost`, notebook, or serialised artifact. A search returns three
hits, all in tests asserting that a package does *not* import them.

So the brief's preservation rules had nothing to preserve, and nothing was
deleted, retrained or replaced. Saying that plainly is more useful than
dressing it up as a migration.

## The defect the audit found, which is not an AI defect

**The backend Docker image cannot run the strategy engine, the backtester, the
replay engine or L23's feature engine.**

It installs only `backend/requirements.txt` — which never declared numpy,
though four modules have imported it since L12 — and copies only `app`,
`alembic`, `alembic.ini` and `pyproject.toml`, so `tools/` is absent even
though `app/strategies/indicators.py` imports `rule_backtest` and `rule_search`
from it. Nothing catches this because the tests run outside Docker and the
health check touches none of it.

numpy is now declared, which is unambiguously correct either way. The `tools/`
half is a build-context change and belongs to L41; it is recorded there, in
`PROJECT_AUDIT.md` §21.2 and in `PROJECT_STATE.json`. It also shaped this
level: `app/ai/` uses only the standard library, so the AI layer does not
deepen a gap it did not create.

## No scikit-learn, and why

Four reasons compound. The backend does not even declare numpy. The three
models needed are a few dozen lines of standard library each. A logistic
model's coefficients ARE the feature importance the brief asks for — the one
model class where "this feature influenced the answer" is provably true rather
than a surrogate. And determinism is easier to guarantee without a framework's
defaults.

When L25 needs gradient boosting it should be added, with a reason and after
the packaging gap closes. That is a different decision from taking it now
because it is conventional.

## One shape, and a gate that refuses

`Prediction` is the same type for all three families, and **a refusal cannot
carry a value** — enforced in `__post_init__` and again by a database CHECK.
Six statuses, each a refusal with a reason: no fitted model, wrong feature
version, a missing feature named, stale features, insufficient data.

The gate lives in `BaseModel` in a **fixed order**, so a fourth family added
later cannot forget one. The order matters: a caller with the wrong feature
version usually also has missing features, and reporting the second would send
them looking for data rather than for a version.

## The three models

**Regime** — five classes plus UNKNOWN, from four boundaries fitted as
quantiles of the *training* segment. A 0.3% ATR is high volatility in EURUSD
and quiet in BTC, so any constant would encode one instrument's habits as a
universal fact: the same unit lesson L23 applies to features, one level up.
Volatility wins at the top of its range, because a market moving violently in
one direction is more usefully described as violent than as trending.

**Trade probability** — P(a *named* label), with the definition attached to
every prediction, and `calibrated` false until something measured it. An
uncalibrated probability is a ranking, not a frequency.

**Anomaly** — median and MAD rather than mean and standard deviation, because
an outlier moves a mean far more than a median and a detector fitted with means
is partly fitted to the events it exists to find. Its own output says the score
is not a probability and not a severity: a rare bar is not a bad bar, and the
project's largest single live loss came from a real 282-point M1 bar.

## The failure policy has two values and no third

AI_REQUIRED plus no answer means **no trade** — a model that could not answer
is not a model that agreed. AI_OPTIONAL plus no answer means the signal
proceeds to the risk engine, which is unchanged and still authoritative. The
policy is applied in exactly one place, and a feature build that *raises* goes
through it too rather than becoming an accept by omission.

## Three defects the tests found in this level's own code

**A refused prediction was stored as the JSON literal `null`, not SQL NULL.**
SQLAlchemy serialises Python `None` that way by default, so the CHECK that
exists to catch a refusal carrying a value failed on a refusal. Fixed with a
`NullableJSONType` for that column — the semantics in the schema rather than
in a call-site discipline.

**The prediction id did not cover its input.** Two different feature vectors at
one timestamp produced the same id and collapsed into one row; a test recorded
three predictions and read back one. Every distribution over that table would
have been wrong in a way nothing reported.

**A test grepped source text for a forbidden import and failed on a docstring
explaining the rule.** A text search cannot tell an explanation from a
violation. Rewritten to parse imports.

## Test counts

| Suite | Before | After |
|---|---|---|
| Backend | 1269 passed, 15 failed, 3 skipped | **1335 passed, 15 failed, 3 skipped** |
| Frontend | 87 | **91** |
| Research toolkit | 34 | 34, untouched |

The 15 are the same 15 in both columns. Lint, format and types clean on both
stacks.

## What was NOT done

**No model is loaded, and none is fitted on real data.** The registry is empty
at startup by design, and every model in the tests is fitted on a seeded
synthetic series because `market_bars` covers one instrument here.

**Nothing consults a model yet.** `ModelBackedFilter` exists and is tested; the
paper engine's seat still takes whatever filter it is handed, which is nothing.
Wiring one in means choosing a policy and a threshold per strategy — L27.

**The probability model is uncalibrated**, and says so. Measuring it needs a
validation run, which is L25/L26.

## Next

**Level 25 — AI training.** There is now a versioned dataset, a deterministic
fit and a prediction that says what it is the probability of. What there is not
is a hypothesis: this repository's own searches have yet to find a rule that
clears its permutation null out of sample, and a model faces the same gate.


# ============================================================ LEVEL 25

# LEVEL 25 — AI TRAINING ENGINE (2026-09-04)

Full write-up in `AI_TRAINING_ARCHITECTURE.md`, which is the filename §43 asks
for. The short version.

## What the audit found

**No training code of any kind** — no script, notebook, Celery, RQ, Optuna,
checkpointing or hyperparameter search anywhere in the repository. And
`training_runs`, created with the L05 schema and **never used**: imported only
by `app/models/__init__.py`, with a NOT NULL `model_version_id` that made a
queued job impossible to record, because a queued job has no candidate yet.

What did exist and is reused: the background-task-plus-semaphore pattern the
backtester and replay have used since L14, L23's dataset and scaler, L24's model
classes, L29's calibration statistics, and L07's realtime hub.

## No new queue

§4 says reuse the existing job system if one exists. One does. A fourth way of
running a background job in one codebase is three too many, and Celery or RQ
would add a broker, a worker process and a deployment surface to run fits that
complete in under a second.

## The dataset lock is a check, not a rule

A version string records what the data was *called*. L23's fingerprint covers
the dataset configuration **and a digest of the bars** — so the job stores it
before fitting, re-derives it afterwards, and **fails** if it moved rather than
recording a candidate against provenance that is a guess. A candidate with
guessed provenance is worse than none: it looks exactly like a reproducible one.

## No hyperparameter search, and that is the level's sharpest decision

Not scope. `reports/bracket_sweep.json` records a 36-cell sweep in which the
**random** rule scored an in-sample t of 1.76 against the best real candidate's
0.83, and `reports/rule_search.json` records 41 candidates whose best
out-of-sample result went negative.

A search is a machine for producing winners that do not survive. Building one
before L26's correction machinery exists would build precisely the trap those
numbers describe — and it would be the most convincing-looking thing in the
repository.

## A baseline before every candidate

The majority-class prior of the *training* segment, not a coin flip: a coin flip
is the weaker baseline on an imbalanced label and using it would flatter every
candidate. `compare()` calls a candidate better only when it beats the baseline
on log loss **and** accuracy **and** clears the majority share, and its own
output says it is not the L26 gate — no permutation null, no correction for the
number of candidates tried, no walk-forward.

Accuracy is never reported without `majority_share` beside it. A labelled set
that is 70% WIN gives 70% accuracy to a model that always says WIN.

## A successful run ends at `validation_pending`

There is deliberately no `completed`: §25's distinction is that "candidate model
successfully trained" is not "model approved for trading", and a status called
`completed` would be read as the second by everyone who did not read the
docstring. `paused`, `validation_passed` and `validation_failed` are absent
because nothing can reach them — the same rule L22 used on bot states and L23 on
dataset states.

## Two defects its own tests found

**The fit starved the event loop.** A synchronous CPU-bound loop inside a
background `asyncio` task blocks every other request in the process for as long
as it runs — including the health check. §4 says training must not run inside an
HTTP request; running it on the same *thread* as every HTTP request is the same
problem wearing a different hat. It runs on a worker thread now.

**The cancel flag was looked up by job id.** The task's done-callback pops that
entry, so a cancelled job's worker thread looked itself up, found nothing, read
"not cancelled" and ran the fit to completion — the row saying one thing while
the machine did another, which is exactly the failure L22's heartbeat supervisor
exists to catch one level up. The flag is now an object the job owns.

Both surfaced as suite flakiness rather than from a test aimed at them, which is
an argument for running the whole file rather than the one test that changed.

## Test counts

| Suite | Before | After |
|---|---|---|
| Backend | 1335 passed, 15 failed, 3 skipped | **1398 passed, 15 failed, 3 skipped** |
| Frontend | 91 | **95** |
| Research toolkit | 34 | 34, untouched |

The 15 are the same 15 in both columns. Lint, format and types clean on both
stacks; the frontend production build succeeds.

## What was NOT done

**No model has been trained on real data.** `market_bars` covers one instrument here — MT5
is not connected and no ingestion has run — so every test trains on a seeded
synthetic series. §48 is explicit that a blocker should be reported rather than
papered over: the infrastructure is verified, and no claim is made about any
model's usefulness.

**No checkpointing.** These fits complete in under a second; a checkpoint would
be state to maintain for a resume nobody needs. `TrainingStage` already provides
the boundaries to write one at when a slower family arrives.

**No scheduled retraining.** No scheduler exists to integrate with, and §32
warns against enabling aggressive retraining.

## Next

**Level 26 — AI validation**, which is what stands between a candidate and any
use of it.

---

# Level 26 — AI validation engine (2026-09-04)

The question, deliberately narrow: **not "is this model good", not "should we
deploy it", but "does the evidence support a claim about this candidate at all,
and if so, what claim".**

## What was already here

More than at any previous level. The methodology predates the platform, and
`MIGRATION_STATUS.md` had recorded L26 as "COMPLETE (methodology)" since the
first audit. `app/validation/` **calls** all of it and reimplements none:

- `tools/rule_search.py` — the permutation null, era blocks, walk-forward,
  date and symbol clustering.
- `tools/rule_backtest.simulate` — every economic figure, because it produced
  every measured figure in `CLAUDE.md` and a second simulator would make the
  comparison meaningless.
- `app/monitoring/stats.py` — Brier, reliability bins, expected calibration
  error.
- `app/datasets/` — the six leakage checks, whose verdict is READ rather than
  recomputed.
- `app/training/metrics.py` — the training record, whose own `compare()`
  docstring already said "this is NOT the L26 gate".
- The `BacktestService` background-job shape, for the fourth time and
  deliberately not a fifth pattern.

## What was added

`app/validation/` (config, statistics, economics, checks, report, service),
`app/ai/loader.py`, `build_validation_loader`, the `validation_runs` table with
migration 0018, six routes under `/v1/ai/validation`, a Validation panel on the
AI Lab, and 93 tests.

`app/ai/loader.py` closes a gap in L24's own work: `artifact_of()` wrote a
fitted model as JSON and **nothing had ever read it back**, because training
holds its model in memory and registers it in the same call.

## The decisions

**There is no score.** `verdict_from()` is precedence — BLOCKED > FAIL >
CONDITIONAL > PASS — and a test asserts the payload carries no key that could
become one. A weighted composite can always be tuned until it hides the check
that mattered.

**BLOCKED is not FAIL.** One says the candidate is not good enough; the other
says we could not tell. A missing check is never a passing one.

**A PASS promotes nothing.** `model_versions.status` is never written; migration
0018 declines to add a `validated` status nothing could set; the run status is
`completed`, never `passed`; the router has no PATCH, PUT or DELETE.

**Calibration fails as a WARNING**, because a miscalibrated model can still rank
correctly and ranking is what the AI seat uses it for. The warning constrains
how the number may be read, not whether the model is usable.

**The in-sample figure is measured here.** L25 stores an early-stopping figure
from the VALIDATION segment, which is not in-sample. Reading it would have
compared two quantities that are not the same thing, and the report would have
looked identical.

## What the first run concluded

On 1,200 synthetic H1 bars, a real training run produced a candidate and
validation returned **FAIL on significance**: twelve checks passed, the profit
factor was 3.46, and the permutation null put p at 0.0784 with the best shuffle
scoring 0.6093 against the model's 0.5641.

That is the correct answer — the data is noise — and it is the answer a rubber
stamp would have got wrong. An engine that passed this would have looked
identical on every other check.

## Tests

| Suite | Before | After |
|---|---|---|
| Backend | 1417 passed, 0 failed, 0 skipped | **1502 passed, 0 failed, 0 skipped** |
| Frontend | 95 | **102** |
| Research toolkit | 34 | 34, untouched |

The three PostgreSQL migration tests ran against a real database, including the
round-trip downgrade of 0018. The development database was migrated 0017 → 0018
in place.

## What was NOT done

**No model has been validated on real data.** `market_bars` covers one instrument here. The
engine is verified end to end against seeded synthetic series; no claim is made
about any model's usefulness.

**No scheduled re-validation and no drift detection.** That is L29's monitoring,
and building a second one here would be the duplication §40 warns against.

**No comparison of two candidates against each other.** Ranking candidates is a
selection procedure and needs its own correction; `compare()` compares a
candidate to its own baseline, which is a different question.

**The regime and anomaly families are not fully validated.** They have no
ranking to score, so the probability-dependent checks BLOCK on an empty list
rather than substituting 0.5.

## Next

**Level 27 — AI strategy integration.**

---

# Level 27 — AI strategy integration (2026-09-04)

The seat `app/execution/ai.py` has held since L16, and which had taken nothing,
is now filled.

> **AI enhances a strategy decision. It never becomes the final authority over
> trading risk.**

## What the audit found

The pipeline was already right. The AI stage has sat between the strategy and
the risk engine in both pipelines since L16 and L20; `Outcome.ai_rejected` has
been in `NO_ORDER` since L20; L23 supplied the feature engine, L24 the
prediction contract and the registry, L26 the verdict that makes a model
eligible. Nothing had to move.

So L27 added four modes, an eligibility boundary and a journal — not a pipeline.

## The four modes

**AI_DISABLED** runs no inference at all, and in a paper bot the engine's seat
is `None` rather than a filter that accepts everything. A filter would journal
rows and appear in the counters, so a reader could not tell a disabled
deployment from one whose model agrees with everything.

**AI_ADVISORY** records and returns NEUTRAL, never ACCEPT. Somebody will later
count how often the layer agreed, and an advisory reading is not agreement.

**AI_FILTER** may reject.

**AI_SCORING** combines by a named formula, defaulting to `min(strategy, ai)` —
the only one of the three that cannot let a confident AI rescue a weak strategy
signal. Every formula is asserted monotone in both inputs, which is what makes a
threshold on the result mean anything.

## What it cannot do, and how that is enforced

- **Approve.** `AiVerdict` has four fields and none names a limit, an approval,
  a quantity or an account. An AI wanting to overturn a risk veto has no
  vocabulary for it.
- **Size.** `app/ai/` contains no `SizingRequest`, `lot_for_risk` or
  `calculate(`. A 99% probability produces the same verdict shape as a 51% one.
- **Execute.** Every module in `app/ai/` is parsed with `ast`; an import of
  execution, risk, sizing, OMS, brokers, orders, positions, paper or bots fails
  the suite. `LIVE_TRADING` appears nowhere.
- **Originate.** In a backtest a bar the strategy left flat is never offered to
  the layer, so there is no branch that could turn a 0 into a ±1.

## No future information

The AI layer fetches nothing. The paper engine hands it the same `Candles`
object the strategy read. A window reaching past the signal is **refused, not
trimmed** — a trim turns a caller's bug into a silently different evaluation
that looks fine. The backtest filter runs bar by bar on `bars[:i+1]`, and a test
records the window length at each consulted bar.

## Two defects its own work found

**Migration 0019 passed a pre-prefixed unique-constraint name** — the identical
defect migrations 0014 and 0015 had with `drop_constraint`, one constraint type
later. Caught by the drift test against real PostgreSQL. The rule generalises:
pass the BARE name, for every constraint kind.

**An out-of-range probability was labelled `PIPELINE_ERROR`**, because L24's
`Prediction` refuses one in its own constructor and raises before the
integration service's own check runs. The check is right and the label was
wrong; now caught by type and reported as `INVALID_OUTPUT` naming the model and
the field.

## Tests

| Suite | Before | After |
|---|---|---|
| Backend | 1502 passed, 0 failed, 0 skipped | **1574 passed, 0 failed, 0 skipped** |
| Frontend | 102 | **111** |
| Research toolkit | 34 | 34, untouched |

Migration 0019 round-trips against real PostgreSQL, including the downgrade.

## What was NOT done

**No model has been wired to a strategy on real data.** `market_bars` covers one instrument
here. No strategy on this deployment is configured for an AI mode.

**No AI-assisted sizing.** §21's default is kept and there is no path to
anything else.

**No autonomous strategy discovery.** The Pine parser does not exist yet; the
boundary it will have to respect is the one this level built.

**No drift monitoring.** L29's. What L27 provides is the data it will read.

**Live trading is unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`. Even
with strategy PASS, AI ACCEPT and risk PASS, the platform stays in paper mode.

## Read this before comparing an AI run to its baseline

Frequency is the one lever this project has shown has a sign, and it points
down: tight brackets trade often and pay the spread every time. **A filter that
removes trades will improve total P&L on many of this repository's own datasets
for that reason alone**, and reading it as an AI edge would repeat the
`time_120` and H4-bracket errors exactly. Read expectancy per trade beside the
total, and run the era blocks.

## Next

**Level 28 — model registry.**

---

# Level 28 — Model registry and model lifecycle (2026-09-04)

The authoritative answer to *"which model versions exist, what happened to them,
and which are eligible for use?"*

## A level of consolidation

Almost everything was already here. `model_versions` since L05, provenance from
L24, candidates from L25, verdicts from L26, and L27's eligibility boundary
written specifically to be switched over. L28 added the lifecycle, the artifact
check, deployments and resolution — and created no second registry, loader,
version table, prediction format or job queue.

## Eight states, four declined in code

`app/ai/lifecycle.DECLINED` records why each absent state is absent, in the
module rather than in a document: *"we thought about it and decided against"* and
*"we forgot"* look identical in a schema. The sharpest is `VALIDATING` —
**nothing could set it**, since L26 deliberately writes no model status and
`validation_runs` already records a running run.

**There is exactly one edge into `promoted`, and it starts at `paper`.** §12 as
a property of the transition table rather than a check in a function, so it
cannot be forgotten by a new code path.

**`validated` does not serve inference.** Validation is about the evidence;
registration is about the artefact. Two checks, failing independently.

## Two defects, both worth the level

**NULLs are distinct in a unique index.** The one-active-per-scope constraint was
over the nullable scope columns, so any number of rows could share the
*unrestricted* scope — the most common one. Absent exactly where it mattered
most, present everywhere it was easy to test. Found by the test that tried to
violate it; fixed with a derived NOT NULL `scope_key`.

**Three levels of realtime events had never fired.** `Hub.publish` takes one
argument and `TrainingService._publish` passed two, so every training and
validation event since L25 raised a `TypeError` the surrounding `except`
swallowed. Nothing noticed, because a subscriber that never fires is
indistinguishable from a market that never moved — which is the sentence
`Hub.publish` itself uses to justify refusing an uncatalogued type.

## Tests

| Suite | Before | After |
|---|---|---|
| Backend | 1574 passed, 0 failed, 0 skipped | **1646 passed, 0 failed, 0 skipped** |
| Frontend | 111 | **122** |
| Research toolkit | 34 | 34, untouched |

Migration 0020 round-trips against real PostgreSQL, including the downgrade.

## What was NOT done

**No model has been through the lifecycle on real data.** `market_bars` covers one instrument
here, and on synthetic noise L26 legitimately returns FAIL — which the registry
then refuses. That refusal *is* the validation gate working.

**No object storage.** These artifacts are kilobytes of JSON.

**No automatic promotion of any kind.** Every transition is an explicit act by a
named actor with a recorded reason.

**No multi-model deployment for one scope.** §14 permits it only where the
architecture supports it; this one deliberately does not.

**Live trading is unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`, all
ten live gates false. `promoted` means *the version this scope resolves to*, and
nothing more.

## Next

**Level 29 — model monitoring.**

---

# Level 29 — AI model monitoring and drift detection (2026-09-04)

The question: **is the currently used model behaving consistently with the
conditions it was validated under?** And the rule: monitoring may never act on
the answer.

## The most prior art of any level — and a sentence that had gone stale

`app/monitoring/` predates L24: the statistical engine, eight checks, the
findings vocabulary and the WARNING→PAUSE→REVIEW→RETRAIN→VALIDATE ladder were
all here and tested.

**And it had never run on a recorded inference.** `MonitoringInputs` was a
dataclass a caller filled by hand, and nothing filled it. `monitor.py` said why
in its own docstring — *"There is no model in this platform yet"* — which was
true when written, and which L27 and L28 made false.

So the missing piece was not statistics. It was `collect.py`: the module that
reads `ai_decisions`, `model_deployments` and `trades` and computes nothing.

## Four checks added, all about the model's service

`latency` (the p95, because a mean hides the tail), `availability` (which
excludes `AI_DISABLED` from the denominator, because counting it would make
turning the AI off look like perfect uptime), `confidence_drift` (a flag for
investigation, never a conclusion), and `possible_concept_drift` — inferred from
one pattern and never claimed from input drift alone.

## The decisions

**`INSUFFICIENT_DATA` outranks `HEALTHY`**, in the precedence and in a CHECK
constraint. L26's BLOCKED-is-not-FAIL, one level along.

**`OFFLINE` outranks everything.** A model that is not serving cannot be healthy
or degraded.

**`DEGRADED` is reserved for the model.** A feature distribution moving is the
market moving; §42's caution made into a state an operator reacts to
differently.

**A severity change is a new alert.** The fingerprint includes the severity, so
WARNING → CRITICAL cannot be suppressed by the cooldown that exists to stop a
monitor repeating itself into being muted.

**An unresolved decision is not a loss.** Scoring a risk veto as a wrong
prediction would make a conservative risk configuration look like a broken
model — which is backwards, and would push somebody to loosen the risk engine to
make the monitoring look better.

## The defect it found

A CHECK constraint name of 67 characters. **PostgreSQL truncates identifiers at
63** and SQLAlchemy appends a hash when it does, so the model's resolved name
and the migration's literal name diverged. Caught by the drift test — another
way the test environment is more permissive than production.

## Tests

| Suite | Before | After |
|---|---|---|
| Backend | 1646 passed, 0 failed, 0 skipped | **1700 passed, 0 failed, 0 skipped** |
| Frontend | 122 | **132** |
| Research toolkit | 34 | 34, untouched |

Migration 0021 round-trips against real PostgreSQL, including the downgrade.

## What was NOT done

**Nothing has run on real inference.** `market_bars` covers one instrument, no strategy is
configured for an AI mode, and no model is deployed. Every test runs against
seeded `ai_decisions` rows shaped exactly as L27 writes them. **No monitoring
figure in this repository describes a real model.**

**No charts.** §37 wants real data and §52 forbids fabricated values for visual
appearance. With nothing measured, a sparkline would be drawn from nothing.

**No automated retraining trigger**, not even a disabled one. The ladder
recommends `retrain` and `validate` to a person, and there is no code path from
a finding to a training job.

**No correlation engine**, which §27 says not to fake.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`, all ten
gates false.

---

# Level 30 — Portfolio management and exposure (2026-09-04)

The question: **what is held, what is it worth, and how sure are we of the
answer?** And the rule from §2: this is not the risk engine. It reports state;
`RiskEngine` decides.

## What the audit found

Almost everything. `portfolio_snapshots` since L05, `positions` since L21,
`trades` since L19, the contract specs since L11, `day_start` since L17,
`value_per_price_unit` since L18, `PaperPortfolio` since L16 and
`brokers.base.Account` since L10. Six systems each owning a piece, and nothing
that assembled them.

So L30 built the assembler and **shipped no migration**: the snapshot table
already had balance, equity, margin, free_margin, open_positions, exposure and
drawdown_pct. §34 says not to duplicate what exists, and here that meant writing
no schema at all.

## The decisions

**Gross and net, on every aggregate.** Long 50,000 and short 30,000 is 80,000
gross and +20,000 net. The case that settles it is the flat book: equal long and
short is `net = 0` with `gross > 0`, which a one-number display renders as no
exposure at all.

**Notional refuses rather than guesses.** `notional_of` requires L11's measured
contract size and returns an uncomputable refusal without one. This is the
metals-points error made unrepresentable — `CLAUDE.md` records a +4,236-point
pooled result that was arithmetic rather than a finding, because median H1 ATR
runs 160 points in silver against 9,386 in palladium. The refused positions are
COUNTED, not dropped: a total that silently excluded three would be wrong in a
way nobody could see.

**Currency comes from metadata, never the ticker**, and one symbol missing a
base or quote withholds the whole breakdown.

**Unrealized is all-or-nothing.** A partial total is a number a reader treats as
the whole, and there is no visual difference between the two.

**An unstopped position has no risk, not zero risk.** Zero would say it cannot
lose — the exact inversion, and it would make a total look safer the more
unstopped positions the account held.

**The peak only ever rises**, and it is read from the whole recorded history. A
windowed peak falls as the window passes an old high, and the drawdown shrinks
without the account recovering.

**Health is a precedence, never a score.** ERROR > RECONCILIATION_REQUIRED >
STALE > WARNING > HEALTHY, with reasons beside it. A disagreement about what is
held outranks an old figure for something we agree on.

**Reconciliation compares and never repairs.** L21's reconciler settles; §63
forbids this engine from modifying a position. Two systems that both repair would
race.

**What the risk engine reads and this engine does not own is DECLARED.**
`NOT_SUPPLIED` names nine `PortfolioState` fields and their owner. L28's
`DECLINED` pattern: an absence looks identical whether it was reasoned about or
forgotten.

**A stale portfolio makes trading more conservative.** Every value in the handoff
may be `None`, and L17 reads a needed `None` as a veto. Worth writing down
because the intuitive fear runs the other way.

## The defect

`day_start` (L17) takes an AWARE UTC datetime; this engine works in NAIVE UTC,
because that is what the database stores. Passing one straight through would have
had `.astimezone(UTC)` read it as LOCAL time — so on this UTC+5:30 machine the
trading day would have started at 18:30 the previous evening, and the identical
code would have been correct on a UTC machine.

That is the bug `CLAUDE.md` documents at length, arrived at from a new direction:
**reusing the right function was not sufficient, because the tz-awareness
boundary sits between the two callers.** Caught before it shipped by writing the
day-boundary test first. A parsed test now asserts that no module in
`app/portfolio/` calls `.replace(hour=…)` at all.

## Tests

90 portfolio tests and 8 API tests where there were none, plus 7 frontend.
Backend **1781 passed, 0 failed, 0 skipped**; frontend **139**.

The L29 count in `TESTING.md` said 1700. That was an estimate written before the
run finished; the run reported **1688**. Corrected rather than quietly adjusted —
a test count nobody checks is exactly the kind of figure this project treats as
generated.

## What is NOT claimed

**No broker adapter is connected here**, so the live path is exercised against a
fake. No position in this repository has ever been marked from a real quote:
`market_bars` covers one instrument.

**No correlation engine.** §27 says not to fake one, and it needs a common window
of market data across held instruments. The currency breakdown is the honest
proxy — several USD-long positions showing as one large USD figure is the
shared-dollar-move clustering the FX searches already document.

**No currency conversion.** No FX rate source is wired, and an invented rate is
the swap-unit error again: `swap.py` reports a gap rather than a number when it
cannot convert, and this follows it.

**No equity chart.** §38 in spirit and §59 in letter: a sparkline drawn from an
empty `market_bars` is a fabricated figure with a nicer shape.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`. Nothing
in `app/portfolio/` references either name, and none of it imports
`app.core.settings`.

---

# Level 31 — Trade journal and trade lifecycle history (2026-09-04)

The question: **what happened, why, and what did the system know at the time?**
And the rule from §55: it records, and it may never act.

## What the audit found

The record already existed, with 252 rows in it, and nine tables between them
already held every fact the brief asks a journal to answer. So L31 built no
second history and **added no table** — `trades` gained 13 nullable columns.

Three things were genuinely missing.

**The live path wrote no trade at all.** `PositionManager._book` closes a
position and books `realized_pnl` onto the row, and nothing then created a
`trades` row. Only the paper service and the JSONL importer ever wrote one — so a
demo or live position closing left the journal untouched, and nothing noticed,
because an empty journal looks exactly like an account that has not traded.

**The paper path wrote one with no attribution.** No position, no strategy
version, no account, no exit reason. It could not be linked back to anything.

**Nothing assembled the context.** Every fact §8 to §12 ask for was recorded.
What did not exist was the module that read them together.

## The decisions

**One journal row per POSITION EPISODE**, enforced by a partial unique index. A
position filled in two parts and closed in three is one trade, not five, and
counting execution rows as trades is the error §41 names.

**A sweep, not a hook.** Four places can close a position, and a hook missed at
one produces a trade that silently never exists. `record_pending` asks the
database which finished episodes have no row, and cannot miss.

**The timeline is derived and stored nowhere.** A `trade_events` table would be a
second copy of nine tables' worth of already-timestamped events; when it
disagreed there would be no way to tell which was right. Derived, it also gets
§27's idempotency for free.

**Realized P&L is summed from the closes, never recomputed from the weighted
average.** They agree only when the entry was never weighted, and booking both is
the double-count §14 forbids.

**Context is read from the row written then.** The risk snapshot, the AI decision
at an exact model version and `orders.sizing` are referenced rather than
re-derived — §10 forbids recomputing historical context with today's values.

**An absent block says which kind of absent.** "No AI decision is linked" for an
AI_DISABLED strategy is the expected shape, and it says so out loud, because a
blank reads as a bypass.

**Nothing is finalised that the venue has not confirmed.** `unknown` stays
`unknown`; a mismatch becomes `reconciliation_required` and changes only the
status and the quality block.

**Data quality is detected and flagged, never corrected.** A negative duration is
reported rather than reordered: a quietly repaired record looks clean and is
wrong.

## The defects

**`op.drop_constraint("ck_trades_status", …)` produced
`ck_trades_ck_trades_status`.** Fourth occurrence in this repository and the
first on a DROP rather than a CREATE. Only the round-trip downgrade against a
real PostgreSQL reaches that statement — SQLite never executes it, so the test
environment is again more permissive than production.

**`/trades/{trade_id}` shadowed `/trades/statistics`.** Starlette matches in
registration order, so the parameterised route captured `statistics` as a trade
id. Caught by the test that asserts both resolve, not by reading the code.

## Tests

62 backend tests and 9 frontend tests where there were none. Migration 0022 run
against real PostgreSQL, including the round-trip downgrade.

## What is NOT claimed

**No broker adapter is connected here**, so every live-path test runs against a
fake, and no trade in this repository has been journalled from a real venue.

**The 252 imported rows carry none of the new attribution** — which is correct,
and is why every added column is nullable.

**No MAE/MFE.** It needs intratrade price data and `market_bars` covers one instrument; §20
says not to approximate it.

**No entry slippage in points.** The requested and actual entry are both
recorded, so the gap is visible; converting it to points needs the symbol's point
size, and a figure from an assumed one is the metals-points error again.

**No currency conversion.** No FX rate source is wired.

**No Sharpe, Sortino or significance.** L32's, deliberately — and `CLAUDE.md`
records why: a live t of 9.33 on a 6:1 adverse bracket was arithmetic rather than
evidence.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`. Nothing
in `app/journal/` references either name.

---

# Level 32 — Analytics and performance analytics (2026-09-04)

The question: **how did it perform?** And the constraint from §2: audit before
building, and reuse a correct existing calculation rather than duplicating it.

## What the audit found

**The metric definitions existed three times.**

| Where | Unit |
|---|---|
| `backtest/runner.py` `compute_metrics` | points |
| `training/metrics.py` `economic` | label returns |
| `services/journal.py` `statistics` (L31) | account currency |

Win rate, profit factor and maximum drawdown, in three places, agreeing. That is
the dangerous case rather than the safe one: three copies stay agreeing only
until somebody changes one, and the copy nobody looked at is the one somebody
quotes.

So the level is an extraction. `app/analytics/metrics.py` holds the definitions
and both former copies call it — with **no output shape changed**, including
`runner.py`'s `NOT_AVAILABLE` sentinel, which the backtest API has served since
L14. 98 backtest and training tests pass unchanged, which is the evidence the
refactor moved no number.

It is the fifth extraction of this shape here, after `value_per_price_unit`
(L18), `AiVerdict` (L22), `AiPolicy` (L27) and the exit vocabulary (L31).

## The decisions

**The unit became a type.** `Series.concat` raises `UnitMismatch` rather than
pooling points with currency. `CLAUDE.md` has documented the metals-points error
for months and it still reappeared as a lot-size spread in the live record; a
comment does not stop it and a type does. Every summary is computed twice, in
currency and in R, each labelled, with a field saying which pools.

**A trade with no recorded planned risk is excluded from R, never assigned one.**
Deriving R from the realised loss makes every loser exactly −1R by construction —
a picture of the definition rather than a measurement.

**`INSUFFICIENT_DATA` is a value, not an absence.** A profit factor with no
losers is not infinity; that is exactly how this repository's 93%-win-rate live
regime looked decisive while being arithmetic. A recovery factor with no drawdown
is undefined, not impressive.

**Two equity curves.** §7 asks that deposits not be read as profit *if they
exist*. They do not — there is no cash-movement table — so the realized curve,
which cannot contain a deposit, is the performance figure, and the account curve
says a deposit and a profit look identical in it.

**No caching, measured rather than assumed.** 252 rows, sub-10ms summaries, and a
cache adds an invalidation path that could serve a stale equity figure as current
— which §27 itself warns against. `calculation_ms` is on every summary so the
decision is revisitable with evidence.

**Analytics contains no write verb at all.** Not just no order placement: no
`add`, `commit`, `delete`, `flush`, `drop_all`, `truncate` or `merge`. A
read-only module that acquired a write would become an owner of state, and the
whole separation depends on it never being one.

## Tests

91 analytics tests and 10 frontend tests where there were none. **No migration** —
every figure is aggregated at read time.

## What is NOT claimed

**Nothing has run on real data.** `market_bars` covers one instrument, no broker adapter is
connected, and the 252 trades in the journal are imported from
`data/track_record.jsonl`. Every metric is verified against seeded fixtures.

**No figure this level produces describes a real edge.** The project's measured
position — nothing clearing its permutation null out of sample across five
universes, and the strongest-looking result dissolving under era blocks, date
clustering and a walk-forward — is unchanged. What L32 adds is that a performance
claim can now be checked rather than believed, and that the check reports
`INSUFFICIENT_DATA` far more often than it reports a number.

**No MAE/MFE, no realtime push, no Excel or PDF export**, each for a stated
reason rather than as an oversight.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`.

---

# Level 33 — AI trade review and post-trade intelligence (2026-09-04)

The question: **why did this trade come out the way it did?** And the constraint
that shapes the answer: do not use information the decision did not have.

## What the audit found

**There is no LLM in this platform.** No `openai`, no `anthropic`, no API key
setting, no prompt template, no provider abstraction. `app/ai/` holds logistic,
regime and anomaly models written in stdlib; none of them talks to a language
model.

That is the finding L24 recorded about models — *"No AI model existed anywhere
before it"* — and it has the same consequence: build the **seat** rather than an
occupant. `ReviewProvider` is a one-method Protocol; the default implementation
is deterministic and is not a language model.

It also found `journal_entries.ai_review`, a column carrying a `# L33` comment
since L05. **It was deliberately not used**: that is a user's note row, and L31
already separated user notes from system-generated facts.

## The decision that shapes everything else

`DecisionContext` holds what was known at or before the entry. `OutcomeContext`
holds what happened after. **Four of the five assessors take the first and
nothing else**, and the type has no `exit_price`, `net_profit`, `r_multiple`,
`mae` or `mfe` on it at all.

§63 asks for a test proving the entry assessment gains no future information when
post-entry data changes. It passes because **the function is not given the
data** — not because it is careful. A convention gets forgotten; a signature does
not.

`exit_quality` is the deliberate exception: an exit is itself a decision, so it
receives the outcome and is rated against the levels planned *before* entry,
never against how far price eventually travelled.

## The other decisions

**A provider cannot return a rating.** `Narration` has a summary and three lists
and nowhere to put a price, a P&L or a model version — so §37's hallucinations
have no route in before the validator even looks.

**UNKNOWN is never POOR.** No strategy record is not a non-compliant trade; no
sizing record is not incorrect sizing. Every UNKNOWN names the field it needed.

**Confidence is derived** from data completeness and section coverage, with no
term for how sure the narrative sounded.

**Validation is two passes.** Every decimal in the narrative must appear in the
recorded facts; an invented price, symbol or model version fails the review
rather than being repaired.

**A provider failure touches nothing.** A test records the trade's status, P&L
and exit price before a deliberately broken provider runs, and asserts all three
are unchanged.

## The defect

The forbidden-name safety test caught `db.rollback()` and
`WorkerRegistry.register` — neither related to the model registry. Narrowed to
the registry's specific verbs plus an import check on `app.ai.registry_service`.

Third time this repository has hit the shape: L27 and L28 both had grep-based
safety tests fail on a docstring explaining the rule they enforced. **A safety
test that cries wolf stops being a safety test.**

## Tests

57 backend tests and 10 frontend tests where there were none. Migration 0023 run
against real PostgreSQL, including the round-trip downgrade.

**The full run surfaced seven failures and every one was real**, which is the
argument for running it rather than trusting the touched files:

  * Four in `test_realtime.py`. The FRONTEND keeps its own copy of the event
    catalogue, and ten types added across L30, L31 and L33 were never synced to
    it. That pair of tests exists precisely to catch this, and it did.
  * `test_every_promised_table_exists` -- 52 to 53 for `trade_reviews`. L30, L31
    and L32 added none, which is worth saying: the portfolio reads a table L05
    already had, the journal extended `trades` rather than shadowing it, and
    analytics aggregates at read time.
  * `test_unbuilt_groups_name_their_level[/v1/portfolio/summary]` -- the 501 stub
    is built now.
  * `test_realized_today_counts_only_trades_after_the_boundary` -- it built two
    trades on ONE position, which L31's `UNIQUE(position_id)` correctly forbids.
    The test was right when written; the shape it constructs became impossible
    when "one journal row per position episode" became a database guarantee.

An eighth came from the frontend suite: its own copy of the event-count
assertion, the mirror of the backend one.

## What is NOT claimed

**Nothing has run on a real review.** The 252 trades in the journal are imported
rows with no strategy, risk, sizing or AI attribution — so a review of one is
mostly `UNKNOWN`, and **that is the correct output rather than a gap**. The
end-to-end test constructs a fully attributed trade to prove the chain works when
the data exists.

**No LLM is wired**, and the reason is not that it would be hard. It needs a
vendor, a cost ceiling and a data-egress policy, and those are decisions somebody
should make deliberately rather than inherit from a level that needed a sentence
written.

**No MAE/MFE, no trade chart, no clustering, no scheduled worker** — each for a
stated reason: intratrade prices do not exist, `market_bars` covers one instrument, §30
permits clustering only where the project already supports it, and this
platform's convention since L20 is that workers are registered and not started.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`. Nothing
in `app/review/` references either name, and it cannot reach the model registry
to promote anything.

## Next

**Level 34 — notifications.**


---

# Level 34 — Notifications and alerting (2026-09-05)

The question: **how does anybody find out?** Thirty-three levels of recording,
measuring, refusing and reconciling, and none of it had ever reached a person
who was not looking at a screen.

## What the audit found

**Nine of the twelve pieces already existed.** This level is mostly wiring, and
the wiring was designed by earlier levels that left seats open on purpose:

* `NOTIFICATION_CREATED` has been in the event catalogue since L07, scoped to
  `user`, with `channels.py` already authorizing `user:{id}` to exactly that
  user and nothing producing it.
* `notifications` has been a table since migration 0002.
* `app/monitoring/alerts.py` had already worked out the deduplication argument
  at L29, including the part that matters most -- a severity change is a new
  alert, because suppressing it hides the transition an operator needs to see.
* `app/auth/reset.py` had a `ResetDelivery` Protocol whose only implementation
  refused, with a docstring saying delivery *"needs the notification engine
  (level 34)"*.

What did **not** exist: any Discord code, any email or SMTP code, any
notification service, any preference, any delivery record. Two placeholders name
Discord -- a line in `app/api/pending.py` and a planned page -- and neither is an
implementation. So nothing was preserved by pretending; the seats were filled.

## The decision that shapes everything else

**A second SUBSCRIBER, never a second bus.**

The obvious implementation is to let the realtime hub persist notifications: it
already reads the bus, it already has every event. It was refused. The hub's own
docstring says *the hub delivers; it never decides*, and its design property is
that it holds no session and cannot write a row -- which is why a bug in it can
drop a frame and cannot trade. Giving it a database session to save one Redis
subscription would have destroyed the property that makes it safe.

So `NotificationConsumer` opens its own subscription onto the same bus. One
Redis, two readers, different jobs. Section 65 says *do not create a second
event bus*; a second subscriber is not one, and the distinction is the whole
design.

## The second decision: persist before you deliver

Step 8 of ten is `PERSIST`, and steps 9 and 10 are preferences and queueing. The
ordering is section 13, and it has a consequence worth stating: **there is no
such thing as an in-app delivery failure.** The row is the delivery. If the bus
is down the frame is not sent and the notification is still there, and the
browser reads it on its next request. Reporting FAILED and retrying would be
retrying something that already succeeded.

That is also the answer to *"does a WebSocket reconnect lose alerts?"* It cannot.
The socket was never the record.

## What the templates refuse to do

**A figure that is not on the event does not appear in the message.** Not as a
zero, not as "unknown P&L". The line is left out.

This is the project's own top-level rule -- *Python computes, the model
interprets, the model never introduces a figure* -- applied to a notification. A
message saying *"closed with +$0.00"* because the payload carried no
`net_profit` is a false statement about a trade, and it is indistinguishable by
eye from a true one.

Three specific refusals came out of it:

* `ORDER_UNKNOWN` produces *"the broker's state for this order could not be
  established... no conclusion has been drawn"*, never *"order failed"*. The OMS
  has a separate `ORDER_FAILED` for the case where it knows, and conflating them
  would tell somebody their order was dead when it may be live at the venue.
* A `RISK_ALERT` does not say trading was blocked unless the engine's own
  payload said so. Only the RiskEngine can make a claim about enforcement.
* An event about trading that named no environment is stored and rendered as
  `[UNKNOWN]`, never `[PAPER]`. A live trade shown as paper is the mistake that
  costs money.

## What a preference is, and is not

A preference decides whether somebody is **told**. It decides nothing else.

`notification_preferences` is read in exactly one place in the whole
application, and a test asserts the list of files that even mention the model.
The RiskEngine's limits, the kill switch, order execution, position exits and
broker reconciliation do not consult it and cannot: `app/notifications/` imports
none of them, and a test parses every module in the package to keep that true.

And a user cannot switch off being told that a risk limit broke. ERROR and
CRITICAL always reach the in-app centre -- enforced three times over, in
`resolve()`, in the API (which **refuses** rather than clamping, because a
clamped setting is one you believe you made), and in a CHECK constraint that
makes the offending row unwritable.

## The awkward finding

`ModelMonitor._persist` has written to `notifications` since L29 with
`user_id = NULL`. Nothing ever read those rows: no route served them, and their
status stayed `pending` forever.

They were **kept**. Deleting them would destroy the only record those monitoring
runs left, and `user_id IS NULL` now has a documented meaning -- a platform
record with no recipient -- which cannot surface as somebody's notification
because every read is scoped to the signed-in user. The user-addressed version of
the same fact already flows through L34 from the `MODEL_ALERT_CREATED` event that
`app/monitoring/service.py` publishes.

What did change is the vocabulary: the column was about to hold L29's three
lowercase words and L34's five uppercase ones. `_persist` now imports the enum
rather than spelling the words out a second time.

**This is a known limitation and it is written down as one.** There are two
writers to that table and only one produces something a person can read.
Collapsing `_persist` into the event path belongs with L37, which owns
monitoring's own reporting; doing it here would have been a behavioural change
to a level that was not being audited.

## Tests

74 backend, 23 frontend. The ones that carry the level:

* `test_the_notification_package_cannot_reach_anything_that_trades` -- an AST
  walk over every module in the package. A property of the imports, not a
  promise about them.
* `test_no_route_can_create_a_notification` -- there is no POST that takes a
  title and a body, so a fabricated trading alert is unrepresentable rather than
  discouraged.
* `test_the_unique_constraint_is_what_actually_holds` -- two sessions racing on
  one replayed event; the database decides and one row exists.
* `test_a_severity_change_is_never_suppressed_by_the_cooldown`.
* `test_an_unknown_order_state_is_not_reported_as_a_failure`.
* `test_a_channel_that_raises_does_not_affect_the_others` -- IN_APP delivered,
  EMAIL retrying, notification untouched.
* `test_a_permanent_failure_is_not_retried_and_a_temporary_one_is`.

Three existing assertions were updated rather than worked around: the table
count (53 to 55), the `PRODUCED_NOW` set (`NOTIFICATION_CREATED` joins it), and
the monitoring severity vocabulary.

`tests/test_health.py` and `tests/test_realtime.py` fail on this machine because
the Compose Redis on 6390 is not running. That is environmental and predates
this level -- the connection is opened by `Hub.start()`, which is L07 code, and
the notification consumer starts after it and is never reached.

## What is NOT claimed

**Most categories cannot fire yet.** Nothing in this platform emits an order,
position, bot, risk or broker event today. Those rules are contracts that L17,
L19, L21, L22 and L38 will satisfy, and `GET /v1/notifications/contract` reports
`producing_now` per type so a quiet category can be told apart from an unbuilt
one. Twenty-three of thirty-seven routed types have a producer.

**Email has not been sent to a real server.** The provider is exercised through
its error classification -- which 4xx retries and which 5xx does not -- not
through a live send.

**No Discord.** The seat exists and reports NOT_CONFIGURED. That is L35.

**No retention policy.** Section 49 forbids deleting records without one, and
inventing the numbers was not this level's to do. The shape is documented.

**The SECURITY category has no producer.** `app/core/audit.py` writes rows for
every login, role change and CSRF failure and publishes no event. Defining one
here would have been inventing an event, which section 6 forbids; it belongs
with L39.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`. Every
`LIVE_GATES` entry is still False, including `monitoring_and_alerting`, which is
L37's and is not flipped by having built notifications.

## Next

**Level 35 — Discord.**


---

# Level 35 — Discord integration (2026-09-05)

The question: **can somebody find out without opening the platform?** And the
constraint: Discord is a destination, not a component of the trading system.

## What the audit found

**L34 had left a seat, not a gap.** `Channel.DISCORD` was in the enum, every
category already had a Discord row in the preference grid defaulting to off, the
`notification_deliveries` CHECK already accepted `'DISCORD'` as a channel, and
`discord_channel_from()` already existed and returned an adapter that reported
NOT_CONFIGURED.

So this level is **one adapter, two routes, four settings and one page**. No
migration. No new delivery status. No new preference column. No new event type.
No second event bus. No new dependency.

That is the whole argument for building a seat one level early: it is cheaper
than building a migration one level late.

## The decision that shapes everything else

**A webhook, not a bot.**

Section 4 asks for a bot only when the project genuinely needs commands,
interactive responses or guild management. It needs none of those. A webhook
posts to one channel, needs no gateway connection, needs no long-lived process
to supervise, and its credential cannot reach anything but that channel.

Commands were refused explicitly rather than deferred. `/portfolio` is a read of
somebody's private trading data authorized by a Discord identity this platform
has never seen. That is not a feature with a Discord library attached; it is an
authentication design, and it should be taken as one if anybody wants it.

**No HTTP client was added either.** `urllib.request` in `asyncio.to_thread`,
which is the argument `email.py` already made for `smtplib`: one POST per
notification, off the request path, bounded by a timeout, does not justify a
runtime dependency in a project where each one is argued for in
`requirements.txt`.

## The decision section 34 asks not to assume

**System-wide, not per-user** -- and the cost is stated rather than buried. One
webhook serves the deployment, so every user who enables the Discord channel
shares the destination. That is exactly why the channel defaults to off for
every category and every user, which it has since L34, and why `scope: system`
is on the status response and rendered on the page.

The alternative was refused with a reason: a per-user webhook is a bearer
credential per user, and storing one needs encryption at rest, a rotation flow
and a key-management decision nobody has made. Storing it in plaintext because
the feature was convenient would have been worse than not having the feature.

## The secret

The URL is read from settings in the constructor and held on the adapter.
Nothing returns it, **including redacted** -- a redacted secret is still a
statement about the secret's shape.

`_redact()` exists because Discord's error bodies can echo the request and a
delivery's `failure_reason` is readable by the user it belongs to. It removes
the whole URL and, separately, the token segment, so a partial echo does not
leak the part that authenticates. Four tests assert it: on the adapter, on the
registry, on the API responses, and on a stored delivery row after a failure
whose error text contained the URL.

There is no field in the browser to type a webhook into, and a frontend test
asserts the page renders zero input elements.

## Three states, not two

`NOT_CONFIGURED`, `DISABLED` and `CONFIGURED` are different operational facts,
and collapsing the first two into "off" is how an operator spends an afternoon
looking for a broken webhook that was never switched on. The same distinction is
why a routed delivery to a disabled channel is recorded SKIPPED rather than
FAILED: "nobody set this up" is not "the provider broke".

## Tests

54 backend, 10 frontend. The ones that carry the level:

* `test_no_trading_service_reaches_discord_directly` -- enumerates every file in
  `app/` that mentions the module and asserts the list is exactly the channel
  registry.
* `test_the_webhook_never_appears_in_anything_a_caller_can_read`.
* `test_a_provider_error_that_echoes_the_url_is_redacted`.
* `test_a_404_is_not_retried_and_a_429_is`, parametrised over 400, 401, 403,
  404, 500 and 503.
* `test_discord_honours_the_retry_after_it_was_given` -- header and JSON body --
  and `test_an_absurd_retry_after_is_capped`.
* `test_every_environment_is_distinguishable` -- five environments, five
  distinct labels.
* `test_discord_being_down_changes_nothing_about_the_trade`.
* `test_one_event_delivered_twice_is_one_discord_message`.
* `test_only_an_administrator_may_send_a_test` and
  `test_a_test_send_is_audited_without_the_webhook`.
* `test_l35_added_no_delivery_status_and_no_channel` -- the level's own claim,
  as an assertion.

## What is NOT claimed

**Nothing has ever been sent to a real Discord server from this machine.** Every
response code -- 204, 400, 401, 403, 404, 429 with both retry-after shapes, 500,
503, timeout and network failure -- is exercised against a mocked transport. The
embed has never been seen rendered by Discord.

**Most categories still cannot fire.** Unchanged from L34: nothing publishes an
order, position, bot, risk or broker event, so a user who enables Discord for
`BROKER` today will correctly receive nothing. That is six systems now waiting on
L38.

**No message editing.** A `BROKER_DISCONNECTED` followed by a
`BROKER_CONNECTED` posts twice rather than editing the first.
`notification_deliveries.provider_message_id` exists and is unused, which is
where that would go.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`. The
Discord module imports no risk engine, no order manager and no broker adapter,
and a test parses it to keep that true.

## Next

**Level 36 — admin panel.**


---

# Level 36 — Admin panel and system control centre (2026-09-05)

The question: **what does an administrator need that does not already exist?**
And the answer that shaped the level: much less than the brief's 76 sections
suggest, because most of what an admin panel wants is already built and already
authoritative somewhere else.

## What the audit found

RBAC with three roles and sixteen named permissions, `require_permission` on
every route, revocable server-side sessions, `/admin/users` with last-admin
protection, an `audit_logs` table with a scrubber that has been filtering
secrets since L04, CSRF, a rate limiter, and paginated collections with an
allow-listed sort. Bot control, model promotion, broker reconciliation and risk
limits all have working, permission-gated surfaces from L22, L28, L10 and L17.

What did not exist: a dashboard, user search, activation, session revocation, an
integration status view, and a page.

## The decision that shapes everything else

**The admin API has no route that touches a trading system.**

Section 1 says the panel must not become a second trading engine. Section 10
says to operate through existing services. Section 49 says never bypass the
authoritative ones. The obvious implementation is an admin route that calls
`BotManager.pause()` -- and that is what section 10 literally describes.

It was refused, and the reason is the one that keeps recurring in this project:
a second door is a second authorization surface, and **the one that drifts is
always the one nobody is watching**. `/v1/bots/{id}/disable` already exists,
already enforces `manage_bots`, and already goes through BotManager. An
`/v1/admin/bots/{id}/pause` that did the same thing would be a second place for
that gate to be got wrong.

So the panel reports and links. `GET /v1/admin/contract` serves the table of
where each control lives, the overview renders it, and three tests hold the
line: an AST walk proving the admin modules import no risk engine, order
manager, broker adapter, sizer or execution pipeline; an enumeration showing
exactly three write-shaped routes, all about a user's access; and a grep for any
assignment to `live_trading`, `trading_mode` or a `LIVE_GATES` entry.

## Dangerous actions cost something

A reason of at least eight characters, recorded in the trail, and the subject's
own email address typed back exactly. Not a yes/no prompt: **a confirmation you
can click without reading is not a confirmation**, and a phrase that is the
subject's own identifier means a misdirected request fails rather than
succeeding against the wrong row.

Plus three refusals that came out of writing it: an administrator cannot
deactivate themselves (the action wants a second person's name on it), the last
active administrator cannot be deactivated (the same reasoning L04 applied to
demotion), and deactivating an already-inactive user is a 409 rather than a
silent no-op.

## Deactivation removes access, not history

Section 13, and it needed saying in code as well as in prose. Deactivation sets
`is_active = false` and revokes sessions. Trades, journal entries, analytics,
strategies, accounts and bots are all kept, and a test asserts each survives.

If the user holds open positions or enabled bots, the response says so loudly
and **nothing is acted on**. A test seeds exactly that state, deactivates, and
asserts the position is still open and the bot still enabled. Closing somebody's
position is a trading decision; section 13 says not to invent emergency
liquidation behaviour, and this surface does not make trading decisions at all.

## Two things refused with reasons

**A feature-flag table.** Section 29 permits one if genuinely required. It is
not: this platform's flags are `LIVE_GATES`, which are deliberately code rather
than configuration, and settings, which are deliberately environment rather than
database. A database-backed flag is a way to change behaviour without a
deployment or a review, which is the property the gate table exists to prevent.

**A `severity` column on the audit trail.** Section 35 lists it as a filter. An
audit row records that something happened; grading how bad it was is a
monitoring judgement, it would be assigned by the same code that writes the row,
and a column that is uniformly "info" is not a filter anybody can trust.

And one thing built the smallest possible way: the configuration tab renders
**zero input elements**, and a frontend test asserts it. Section 28 forbids a
browser field that accepts `DATABASE_URL`; a validated field with a whitelist is
a whitelist somebody widens, and a page with no field cannot be widened by
accident.

## The audit trail

Extended, not duplicated. Section 31 names `admin_audit_logs`; section 51 says
not to create duplicates; `audit_logs` has been the administrative trail since
L04. Migration 0025 adds an `environment` column -- because §35 asks for it as a
FILTER and a key inside JSON is not one -- and three indexes.

`audit.record_admin` requires the reason **in its signature** rather than by
convention, and writes before/after state through the same `_scrub`, so a
snapshot cannot carry a credential into the table even if a caller assembled one
carelessly.

Immutability is a property rather than a habit: there is no DELETE and no UPDATE
against `audit_logs` anywhere in the application, and a test greps every module
to keep it that way.

## Tests

40 backend, 19 frontend. Every admin GET route is walked three times --
unauthenticated (401), plain user (403), trader (403) -- and a trader's
deactivate attempt is asserted to be refused *and* to have left the target
untouched. A separate test builds an application configured with an SMTP
password, a TradingView secret and a Discord webhook, walks every admin route,
and asserts none of them, nor the database URL, nor the Redis URL, appears in
any response.

## What is NOT claimed

**There is no MFA and no step-up re-authentication.** An administrative session
is a password and a cookie. Section 54 asks for this to be documented as a gap
rather than implied, and it is -- in the API, in the panel and in
`LEVEL_36_ADMIN.md`. It belongs to L39.

**Admin sessions have the same lifetime as any other.** A shorter privileged
lifetime needs a second session policy in L04's model.

**No audit retention policy**, for the same reason L34 has none for
notifications: the numbers are an operator decision nobody has made, and §52
forbids deleting silently.

**No object-level scoping.** Every administrator sees every user, because this
platform has no tenancy or team model to scope against, and inventing one here
would be inventing a product decision.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`, every
`LIVE_GATES` entry still False. The dashboard reports all of it and there is no
route that could change any of it.

## Next

**Level 37 — monitoring and observability.**


---

# Level 37 — Monitoring, observability and system health (2026-09-05)

The question: **is the platform healthy, and is trading safe?** And the
constraint that shapes the whole level: answer both by observing, and never by
acting.

## What the audit found

Four of the five observability layers already existed. JSON logging with levels
since L02, request ids threaded through every request, response and log line
since L02, correlation ids on every bus event since L07, a secret scrubber
since L04, three-state health checks with weighted aggregation since L02,
worker heartbeats and staleness detection since L02, hub counters since L07,
broker and provider health since L08 and L10, and L29's entire model-monitoring
engine.

What did not exist: a metrics registry, and anything that pulled the rest
together into one answer.

It also found **`system_events`** -- a table L05 created with the docstring
"operational events: connects, disconnects, reconciliations, halts", carrying
component, event type, level, occurred_at, correlation id and a JSON payload,
which **nothing had ever written to**. That is exactly the incident table
sections 43, 62, 63 and 76 ask for, so L37 shipped no migration.

## The decision that shapes everything else

**Five component states, not three.**

L02's `HealthStatus` has healthy, degraded and unavailable, which is right for
an HTTP readiness code. It has no word for "we did not observe this" and no
word for "nobody set this up", and both were needed on almost every collector.
A broker registry with no adapter is not unhealthy -- registering one is an
operator action. A market data feed with no stored bar cannot report freshness
at all.

Forcing either into healthy or unhealthy is a lie; the reassuring direction is
worse. So `UNKNOWN` and `NOT_CONFIGURED` were added, `HealthStatus` was left
alone, and `from_health` is the single translation between them. The moment
those two words existed, three collectors that would otherwise have had to
guess became straightforward to write.

`NOT_CONFIGURED` also never moves the overall state, which is section 12: an
unconfigured optional integration must not make a working platform report
unhealthy.

## Detect, and deliberately do nothing

Section 47 draws the boundary and L37 obeys it literally. This layer observes
worker staleness, orders whose broker state could not be established, positions
in `reconciling` and bot runs with no heartbeat -- and acts on none of them.

Two AST tests hold the line: one over imports (no risk engine, no order
manager, no adapter class, no execution pipeline) and one over called method
names, which refuses `connect`, `disconnect`, `reconnect`, `restart`, `start`,
`stop`, `submit_order`, `close_position`, `engage_kill_switch` and
`release_kill_switch`.

The reason is not tidiness. A monitor that restarts the thing it is watching
cannot tell you it failed to restart it.

## Hysteresis, and a real bug

Section 45 asks that a service failing for 100ms must not produce
DOWN/UP/DOWN/UP. `Tracker` requires two consecutive observations before raising
and two before clearing, and the first sighting is a baseline rather than an
incident -- a process that started while something was already down should show
it, not page somebody as though it had just happened.

**Writing the test for that found a bug.** `Tracker.observe` updated its
recorded state on every healthy observation, including while a recovery was
still being counted towards. That erased the "it was down" the threshold was
counting against, so a recovery could never have been announced. Caught by
`test_a_sustained_failure_produces_one_incident_and_then_a_recovery` and fixed
by only updating the recorded state when it was already good.

## One alert, through the layer that owns alerting

Section 46 says monitoring must not send Discord or email directly. An incident
becomes a `system_events` row and **one `SYSTEM_ALERT`** on L07's bus. L34's
consumer grades it, applies each recipient's preferences and routes it; L35
delivers it to Discord. `app/observability` imports no email provider, no
Discord adapter and no notification service, and a test greps the package for
all three.

No event type was invented: `SYSTEM_ALERT` has been catalogued since L07, and
the incident vocabulary travels in its payload.

One L34 change came out of this. `SYSTEM_ALERT` was catalogued with
`Audience.everyone` on the assumption it was a user-facing platform notice.
What L37 actually publishes onto it is infrastructure health, and a read-only
user cannot act on a degraded event bus -- so the audience moved to
`operators`, changed by the level that finally supplied the producer.

## Trading safety is a report, not a gate

Section 40 asks for a distinct verdict derived from the authoritative
components, and forbids a UI inventing it. It is derived from `database`,
`risk_engine`, `broker`, `oms`, `positions` and `market_data.freshness`, and it
always carries its reasons.

`UNKNOWN` is a real verdict and is used: a component nobody could observe does
not become SAFE by default.

And it is observational. Nothing in the platform reads it before trading -- the
RiskEngine vetoes, the OMS reconciles, the adapter refuses. If it returned SAFE
while the risk engine was blocking, the platform still would not trade, which
is exactly what makes it safe to compute a summary at all.

## What monitoring made visible

Three things that have been true for many levels and were not on a screen:

* `market_data.freshness` is **UNKNOWN**, and says *"no bar has ever been
  stored, so freshness is UNAVAILABLE rather than 0"*. `market_bars` has been
  empty on this machine since L08.
* `broker` is **NOT_CONFIGURED**, because no adapter has ever been registered.
* The OMS reports orders needing reconciliation, and nothing runs a
  reconciliation pass at boot.

The third is L38's, and L37 hands it a detector rather than a blank page.

## Tests

50 backend, 14 frontend. Beyond the two AST tests: a component flapped six
times produces zero incidents; a sustained failure produces exactly one
incident and then exactly one recovery; nine healthy optional components and
one unhealthy critical one aggregate to unhealthy; an identifier is refused as
a metric label at record time; a metric stops growing at 200 series; the public
health endpoint mentions neither the database nor Redis; every detailed route
answers 401 unauthenticated and 403 to a plain user; and a deployment
configured with an SMTP password, a TradingView secret and a database URL has
none of them appear in any monitoring response.

## What is NOT claimed

**No tracing spans.** Correlation ids thread request to event to log, which
answers "where did it go"; per-stage timing needs a tracer, and adding
OpenTelemetry is the competing stack section 3 warns about.

**No frontend error reporting.** Section 37 asks to reuse an existing provider.
There is none, and choosing one is a vendor decision rather than a level.

**No uptime percentage.** Section 64 asks not to claim one without the data,
and this platform stores state transitions rather than a sample series. The
endpoint says "insufficient history".

**Latency is measured for the health probes, not the trading path.** Most
stages of webhook to journal have no producer yet, and instrumenting a path
nothing walks would be measuring nothing.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`, every
`LIVE_GATES` entry still False -- including `monitoring_and_alerting`, which is
nominally this level's. It stays False for the reason every gate stays False:
flipping one is an operator decision taken in a reviewed pass, not a side
effect of the level that wrote the code.

## Next

**Level 38 — startup, disconnect and reconciliation.**


---

# Level 38 — Recovery, resilience and reconciliation (2026-09-05)

The question: **after a crash, how does the platform know it is safe to start
again?** And the answer that shaped the level: by asking the systems that
already know, in an order, before anything else runs.

## What the audit found

Almost all of the checking. Four reconcilers, each written by the level that
owns the state it compares:

* `app/brokers/reconcile.py` (L10) — venue positions and orders against ours.
* `OrderManager.reconcile` (L19) — the only exit from `unknown`, which works by
  asking the venue and never by re-sending.
* `app/positions/reconciler.py` (L21).
* `BotSupervisor.plan_restart` (L22) — what should happen to each bot, which
  refuses far more often than it acts.

Every one of them reports rather than repairs, which is exactly what step 14
asks for. Plus L19's `intent_id` idempotency, L20's in-process signal
deduplication, L02's worker loop that survives a failing tick, L02's graceful
shutdown in the right order, L34's bounded retry with backoff, and L37's
detector.

**And not one of the four ran at boot.** `MIGRATION_STATUS.md` had said so
since L30: *"the pieces exist; nothing calls them first."* That sentence was
the level.

## The decision that shapes everything else

**`app/recovery/reconciliation.py` contains no comparison logic.**

The obvious implementation is a recovery module that knows how to compare a
local position to a broker position. It was refused for the reason this project
keeps arriving at: a second implementation of one fact is a second answer, and
the one on the dashboard would eventually disagree with the one the OMS acted
on. So the module calls the four reconcilers and collects what they said.

What is genuinely new is the ordering and the latch.

## Safe mode

The thing L38 actually adds. A latch that:

* **never closes without a reason.** Step 18 says not to use safe mode to hide
  errors, so every latch carries a `SafeModeReason` and a detail, and the
  refusal a caller sees names the condition — *"the platform is in safe mode,
  so no new order is submitted. UNKNOWN_ORDER_STATE: 1 order whose venue state
  was never established"* — rather than the mode.
* **blocks three paths, all server-side**: `POST /v1/orders` answers 409, the
  execution pipeline refuses at gate zero with its own outcome, and the bot
  supervisor's safety check refuses recovery.
* **blocks nothing observational.** Monitoring, reconciliation, position and
  order visibility, risk evaluation and the recovery routes keep working,
  because reconciling is how you get out.
* **is a second refusal, never a replacement for one.** It is checked before
  risk and adds a veto; it never approves anything, never releases a kill
  switch and never widens a limit. A submission that gets past it faces every
  check it faced before.
* **is process state, deliberately.** A stored flag is one somebody forgets to
  clear, and one set by a process that has since died is a platform that will
  not trade for a reason nobody can find. The latch is re-derived at every
  startup, and every open and close goes to `system_events`, so the history
  outlives the state.

And the asymmetry that matters: **closing it is automatic and free; opening it
re-runs the whole sequence.** `POST /v1/recovery/safe-mode/exit` runs the
fourteen steps again and clears only the latches whose condition has actually
gone, then says which are still holding. A release that skipped the re-check
would let somebody resume trading into an unreconciled account — and a latch
equally easy to open and close is one that gets opened by whoever is in a
hurry.

## L22's seat, filled without lowering the bar

`BotSupervisor` has taken a `SafetyCheck` since L22 and defaulted to
`_refuse_by_default`, with the note that *the absence of a check is not evidence
that recovery is safe*. That default has been refusing everything for sixteen
levels.

L38 supplies an implementation whose every branch is a refusal: safe mode, a
kill switch, an order whose venue state was never established, a position the
platform could not settle, an adapter that reports unusable. `None` means "these
five found nothing" — L22's own gates still run afterwards. There is no path
through the new module that makes a restart more likely than L22 intended.

## Three things reported as SKIPPED rather than OK

Writing the sequence made the distinction sharp:

* **no broker adapter registered** → *"this is not a clean reconciliation; it is
  the absence of one"*. Reporting it as OK would be the fake health L37 spends
  a section on.
* **no bar ever stored** → *"that is not staleness"*. A fresh install must not
  boot into safe mode for having no history.
* **no webhook secret** → L09's deliberate default, which refuses every alert.

## Tests

44 backend, 13 frontend. Four of them are structural rather than behavioural:
an AST walk asserting the package imports no order manager, adapter, risk
engine or sizer and never calls `submit`, `cancel`, `modify`, `close` or
`close_now`; and three greps — for `live_trading =`, for `DROP TABLE` and
`DELETE FROM`, and for a notification provider.

The behavioural ones that carry the level: an unknown order latches safe mode
and is left byte-for-byte unchanged; a dropped table latches safe mode and
nothing migrates; a broker registry that raises produces a FAILED step and the
sequence continues; the pipeline refuses with `Outcome.safe_mode`; a bot is not
recovered over an unresolved order; and releasing safe mode while the condition
still holds leaves it shut.

**A real bug came out of writing them.** Adding `Outcome.safe_mode` without
adding it to `NO_ORDER` made `created_order` report True for a refusal that
sent nothing — the execution log would have claimed an order was created when
the pass stopped at gate zero. Caught by the test that exercises the gate.

## Backup: a refusal with a reason

Steps 22 and 23 ask for a backup strategy and a restore procedure. There is no
backup script, and L38 did not write one.

A backup script this project cannot test is a backup nobody should trust:
`pg_dump` in a container this repository does not deploy, on a schedule nothing
runs, to storage nobody has chosen, with a key nobody holds, is a file that is
discovered to be empty on the day it matters.

`BACKUP_RESTORE.md` documents what must be backed up, the fifteen-step restore
order — with reconciliation at step 8, because a restored database believes
things about the venue that are as old as the dump — and an **honest RPO and
RTO**: *since the last manual dump*, and *however long a person takes*. Step 22
says not to claim ones the infrastructure cannot achieve, and writing "RPO: 15
minutes" here would be the most dangerous sentence in the repository.

## What is NOT claimed

**No automatic reconnection.** A disconnected adapter is detected, latches safe
mode and waits for a person. Step 5 asks for controlled reconnection; a
reconnect loop belongs with a live adapter, and this platform has never
registered one.

**No fault injection against anything real.** Every failure mode — a registry
that raises, a hub that raises, a dropped table, an unknown order — is
exercised against a fake. There is no terminal to disconnect.

**The latch is per process.** One API process today; a second needs a shared
latch, which is a row and a lease rather than a flag.

**Live trading unchanged.** `TRADING_MODE=paper`, `LIVE_TRADING=false`, every
`LIVE_GATES` entry still False — including `restart_reconciliation`,
`unknown_status_reconciliation` and `broker_sync_on_connect`, which are
nominally this level's. They stay False for the reason every gate stays False:
flipping one is an operator decision taken in a reviewed pass, not a side
effect of the level that wrote the code. The mechanism now exists; asserting
that live execution may rely on it is somebody's signature.

## Next

**Level 39 — security.** Both L36 and L38 left it the same named gap: there is
no multi-factor authentication and no step-up re-authentication anywhere in
this platform, and the administrative session that releases safe mode is a
password and a cookie.

---

## Level 70 — controlled live activation layer (2026-09-07)

**Added, and every piece of it can only refuse.** `backend/app/live/`:
`Allowlist` (empty permits nothing), `LiveTradingGate` (checks in nine groups,
PASS/FAIL/WARNING, missing evidence FAILS), `ActivationMachine` (twelve states;
`DISABLED -> ACTIVE` is not in the transition table), and
`python -m app.live.preflight`. 84 tests in `tests/test_live_gate.py`, one of
which parses every module in the package to prove none can import an order
manager, a broker or the risk engine.

Five settings, all fail-closed: `LIVE_ALLOWED_ACCOUNT_IDS`,
`LIVE_EXPECTED_ACCOUNT_ID`, `LIVE_EXPECTED_SERVER`,
`ENABLE_LIVE_TRADING_CONFIRMATION`, `LIVE_QUOTE_MAX_AGE_SECONDS`. A test
asserts that with all five set as favourably as possible,
`live_execution_allowed` is still False and there are still 12 blockers -- a
setting widens what the platform WANTS, never what it CAN.

**No trading logic changed.** Not one line of the execution path.

## What is NOT claimed

**Nothing was activated.** `python -m app.live.preflight` returns
`NOT_READY_FOR_LIVE`, 34 blocking checks, run against the real terminal. The
activation machine never left `DISABLED`, no order was placed, no gate was
flipped and no fence was weakened.

**The platform still has no demo path.** `_ADAPTERS = {"simulator": "paper"}`,
so `MT5Adapter` -- written, demo-fenced, wrapping the toolkit rather than
reimplementing it -- has no runtime registration path and `accounts_broker` is
still 0.

**There is no live broker adapter and no credential storage**, and
`tools/mt5_paper.assert_demo` refuses any account whose `trade_mode` is not 0.
Those are the reasons live is unreachable, and they are not configuration.

## Level 70b -- the demo venue (2026-09-07)

**The seat that was empty for eleven levels is now fillable.**
`_ADAPTERS = {"simulator": "paper", "mt5_demo": "demo"}` in
`app/api/v1/brokers.py`. `MT5Adapter` had existed since L10, wrapped
`tools/mt5_paper` rather than reimplementing it, and had **no runtime
registration path** -- so `accounts_broker` was 0 and the platform's execution
machinery had never run against a venue it did not also write.

The route connects BEFORE it registers and registers nothing if that fails, so
a registry entry never names a venue that did not answer. Every failure path
releases the terminal handle: `connect()` opens the terminal and *then* runs the
fence, so a refusal would otherwise leak one connection per rejected attempt.
`expect_account` refuses unless the terminal holds the login somebody named.

15 tests in `tests/test_mt5_demo_venue.py`, one of which drives a **real
terminal** and is skipped where MetaTrader5 is absent. Verified live: account
5055473926 @ MetaQuotes-Demo, 12,412 symbols, health usable at 1.1ms, seven
positions carrying the toolkit's MAGIC.

## What is NOT claimed (L70b)

**Nothing is registered.** The route can be called; nobody has called it on the
running deployment, which is in `TRADING_MODE=paper` and would refuse a demo
venue on the mode fence. `accounts_broker` is still 0.

**Windows only.** The `MetaTrader5` package does not exist for Linux, so the API
CONTAINER CANNOT USE THIS ADAPTER -- it returns 503 and registers nothing. Using
the demo venue means running the API process on the host that holds the
terminal. That is a property of MetaTrader, not of this design.

**Live is untouched.** No `_ADAPTERS` value is `live`, no gate flipped, and a
test asserts `live_execution_blockers()` is identical before and after a
successful registration.

## Level 70c -- the first real order (2026-09-07)

**It filled.** Order `00943128-ecd6-42bb-accb-2264aa023601`, MT5 ticket
`58325115749`, 0.01 EURUSD at 1.16104, stop 1.15904 and target 1.16304 applied
at the venue. Risk approved it, sizing produced the volume from a $2 budget, the
OMS wrote the row before the send, and the adapter reached MetaTrader. The full
record is `FIRST_DEMO_VENUE_RUN.md`.

**It found three defects and one gap**, none of which the test suite could have
found:

1. `POST /v1/orders` set `orders.signal_id` to the idempotency key, which is a
   foreign key to `signals` -- every manual order was a 500 on PostgreSQL.
   FIXED.
2. That survived 174 API tests because **SQLite does not enforce foreign keys**
   and nothing turns the pragma on. Covered for one column, reported for the
   rest.
3. `MT5Adapter` passed ATR multiples of 0.0 meaning "no bracket"; the toolkit
   read it as a bracket of zero width and sent `sl == tp == entry`. One attempt
   was rejected 10016, the next filled and was immediately closed by the
   toolkit's own safety net. FIXED in `tools/mt5_paper.place()`.
4. **No broker-mode fill creates a `positions` row.** Only `app/paper/service.py`
   constructs one. So `PositionManager` cannot see, manage or close a position
   the platform itself opened. OPEN, and it is the next real piece of work.

## What is NOT claimed (L70c)

**One order is one sample.** Partial fills, requotes, real slippage, cancels,
disconnects and unknown-state recovery are all still exercised against
`FakeBroker` only.

**The platform cannot manage what it opened.** Gap 4 above. The position from
this run is live at the venue and the platform has no record of it as a
position.

**Live is untouched.** Demo throughout, `assert_demo` passed on every connect,
`LIVE_GATES` unchanged, 12 blockers before and after.

## Level 70d -- the platform knows what it opened (2026-09-07)

**`app/positions/ingest.py`.** One function, `record_fill`, called from the order
route in the SAME transaction as the order it came from, so an order recorded
filled and the position it implies cannot disagree. Only on a confirmed fill,
never for paper, idempotent on the venue's ticket, and every foreign key it
writes is one the caller handed it -- nothing is derived from a value that
merely looks like an id, which is the mistake `orders.signal_id` made.

`app/api/v1/brokers.py::reconcile` now compares against those rows instead of
the hard-coded empty list it had passed since L10.

Proved on a real order, ticket 58326177606, 0.01 EURUSD at 1.16118: the
positions row exists, and reconciliation reports 8 broker positions against 1
internal, flags the harness's seven as `unexpected_at_broker`, and **does not
flag ours** -- the platform and the venue agree about it.

24 tests in `tests/test_position_ingest.py`, most of them about what is NOT
written: an unknown outcome, a missing fill price, a missing ticket and a paper
order all produce no row, because a position written from an unconfirmed fill is
a belief with nothing behind it.

## What is NOT claimed (L70d)

**The platform still cannot CLOSE a broker position.**
`/v1/positions/{id}/close` refuses with *"no usable quote ... refusing to close
against a price the platform does not have"*, and the refusal is correct. Two
things stand behind it, both structural:

* the close route prices from `app.state.market_data`, never from the broker
  book -- the two are documented as different measurements that are never
  merged -- and that feed has 705 bars across two symbols and no live quotes;
* a position cannot name its venue: the only column that could,
  `positions.broker_account_id`, is a foreign key to `broker_accounts`, a table
  with zero rows, because registering a venue fills two in-process registries
  and creates no account record.

**No netting, no close accounting, no partial-close path.** Each fill with its
own ticket is its own row, which is what a MetaTrader hedging account gives and
is not what a netting account would.

## Level 70e -- the full position lifecycle (2026-09-07)

**A position can now be opened at a real venue, recorded, reconciled and
closed through the platform.** Ticket 58328827918, 0.01 EURUSD opened at
1.16319 and closed at 1.16315, outcome CONFIRMED, `needs_reconciliation: false`.
Full record in `DEMO_VENUE_LIFECYCLE.md`.

Three pieces made it possible: `app/brokers/accounts.py` writes the
`broker_accounts` row at registration from what the terminal reported; the
adapter is registered under both the operator's label and that row's durable id,
which is the key `BrokerExitExecutor` looks a manager up by; and a broker
position now prices off its own venue rather than a feed that has no live
quotes.

It found four more defects. Two fixed: a close was never confirmable, because
`close_own` recorded a retcode and not the fill; and reconciliation could not
write at all on PostgreSQL, because aware datetimes were going into naive
columns -- found at three separate write sites and now behind one shared
`naive_utc`.

## What is NOT claimed (L70e)

**A close is not durable before it is sent.** The OMS persists state before a
venue call precisely so a crash is distinguishable from a send that never
happened. Closes have no equivalent: the venue is asked first and the row is
written after. Observed once -- a close executed for -0.03 and the write rolled
back, leaving the platform believing the position was open. The reconciler
settles it, but "recoverable by a sweep" is weaker than "durable before the
call".

**No netting and no partial-close accounting against a real venue.** Each fill
with its own ticket is its own row, which is what a MetaTrader hedging account
gives and is not what a netting account would.

## Level 70f -- realised P&L is money (2026-09-07)

**The figure comes from the venue now.** `(fill - entry) * direction * quantity`
is a PRICE DIFFERENCE: it omits the contract size, so 0.01 lots of EURUSD booked
-0.0000004 where the account received -0.04, and a NUMERIC(18,4) column stored
that as 0.0000. Every reader of `positions.realized_pnl` inherited it.

No multiplier repairs it -- contract size alone fixes EURUSD and leaves USDJPY,
XAUUSD and DE40 wrong, because the profit currency is not always the account
currency. So `tools/mt5_paper.deal_money()` reads `profit`, `swap` and
`commission` back from the server's own deal history and the platform records
their sum, carried through `OrderResult` -> `FillRecord` -> `CloseOutcome` ->
`positions.realized_pnl`. A test asserts every link can carry it, because a
field missing at one end would look exactly like a venue that did not report.

**A broker close whose money the venue did not report books NOTHING**, and the
position event says `realized_pnl_source: "not reported"`. A gap is recoverable;
a plausible wrong number is not. Paper is untouched -- the simulator is its own
venue and its arithmetic is self-consistent in the units it quotes.

Proved on position 58329837257: venue -0.02 USD, `realized_pnl` -0.0200,
source `venue`. 10 tests in `tests/test_realized_pnl_is_money.py`.

## Level 70g -- the close order is actually recorded (2026-09-07)

**The close path always persisted before the venue call. It had never worked.**
`orders` contained ZERO rows with a `close:` intent, including for closes that
succeeded end to end, because the executor built its `OrderProposal` from
`PositionView.symbol` -- which `view_of` fills with the row's `symbol_id` --
while the order store resolves a tradable CODE:

    OrderNotRecorded: symbol '4097df82-...' does not resolve. Nothing was sent.

`_record` logs that and proceeds, which is right: a close that already happened
must not be refused because storage blinked. The cost was that the durability
this path was designed to have was never once achieved, silently, for every
close.

**Nothing caught it because `PositionView.symbol` means two different things.**
`view_of` puts a UUID there; every hand-built view in the test suite puts
"EURUSD" there. The suite agreed with itself and production disagreed with the
suite. That is the THIRD identifier confusion this exercise has found, after
`orders.signal_id` and `positions.broker_account_id`.

`PositionView` now carries an explicit `symbol_code`, `PositionManager` resolves
it once per symbol, and a close with no code is refused before the venue is
asked. Proved on position 58330565300: `close:729785da-...:0.01000000`,
status filled, fill 1.16234.

## What is NOT claimed (L70g)

**The close order's `broker_order_id` is empty.** `MT5Adapter.close_position`
returns the position ticket, not the closing order's. Reconciliation matches on
the position, so nothing is lost today, but the closing order cannot be looked
up at the venue by id.

**`PositionView.symbol` is still ambiguous**, now documented rather than fixed.
Making it mean the code everywhere would ripple into `MarketState` and the paper
engine, which is a separate change.

## Level 70h -- one meaning for a symbol (2026-09-07)

**`PositionView.symbol` and `MarketState.symbol` are the tradable CODE, both of
them, everywhere.** They held the row's `symbol_id` in the application and
"EURUSD" in every hand-built test view, which is why the suite agreed with
itself while the application disagreed with the suite. The transitional
`symbol_code` field added an hour earlier is gone: two fields for one idea was
the problem, not the fix.

`view_of` fills `symbol` from the code the manager resolves, and leaves it EMPTY
when a row's symbol does not resolve -- a refusal anything building an order acts
on, rather than a fallback to an id that would fail later and quietly.
`run_once` keys its quote dictionary by the code too, like the market feed that
produces it.

This closes the last of the three identifier confusions. The other two --
`orders.signal_id` given an idempotency key, `positions.broker_account_id`
nearly given a registry label -- each silently broke a write that only a real
venue revealed.

Proved end to end after the change: position 58331472838 opened at 1.16229 and
closed, `close:623070ab-...:0.01000000` recorded `filled` at 1.16229.

## Level 70i -- the signal path reaches a real venue (2026-09-07)

**A TradingView alert became a real order.** Signal 3c98699c -> bot c8461d2a ->
broker account 8ea1cacb -> order `tv:id:demo-signal-1788782446` -> MT5 ticket
58332563074, filled 0.01 EURUSD at 1.16237 with the alert's bracket applied,
recorded as a position, and closed through the platform for the venue's own
+0.02. Full record in `SIGNAL_PATH_FIRST_RUN.md`.

**Two things were missing.** `route_for` needs one enabled bot for a strategy
version in the platform's mode, and there was no way to create one -- the only
bot-creation route is `/v1/paper/bots`, which hard-codes `mode="paper"`
deliberately. `POST /v1/bots` now creates a DEMO bot behind the same fences the
venue registration uses: the platform's own mode, an active `broker_accounts`
row whose mode matches, an existing strategy version, step-up, a reason, a
security event and an audit row, and a refusal when a second enabled bot would
make routing a choice nobody made.

And `record_fill` had been wired into the manual `/v1/orders` route only, so a
signal that filled at the venue produced an order and no position --
the L70d gap still standing on the path that actually trades.
`DatabaseOrderStore.record` now writes both in one session.

**The platform diagnosed the rest itself.** The first alert was retired
`signal_not_executable`, and the signal row said why: routed correctly, risk
budget from the bot, but `price_reported: None` and *"this bot has not opted
into the alert's bracket, and the platform has no bracket policy of its own;
sizing will refuse this signal"*. Both are designed knobs -- an alert price and
`POST /v1/bots/{id}/bracket-source`, which has its own step-up scope because
promoting an external payload's stop lets that payload decide position size.

## What is NOT claimed (L70i)

**The bracket came from the alert**, which is one of two supported sources and
opt-in per bot. A strategy with its own bracket policy would not need it.

**A position with a NULL `broker_account_id` is invisible to the account-scoped
reconciliation sweep.** One legacy row sits open in the platform and closed at
the venue for exactly that reason. New positions always carry the account.

**One position from the first alert is open at the venue with no platform
record** -- opened before the ingestion fix, carrying a valid bracket, left
rather than closed out of band.

## Level 70j -- the platform's record against the venue's (2026-09-07)

**`python -m app.brokers.venue_audit --mode demo`.** Every other check in this
platform compares the platform against itself, which is exactly what missed a
`realized_pnl` of 0.0000 against an account that received -0.04. This reads
MetaTrader's own deal history and compares entry, size, status and money for
every position the platform believes it opened. Read-only on both sides, and a
test parses the module to prove it: no add, no commit, no order_send.

It is NOT reconciliation. `reconcile` asks whether the venue still holds what we
think it holds -- about OPEN positions, against the current book. This asks
whether what we recorded was true -- about CLOSED ones, against the history. A
position can reconcile perfectly all the way to a close and still have been
booked at the wrong price.

**9 checked, 5 agree, and the boundary is chronological.** Every disagreement
predates the fix for it; every trade after the fixes agrees, including the one
the signal path produced. The four are this session's own defects, still visible
in the record they left: a position the harness harvested out of band, a close
that was never confirmable, a close whose write rolled back, and the money
defect itself. Nothing was rewritten to make the report clean.
Full record in `LEDGER_COMPARISON.md`.

## What is NOT claimed (L70j)

**The toolkit's ledger is not part of the comparison.**
`data/track_record.jsonl` holds 252 positions and none of today's nine -- its
last merge predates them. `track_record.py --merge` WOULD ingest them, because
`tools/` and the platform share `MAGIC 770315` and the toolkit cannot tell them
apart. That is a decision about what the research sample is, not a default, so
the ledger was left alone.

**Nine positions is not a sample.** One order, one fill, every time. Slippage,
partial fills, requotes and latency remain unmeasured.

## Level 70k -- the two systems stop acting on each other (2026-09-07)

**Separate magic numbers.** They shared 770315 deliberately, "so both see the
same positions". Seeing was never the problem -- ACTING was: `close_own` closes
every position carrying its magic, so the running harness harvested TWO
positions the platform had opened, at >= $0.50, and the platform's rows went
stale underneath it. It is the first entry in `LEDGER_COMPARISON.md`.

The adapter now tags its own orders **770316** and keeps `TOOLKIT_MAGIC = 770315`
for reading. `tools/mt5_paper.place`, `close_own` and `own_positions` take a
`magic` argument defaulting to the toolkit's, so ONE implementation of order
construction still serves both -- what separates the systems is the tag, not the
code.

**Separating them must not blind reconciliation**, so `get_positions`,
`get_orders` and `get_order_history` now default to NO filter. The platform sees
everything the account holds; it only acts on what it opened.

Proved with the harness running throughout: 8 positions at the venue, 7 tagged
770315 and 1 tagged 770316; `own_positions()` returned 7 and excluded ours; the
platform's reconcile saw all 8, flagged the harness's 7 `unexpected_at_broker`
and matched ours. The position closed for the venue's own +0.03.

## What is NOT claimed (L70k)

**The research ledger has still not been merged.** `track_record.py --merge`
would now be separable by magic, so "we cannot tell them apart" is no longer a
reason to avoid it -- but what the sample behind every "no edge" finding in
`CLAUDE.md` should contain is a decision, not a default.

**Existing positions keep the old tag.** The seven the harness holds, and every
platform position opened before this change, carry 770315. Only new platform
orders are tagged 770316.

## L70l -- the ledger can now say whose trades it holds

Making that decision decidable turned up a defect older than anything today, and
it is in the research toolkit rather than the platform.

`mt5_account.closed_trades` pairs every deal on the **account**. So the ledger
behind `reports/track_record.json` -- and behind every "no edge" finding in
`CLAUDE.md` -- has always meant *"every closed trade on this login"*, not *"every
trade this tool made"*, and nothing on the row could distinguish them because the
entry deal's magic was never recorded.

Measured on the demo account: **258 closed trades over 30 days, `{0: 1,
770315: 256, 770316: 1}`**. The untagged one is 58323811275, AUDCAD opened by
hand at 11:05 and closed for **-0.17** -- bigger than any single trade the
platform made, and not in the ledger only because the last merge predates it.
The next merge would have taken it as one of the tool's own.

**Fixed.** `closed_trades` records the ENTRY deal's magic (the entry, because a
server stop-out can carry its own tag). `merge()` takes `magic=`, defaulting to
the harness's 770315, writes it on each row, and prints the account census
beside what it took. `--all-magics` restores the old behaviour explicitly.
`census` and `select_by_magic` are split out as pure functions so the question
*whose trades is this* is tested without a terminal
(`tests/test_rule_backtest.py`).

**The fix is not retroactive, and that is the part worth reading.** The adapter
shared 770315 until this evening, so **nine of the ten** trades the platform made
at this venue carry the harness's tag; only 58334342528 is separable by magic.
The default therefore excludes 2 of 258 and cannot do better. They are separable
by identity instead -- `--exclude` takes position ids, and the platform's own
`positions` table is where the list comes from. Full table in
`LEDGER_COMPARISON.md`.

## What is NOT claimed (L70l)

**The merge has still not been run.** Nothing was added to
`data/track_record.jsonl`; its 252 rows are untouched and carry no magic field.
What the research sample should contain is a decision about evidence, and taking
it as a side effect of a cleanup is exactly the wrong way to make it.

**The existing 252 rows cannot be audited this way.** They predate the recording,
so their provenance rests on nothing else having traded this login before today
-- not on anything in the file.

## L70m -- the position no sweep could reach, and the close with no name

Three defects, all of the same shape: something real that no record could refer
to.

### The sweep stepped over a position and said it was clean

`PositionReconciler._local` scopes by account, which is right -- one venue's
adapter can only speak for its own account. But a row with BOTH account columns
NULL matches no account_id, so every sweep silently stepped over it. Position
58326177606 sat that way all day: `open` in the platform, closed at the venue,
and reachable by nobody. It was not filtered out by a rule anybody could read;
it simply never appeared, which is the harder kind of invisible.

`ReconcileReport.unattributed` now lists them, and a report carrying one is
**not `clean`**. That is the load-bearing half: a sweep reporting agreement
while a position sits outside every account it could have swept is describing a
subset and calling it the whole -- the exact shape of defect this module's own
venue comparison exists to catch.

**Reported, never touched.** The sweep holds one account's adapter and cannot
know the orphan was held there, so closing it or attaching it to this account
would be a guess about which venue a position lived at. Every sweep of a mode
reports the same orphans, deliberately: a finding that appears once and then
hides until somebody sweeps the right account is one nobody would see.

### A close had no venue identifier

`close_own` reported a retcode, and after this morning a fill, but never the
ticket of the close ORDER it had just sent -- so `orders.broker_order_id` was
empty on every close the platform has ever made. That is precisely the handle an
`unknown` close is settled by: the OMS parks the order, refuses to retry it
(retrying an uncertain close is how a hedging account opens a position the other
way), and the only safe way out is to ask the venue about that ticket.

It is now recorded on **every** branch, including the ones that failed. The
failing branch is the one it exists for.

### A fill was keyed by the wrong thing

`_apply_result` set each fill's `broker_deal_id` from `result.position_id` --
the same value for every fill of one position. `FillBook.already_recorded`
deduplicates by that field, and its own docstring says why the check must be by
identity: *"two genuine fills of the same size at the same price are a real
thing that happens"*. Two genuine partial fills of one order would both have
carried the position's ticket, and the second would have been discarded as a
replay.

`OrderResult.deal_id` now carries the venue's DEAL -- the execution, as distinct
from the order that asked for it and the position it belongs to -- and the fill
falls back to the position or order id when a venue reports none. It falls back
rather than generating one: an invented id looks unique and deduplicates
nothing.

Latent, not active: one order has produced one fill every time so far. It is
recorded here because "we have never seen it" is what the whole day has been
about.

## What is NOT claimed (L70m)

**The partial-fill collision was never observed.** Every order this platform has
sent returned a single fill. The defect is read off the code and the dedupe
contract, not off a broken trade.

**The orphan is still an orphan.** 58326177606 is now visible in every demo
sweep and is still `open` in the platform and closed at the venue. Making it
visible is what was fixed; deciding what to do about it is a person's call, and
attaching it to an account would be inventing the attribution the reconciler
exists to refuse.

## L74-76 -- three levels audited, and what the audits found

All three arrived in sequence and all three mandate an audit before any code.
The audits are `LEVEL_74_RESEARCH_AUDIT.md`,
`LEVEL_75_RESEARCH_VERIFICATION_AUDIT.md` and
`LEVEL_76_DECISION_INTELLIGENCE_AUDIT.md`. **No code was written for L74 or
L75.**

They found the same thing from three directions: **the methodology these levels
ask for is largely already built, and it has only ever been pointed at trading
rules rather than at a company.** Selection-bias control
(`app/research/selection.py`), behavioural leakage detection
(`app/datasets/leakage.py`), point-in-time splits, clustered t and calibration
(`app/validation/statistics.py`, `app/monitoring/stats.py`), one centralised
metrics implementation (`app/analytics/metrics.py`), one simulator and a module
that exists to forbid a second (`app/validation/economics.py`), a model
registry, an evidence-kind taxonomy (`app/review/contract.py`) and a review
layer whose assessors structurally cannot see the outcome.

L76's own core turned out to exist too, as `app/portfolio/decision.py` from
L60 -- including a `Layer` ordering from preference up to hard safety that L76
does not specify and should adopt, and four of its section 50 safety tests
already written by name.

**The blocking finding, recorded rather than worked around.** L75 exists to
verify L74's objects and L76 consumes both. Ten of L74's deliverables return
zero files; 16 of the 28 fields in L76's `InvestmentDecisionContext` have no
source in this system. Built now, twelve of L76's thirteen research engines
would return `INSUFFICIENT_DATA` permanently -- the exact failure all three
levels forbid, which is maximum apparent sophistication carrying zero
information.

### The one L76 change that was real, and was made

`Freshness` gained the two states section 5 names and the module lacked.
`CONFLICTED` could not previously be expressed at all: an input whose sources
disagree is not missing, stale or invalid, and had to be mislabelled as one of
those three. A stale figure can be refreshed by asking again; a contested one
cannot. It is recorded on the input rather than resolved, because choosing a
winner silently is what L75 section 6 forbids.

`AGING` is opt-in with **no default threshold**. `DEFAULT_MAX_AGE` already
records itself as an assumption rather than a measurement, and a second
unmeasured boundary inside the first would compound that rather than inform
anything.

Safe by construction: every consumer tests `is not FRESH`, so a state nobody
taught them about is degraded rather than ignored.

**A duplication was found and deliberately not fixed.** There are two
`Freshness` enums -- `decision.py` with six uppercase states and `state.py`
with three lowercase ones, 29 call sites against 16. They are not
interchangeable: `state.unknown` is `decision.INVALID`, and `MISSING`,
`CONFLICTED` and `AGING` have no counterpart there. Merging them is a semantic
refactor across 45 call sites, not a rename, so both now carry a comment
pointing at the other rather than being merged carelessly at the end of a long
session.

## 2026-09-11 -- the night a session finished

The harness had never once reached its own deadline. Eight of eight runs died
early, which meant the `--flat-by` flush -- the single mechanism that bounds a
losing tail overnight -- had never executed outside a test. P1b built the crash
journal, the console guard and `--detach` to make finishing possible; it could
not prove that finishing happened.

It happened. Session `20260910-224639` started at 22:46 and ran 433 minutes and
1,298 passes. At 05:59:24 the flush closed all six open positions by name, the
process exited 0, and the watchdog wrote `the session ended and will NOT be
restarted: the session finished normally` -- declining to spend a restart, which
is the branch that matters, because a watchdog that relaunches a completed
session is worse than no watchdog. `CRASH_REPORTS/session-20260910-224639.json`
records `status: completed`, `flushed: 6`, `positions_open: 0`.

The night's result: 21 opened, 12 harvested, realised -14.95 on a 100,000
account. That is not an edge and is not claimed as one.

**One session is evidence that the mechanism works, not a reliability rate.**
The three artefacts that show it -- the journal record, the log tail, the
watchdog line -- are what the next long run gets read against, rather than
assuming the question is now settled.

Two earlier records from the same evening are worth keeping beside it, because
they are what the journal is for. `20260910-220313` died after 11ms with
`AttributeError: 'NoneType' object has no attribute 'write'` -- the detached
launch had no stdout, fixed in `0c0be45`. Without the journal that death would
have been a log file that simply stopped.

**The ledger was merged again**, on the same terms as before: the harness's own
tag, and `--exclude` for the nine platform trades that carry it. 247 new trades,
2 excluded by tag, 9 by id; the ledger stands at **751**. The account split the
last merge exposed is now printed rather than pooled -- `5055473926` holds 499
trades at -30.39 from 09-04 to 09-11, and the older 252 at -22.37 remain
`unrecorded`, because that login is still not knowable from here.

The R-multiple over 741 trades is **-0.014R, t = -0.92**, by date -1.2 and by
symbol -1.77. 2,599 more trades would be needed to reach a pooled t of 1.96 at
this effect size. Every read of this sample has said the same thing and this one
does not differ.

## Next

**P2 -- fence the harness path.** This displaces the ablation item below, and
the reason is the one the audit gave: `tools/take_profit.py` ->
`tools/mt5_paper.py` reaches a broker through four `order_send` sites
(`mt5_paper.py:485, 526, 540, 644`) while importing nothing from `app/` -- no
RiskEngine, no OMS, no kill switch, no journal. It is still the only path that
has ever traded this account, so the weekly-loss, consecutive-loss and
correlation vetoes added at P4 did not see one of last night's 21 trades. Now
that the harness can finish a night, the thing it finishes outside of is the
next question.

~~**Build the ablation harness.**~~ **Built, and run once.**
`app/validation/ablation.py` with `backend/tests/test_ablation.py`, and
`reports/ablation_features.json` holds its first result: 8 feature components,
baseline expectancy +6.04 points clearing its null at a date-clustered t of
3.00, 2 of 8 components clearing 1.96 against 0.2 expected by chance --
`ABOVE_CHANCE_NOT_SIGNIFICANT`, since searching 8 candidates raises the bar any
one must clear to 2.734 and neither `rsi_14` nor `hour_utc` reaches it.

**What it has not yet been pointed at is the question it was written for.**
The docstring names the AI seat, the regime model and the anomaly detector; the
one report on file ablates features instead, and nothing outside the module and
its tests imports it. Running it over the three engines is a separate, small
job, and its null result -- if that is what it returns -- would still apply to
all twelve of L76's unbuilt engines.

Then L74 phases 1-2: provenance and the eight-kind taxonomy, followed by
prediction/outcome tracking -- the only component whose value grows with
wall-clock time, so every quarter it does not exist is calibration data
permanently lost.

**The ledger decision was taken and the merge was run.** The operator chose the
harness's own tag plus `--exclude` for the nine platform trades that carry it,
so the sample now means exactly "every trade this harness opened". 252 trades
were added, 2 excluded by tag and 9 by id, and the ledger stood at 504. *(The
2026-09-11 merge repeated those exclusions and took it to 751; see the section
above.)*

That merge immediately exposed the account defect above: the 504 are **two
different demo accounts** -- 252 at -22.37 from an earlier one and 252 at
+31.87 from the current one -- and the report had printed the sum, +9.50, as
one number. The recent 252 were attributed by reading the venue's own deal
history rather than by inferring from the ticket range; the older 252 stay
`unrecorded`, because that login is not knowable from here and inventing one
is the failure this repository exists to prevent.

Still open: slippage, partial fills and requotes remain unmeasured -- one
order, one fill, every time so far, which is not a sample of anything.
