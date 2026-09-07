# ARCHITECTURE.md

Master context for the platform this repository is migrating toward. This
file states the target, the invariants that hold at every level, and how the
existing `trade-research` code maps onto it. Per-component decisions are in
`ARCHITECTURE_MIGRATION.md`; progress is in `PROJECT_PROGRESS.md` and
`MIGRATION_STATUS.md`.

## 1. What is being built

An AI-assisted automated trading platform that integrates TradingView,
MetaTrader 5, market data, a strategy engine and visual builder, backtesting,
market replay, paper trading, risk management, position sizing, order
management, automated bots, position management, AI/ML with training,
validation and a registry, portfolio management, a trade journal, analytics,
AI trade review, notifications, Discord, admin, monitoring, recovery,
security and deployment.

It is built **on top of** the existing research toolkit, not in place of it.
The toolkit's measured results (no rule has cleared its null; the composite
score has no edge; cost is the only lever with a sign) are the platform's
starting knowledge, and its refusal mechanisms are the platform's safety
layer.

## 2. Target trading flow

```
TradingView
  → Webhook Gateway → Validation → Idempotency
  → Signal Engine
  → Strategy Engine
  → AI Layer
  → Risk Engine            (veto)
  → Position Sizing
  → Order Management
  → Broker Adapter
  → MT5 → Broker
  → Execution Confirmation
  → Position Manager
  → Trade Journal
  → Analytics
  → AI Monitoring
  → Model Training
```

Every stage is a module with an explicit input and output contract. The
Signal object is the contract from the gateway through the AI layer; the
OrderIntent is the contract from the risk engine through confirmation; the
journal row is the contract from confirmation onward.

## 3. Invariants

These hold at every level and are never relaxed to make a run work.

1. **Defaults:** `TRADING_MODE=paper`, `LIVE_TRADING=false`. Live trading is
   never enabled automatically, and is not enabled at all until every gate in
   §5 exists and is tested.
2. **The Risk Engine has veto power.** Nothing reaches the OMS without an
   approval object from it.
3. **The AI layer never bypasses the Risk Engine.** AI output can lower or
   withhold a signal's confidence; it cannot place, size or approve.
4. **TradingView never executes broker orders directly.** The gateway emits
   Signals and nothing else.
5. **Unknown execution status is never retried blindly.** Reconcile intent
   against broker orders and positions first; retry only if the intent is
   confirmed absent.
6. **MT5 disconnect:** stop new orders → alert → reconnect → reconcile →
   resume only if safe.
7. **Application restart:** reconcile broker state before any automated
   execution resumes.
8. **Nothing is faked:** no simulated broker execution presented as real, no
   fabricated market data, AI predictions, backtest results, API results or
   MT5 connection. Where credentials or a terminal are unavailable, a clearly
   labelled fake adapter is used and the label propagates into every record.
9. **Secrets** live in the environment or a secrets store, never in code,
   logs, frontend bundles or committed files.
10. **Bots run in backend workers.** No browser needs to stay open.
11. **The existing code-level demo fence (`assert_demo`) stays** as a second
    gate underneath the settings. A setting cannot override it.

## 4. Trading modes

The platform distinguishes three modes. The existing project used "paper" to
mean "the MT5 demo account", so the mapping is recorded here to avoid
ambiguity in every later level.

| Mode | Venue | Money | Fills | Existing code |
|---|---|---|---|---|
| **PAPER** | internal simulator (`FakeBroker`, replay engine) | none | simulated from bars, spread charged, ambiguous bar booked as loss | `rule_backtest.simulate()`, `tests/…FakeMT5` |
| **DEMO** | MT5 demo account at the real broker | broker's play money | real broker fills, real spread and swap | `mt5_paper.py --live` guarded by `assert_demo` |
| **LIVE** | MT5 real account | real | real | **does not exist and is refused in code** |

`TRADING_MODE` selects PAPER or DEMO. LIVE additionally requires
`LIVE_TRADING=true`, a real-account adapter that does not exist yet, and every
gate in §5. The journal records the mode on every row so the 252 existing
DEMO trades are never pooled with PAPER results.

## 5. Gates required before LIVE can be considered

Risk engine with veto; position sizing with refusal on missing data; kill
switch (file, setting and API); broker synchronisation on connect; order
idempotency; reconciliation on unknown status, disconnect and restart;
structured logging of every intent, order, fill and close; monitoring with
alerting. Each is a level in `MIGRATION_STATUS.md`; none is complete today.

## 6. Stack

| Layer | Target | Adaptation for this project |
|---|---|---|
| Frontend | Next.js, React, TypeScript, Tailwind, TanStack Query, WebSockets, trading charts | Nothing exists; built last, over the API |
| Backend | Python, FastAPI, Pydantic, SQLAlchemy, Alembic, asyncio | Existing tools are synchronous Python; they are wrapped, not rewritten. Async is used at the API and worker boundary |
| Database | PostgreSQL | JSONL ledgers stay the system of record until an Alembic migration imports them and reconciles to `reports/track_record.json` |
| Cache / events | Redis | Signal and heartbeat bus between API and workers |
| Workers | background worker framework (chosen at L20/L22) | The MT5 adapter worker must run on the Windows host with the terminal; everything else can run in containers |
| Infrastructure | Docker, Docker Compose, Nginx | MT5 is the one component that cannot be containerised on Linux |
| Testing | pytest, Vitest, React Testing Library, Playwright | 34 pytest tests exist and run in CI on Python 3.10/3.12/3.14 |

### 6.1 Foundation as built (L02)

- `backend/app/core/settings.py`: `Settings` from the environment.
  `TRADING_MODE`, `LIVE_TRADING`, `ENVIRONMENT` with fail-closed defaults;
  `LIVE_GATES` lists every safety gate and `live_execution_allowed` is true
  only when all are built. Live mode without the explicit flag refuses to
  start. A test asserts no gate is flipped at this level.
- `backend/app/core/logging.py`: one JSON line per record on stdout; `extra=`
  fields become top-level keys for `intent_id`, `signal_id`, `trading_mode`.
- `backend/app/core/health.py` + `main.py`: `GET /health` (mode, flags,
  blockers; no URLs) and `GET /health/ready` (database and Redis actually
  answered; 503 when not). Checks are injected so tests use labelled doubles
  and the real checks are proven honest against a closed port.
- `backend/app/db/`: async SQLAlchemy engine, declarative base with a naming
  convention, Alembic async environment reading the URL from settings. No
  models yet.
- Driver and addressing decisions, both measured on this machine: asyncpg
  (psycopg's async mode refuses uvicorn's Windows Proactor loop) and
  `127.0.0.1` rather than `localhost` (IPv6-first resolution hangs against
  Docker's IPv4 publish).
- `frontend/`: Next.js 16 scaffold, standalone output, Vitest + Testing
  Library, `src/lib/config.ts` as the only place the browser learns the API
  URL (`NEXT_PUBLIC_*` only).
- `docker-compose.yml`: postgres, redis, api, frontend, nginx on
  loopback-only host ports 5440 / 6390 / 8000 / 8080.

### 6.2 UI shell (L03)

Dark terminal shell in `frontend/src`: `lib/nav.ts` is the single list of
routes with the level that makes each real; `components/Unavailable.tsx` is
the one way to show an absent capability (level, reason, today's CLI, and a
real disabled fieldset around any controls); `components/ModeBadge.tsx` is
the one place PAPER / DEMO / LIVE is rendered and it shows MODE UNKNOWN when
the API is unreachable. Real data flows only through `hooks/useHealth.ts`
today; later levels add hooks per API and replace `Unavailable` notices
panel by panel, never by redesign.

### 6.3 Authentication (L04)

`backend/app/auth/`: models (`users`, `auth_sessions`), Argon2id passwords,
service layer, FastAPI dependencies (`current_user`, `require_role`), the
permission table, routers and the admin bootstrap. Sessions are database
rows so revocation is immediate; the browser holds only an opaque token in
an HttpOnly cookie. Roles: USER < TRADER < ADMIN. Protected resources that
do not exist yet are still routed and gated, answering 501, so authorization
is proven before any of them can act. The frontend mirrors the role table in
`lib/nav.ts` for navigation only; the API is the boundary.

### 6.4 Database (L05)

39 tables in two migrations. `backend/app/models/` holds them in nine
modules: accounts, market, strategies, signals, execution, research, risk,
bots, journal, ai, ops. Conventions that are load-bearing rather than
stylistic:

- **`mode` on every execution-side row** (`paper` | `demo` | `live`), so the
  simulator, the MT5 demo account and a real account are never pooled. The
  252 imported trades are all `demo`.
- **`Numeric`, never float**, for prices, quantities and money.
- **`orders.intent_id` unique** — the OMS idempotency key, so one intent can
  never become two orders.
- **`orders.status` includes `unknown`** for the IPC-timeout case. Its only
  legal exit is reconciliation, never a retry (invariant 5 in §3).
- **`symbols.point_size` and `unit_class`** exist from the start, because
  pooling points across symbols with different point sizes is the error this
  project keeps rediscovering.
- **`risk_events` records approvals as well as vetoes**, so the Risk Engine's
  decisions are auditable in both directions.

The JSONL ledgers remain the system of record. `app/db/import_ledgers.py`
copies them in idempotently and reconciles the result against
`reports/track_record.json`; a mismatch is a non-zero exit. The brief's
`sessions` table is `auth_sessions` from L04, kept under its own name.

### 6.5 Symbol mapping (L11)

Three names, none derivable from another:

| Name | Example | Where it lives |
|---|---|---|
| `source_symbol` | `XETR:DAX`, `OANDA:EURUSD` | `symbol_mappings` (provider `tradingview`) |
| `internal_symbol` | `DE40`, `EURUSD` | `symbols.code` |
| `broker_symbol` | `DE40`, `EURUSD.r` | `symbol_mappings` (provider `mt5`) |

`app/symbols/service.py` resolves between them by lookup only. A miss raises;
nothing falls back to the input string. A bare ticker may reach an
exchange-qualified mapping, but only when exactly one instrument matches —
two exchanges listing the same ticker raise rather than route an order to a
guess.

Contract specs (contract size, tick size and value, volume min/max/step,
price and volume precision, trading hours) hang off the **mapping**, because
they are the broker's terms and not the instrument's. Every one is nullable
so an unsynced mapping can exist, and every one is refused rather than
defaulted when a caller needs it: `contract_spec()` names the missing fields.
`spec_source` and `spec_updated_at` record provenance, so a spec read from a
terminal is never confused with one typed by hand.

Measured on this broker, and the reason the layer exists: `DE40` has contract
size 1 and minimum volume 0.1, while the FX pairs have 100,000 and 0.01. Tick
value ranges from 0.0116 (DE40) to 1.2301 (USDCHF). A flat 0.01 lot is a
valid order on EURUSD and an invalid one on DE40.

Trading hours are null on this MetaTrader5 build, which exposes no
per-weekday session API. Null means no schedule is known; nothing may read it
as an open market.

### 6.6 Position management (L21)

`app/positions/` splits into three pieces so the decision is testable apart
from the venue:

- **`policies.py`** is pure. Seven exit types evaluated in a fixed priority —
  emergency, risk, stop loss, take profit, trailing, time, strategy. Two
  rules are inherited rather than invented: a poll that spans both the stop
  and the target books the loss (the simulator's rule, for the same reason),
  and floating P&L means net of swap (`take_profit.net_floating`'s lesson).
  The trailing policy never closes anything; it moves the stop and lets the
  stop policy fire, so one place decides a stop exit.
- **`executor.py`** is the close contract: CONFIRMED, REJECTED, UNKNOWN. Only
  a confirmation carrying a fill price closes a position. UNKNOWN is the
  reason the module exists — an IPC timeout after a send is indistinguishable
  from a rejection, retrying can double-close, and calling it failed can
  leave a position unwatched. The paper executor fills at the exit side of
  the quote and labels every fill `simulator`; demo and live use an executor
  that cannot confirm anything until L10.
- **`manager.py` and `monitor.py`** apply the decision, write every stop move
  and close attempt to `position_events`, and sweep continuously in a backend
  worker. A position parked as `unknown` is never acted on again.

`position_events` uses an autoincrementing integer primary key so the log
replays in write order; a UUID orders nothing, and a decision and its fill
share the same `occurred_at` by construction.

### 6.7 Foundation infrastructure (L02, extended)

Verified and extended rather than rebuilt. The pieces added when the fuller
foundation checklist arrived:

- **`core/errors.py`** — one exception hierarchy, one response envelope
  (`code`, `detail`, `request_id`), and a request id on every request,
  response header and log line. An unhandled exception is logged in full and
  answered with the id alone; a validation failure reports field names and
  messages but never the submitted values, because a rejected login body
  carries a password.
- **`core/events.py`** — the event *transport*, not the trading event
  catalogue (that is L07). `RedisEventBus` and `InMemoryEventBus` behind one
  protocol, each labelled by `kind` so an in-process delivery is never
  mistaken for a distributed one. Events carry their own id and timestamp so
  a subscriber can deduplicate, and oversized or malformed messages are
  refused rather than truncated or guessed at.
- **`workers/base.py`** — the loop, the heartbeat, cooperative shutdown and a
  registry. `PositionMonitor` had its own copy of this loop from L21 and now
  uses this base, so one implementation exists.
- **Three-state health** — HEALTHY, DEGRADED, UNAVAILABLE, with criticality a
  property of each check. The system is never reported healthy while a
  critical dependency is down, and 503 is reserved for that case; a degraded
  optional dependency still answers 200. Redis is optional today and becomes
  critical at L07 when the bus carries trading events. `/health/live` touches
  no dependency at all.

## 7. Module map (existing → target)

| Target module | Existing source | Decision |
|---|---|---|
| `gateway/tradingview` | `tools/tv_webhook.py` | KEEP + MODIFY |
| `signals` | none (rules return strings) | ADD |
| `strategies` | `mt5_paper.RULES`, `rule_backtest.signals_*`, `rule_search.build_candidates` | KEEP + wrap |
| `ai` | Claude agents (research prose) | ADD interface, identity default |
| `risk` | `mt5_paper.assert_demo`, `cycle()` caps, `server_day_start` | REFACTOR out of the loop |
| `sizing` | `mt5_paper.lot_for_risk` | KEEP + MOVE |
| `oms` | `mt5_paper.place()` + `_log()` | KEEP construction, ADD state and idempotency |
| `broker` | `mt5_paper`, `mt5_account`, `rule_backtest.connect/fetch_rates`, `FakeMT5` | MERGE into `BrokerAdapter` |
| `execution/confirm` | `bracket_is_sane` + repair | KEEP + MOVE |
| `positions` | `take_profit.harvest`, `close_own` | KEEP + REFACTOR |
| `bots` | `take_profit.run`, `run_overnight.py` | REFACTOR into supervised runner |
| `journal` | `paper_trades.jsonl`, `track_record.jsonl`, `track_record.merge` | KEEP + MOVE |
| `analytics` | `trade_stats`, `track_record.significance`, `patterns` | KEEP |
| `backtest` | `rule_backtest`, `rule_search`, `exit_search`, `bracket_sweep`, cost tools, `swap`, `backtest.py` | KEEP untouched |
| `intelligence` | `snapshot`, `edgar`, `score`, `verify`, `market`, `indicators` | KEEP |
| `data` | `market.py`, `crypto_market.py`, MT5 history | KEEP + unify |

Full table with reasons: `ARCHITECTURE_MIGRATION.md`.

## 8. Known execution-safety gaps against §3

Recorded here so no level forgets them. Each is assigned in
`IMPLEMENTATION_PRIORITY.md`.

- `place()` treats a missing `order_send` result as REJECTED and logs it,
  which violates invariant 5: an IPC timeout after the server accepted the
  order looks identical to a rejection. Fix at L19 with reconciliation.
- A crash between `order_send` and `_log()` leaves an unlogged position;
  `track_record` already reports two such trades. Fix at L19 with intent
  persistence before send.
- No disconnect handling: `mt5.*` calls returning `None` mid-loop are
  handled per call, not as a state. Fix at L20/L38.
- No restart reconciliation; `NIGHTLY.md` relies on server-side SL/TP.
  Fix at L38.
- No kill switch other than Ctrl+C or `--max-daily-loss`. Fix at L17.
