# PROJECT_AUDIT.md

Full audit of `trade-research` as it stands on 2026-09-02, after Levels 00–05,
11, 21 and 29. This supersedes the Level 01 audit, which described the
repository before any platform code existed.

**On the prompt files.** `claude-prompts/00_MASTER_CONTEXT.md` and
`claude-prompts/01_AUDIT.md` are not present in this repository or anywhere on
this machine; only their contents, as pasted into the working session, are
available. They are recorded in `ARCHITECTURE.md` §1–§6 and this audit is
written against them. Drop the real files in and any level whose target
differs can be corrected.

**Nothing was modified during this audit.** Read-only inspection, plus the
three test suites, lint, type checks and a production build, all of which
pass.

---

## 1. Repository overview

Two systems in one repository, deliberately side by side rather than merged:

| | Research toolkit | Platform |
|---|---|---|
| Location | `tools/`, `tests/`, `.claude/` | `backend/`, `frontend/`, `nginx/` |
| Size | 24 modules, ~7,000 lines | ~8,100 lines Python, ~2,200 lines TypeScript |
| Age | 18 commits, Aug–Sep 2026 | this migration, uncommitted |
| Tests | 34, on Python 3.10/3.12/3.14 | 121 backend, 31 frontend |
| Status | working, untouched | Levels 00–05, 11, 21, 29 built |

The toolkit computes and refuses; the platform is being built around it. Its
central claim — Python computes, the model interprets, and no figure is ever
generated — is the reason the platform inherits refusals rather than defaults
everywhere.

**Working tree:** 16 untracked paths and 2 modified files. Every platform
directory and document is uncommitted. `tools/` and `tests/` are clean.

## 2. Technology stack

| Layer | Chosen | Notes |
|---|---|---|
| Backend | Python 3.12+, FastAPI 0.136, Pydantic 2.13, SQLAlchemy 2.0 async, Alembic 1.19 | matches the target stack |
| Database | PostgreSQL 16 | driver is **asyncpg**, not psycopg: psycopg's async mode refuses uvicorn's Windows Proactor loop, and the MT5 worker must run on Windows |
| Cache / events | Redis 7 | health-checked only; no event bus yet |
| Frontend | Next.js 16, React 19, TypeScript 5, Tailwind 4, TanStack Query 5, lightweight-charts 5 | matches the target stack |
| Testing | pytest 9, Vitest 4, Testing Library | Playwright not yet introduced |
| Lint / types | ruff 0.16, mypy 2.3, eslint, tsc | scoped to `backend/` and `frontend/` only |
| Infrastructure | Docker 29, Compose v5, Nginx 1.27 | one dev Compose file |
| Research | numpy 2.4, pandas 3.0, yfinance 1.6, MetaTrader5 5.0.6090, ccxt 4.5 | MetaTrader5 and ccxt optional |

## 3. Existing architecture

```
Research toolkit (unchanged, CLI)
  market/edgar/crypto -> indicators -> score -> snapshot -> agents -> verify
  rule_backtest.simulate -> rule_search / exit_search / bracket_sweep / cost_*
  mt5_paper (the only order sender) -> take_profit -> run_overnight
  mt5_account (read-only) -> track_record -> JSONL ledgers

Platform (new, beside it)
  frontend (Next.js) -> nginx -> api (FastAPI)
                                   |- auth + admin + protected stubs
                                   |- health / readiness
                                   |- symbols (resolution + specs)
                                   |- positions (exit policies, paper executor)
                                   |- monitoring (drift, escalation)
                                   `- SQLAlchemy -> PostgreSQL (39 tables)
```

The platform does not yet reach a broker. The toolkit still does, from the
command line, exactly as before.

## 4. Existing modules

### Research toolkit (`tools/`, 24 modules)

| Module | Role | Sends orders |
|---|---|---|
| `market.py` | yfinance OHLCV, profile, earnings, 7-day disk cache | no |
| `edgar.py` | SEC XBRL, period alignment, derived ratios | no |
| `indicators.py` | RSI, MACD, ATR, ADX, Bollinger, beta, swings, Fibonacci | no |
| `score.py` | composite with coverage; missing components drop out | no |
| `snapshot.py` | the JSON contract the agents consume | no |
| `verify.py` | every number in a note matched to the snapshot | no |
| `backtest.py` | point-in-time IC and quintile test of the score | no |
| `patterns.py` | 23 event studies, BH-FDR, date clustering, cycles | no |
| `trade_stats.py` | execution statistics shared by MT5 and TradingView | no |
| `tv_import.py` | TradingView CSV importer with column sniffing | no |
| `tv_webhook.py` | stdlib alert receiver; records, never trades | no |
| `mt5_account.py` | read-only account, positions, deal pairing | no |
| `mt5_paper.py` | **the only order sender**; demo fence, rules, sizing, fills | **yes** |
| `take_profit.py` | harvest loop; closes at floating profit | via mt5_paper |
| `run_overnight.py` | fixed-settings launcher to a wall-clock hour | via take_profit |
| `track_record.py` | idempotent ledger merge, R-multiples, clustered t | no |
| `rule_backtest.py` | bar-walk simulator, spread choice, MT5 history | no |
| `rule_search.py` | 41 candidates, permutation null, eras, walk-forward | no |
| `exit_search.py` | entry × exit grid including winner-letting exits | no |
| `bracket_sweep.py` | SL/TP grid with holdout and random null | no |
| `cost_hurdle.py`, `cost_profile.py` | breakeven win rate; spread by symbol/hour/timeframe | no |
| `swap.py` | financing in points; refuses unconvertible units | no |
| `crypto_market.py` | ccxt OHLCV in percent units | no |

### Platform backend (`backend/app/`)

| Package | Contents | Level |
|---|---|---|
| `core/` | settings with `LIVE_GATES`, JSON logging, health and readiness, Redis factory | 02 |
| `db/` | async engine, declarative base, JSONL importer with reconciliation | 02, 05 |
| `models/` | 39 tables across nine modules | 05 |
| `auth/` | Argon2id, server-side sessions, roles, permission table, bootstrap | 04 |
| `admin/` | user listing and role changes, ADMIN only | 04 |
| `api/protected.py` | five TRADER-gated stubs answering 501 with their level | 04 |
| `symbols/` | three-name resolution, broker contract specs, MT5 sync | 11 |
| `positions/` | seven exit policies, close contract, paper executor, monitor loop | 21 |
| `monitoring/` | eight drift and calibration checks, FDR, escalation ladder | 29 |

### Platform frontend (`frontend/src/`)

18 routes plus login and register, a dark terminal shell, and the terminal's
eleven panels. One `Unavailable` component is the single way an absent
capability is shown; one `ModeBadge` is the single place PAPER, DEMO or LIVE
is rendered. Real data flows only from `/health` and `/auth/me`.

## 5. Existing integrations

| Integration | State |
|---|---|
| **MetaTrader 5** | Working from the toolkit: connect, read, deal pairing, order send, SL/TP modify, close, fill and slippage recording, bracket repair, server-clock helpers. The platform reads contract specs from a live terminal (`symbols/sync_mt5.py`) and nothing else. No `BrokerAdapter` yet. |
| **TradingView** | Alert receiver with constant-time secret compare, redaction at any depth, IP allowlist, body cap, loopback bind. CSV importer with column detection that refuses when P&L is absent. Records only; no path to execution, by design. |
| **Market data** | yfinance with disk cache, SEC EDGAR, MT5 history, Binance via ccxt in percent units. No unified provider interface. |
| **SEC EDGAR** | Working, period-aligned, accession numbers attached. |
| **Redis** | Container runs and is health-checked. Nothing publishes or subscribes. |
| **Discord, Telegram, email** | None. |

## 6. Existing trading functionality

- **Order sending** exists only in `tools/mt5_paper.py`, fenced by
  `assert_demo` (trade_mode must be 0; real, contest and unknown all abort),
  dry-run by default, MAGIC-tagged, capped by positions and daily loss on the
  server day.
- **Sizing** by `lot_for_risk`, which refuses on missing tick data and rounds
  the step count before flooring (a float floor once dropped a whole step).
- **Fill handling**: `place()` records the fill and slippage, checks the
  bracket straddles the actual fill, and repairs or closes when it does not.
- **Exit management** now exists in the platform (`app/positions/`) with seven
  policies, but its only working executor is the paper simulator.
- **Live record**: 252 closed demo trades, +0.023R pooled, date-clustered
  t −0.80. Imported into PostgreSQL and reconciled exactly.

## 7. Existing AI functionality

**There are no machine-learning models, and none has ever existed here.** What
exists is five Claude Code sub-agents that interpret a snapshot and are
forbidden from producing numbers, plus a statistical validation harness that
is stronger than most model-validation code: permutation nulls, Bonferroni,
era blocks, walk-forward, date and symbol clustering, unit-mismatch fences.

Classification for the AI section, per the brief's KEEP / INTEGRATE / EXTEND /
RETRAIN / REPLACE:

| Asset | Decision | Reason |
|---|---|---|
| Five research agents | **KEEP** | Their prohibitions are the AI safety policy; they interpret and never emit numbers |
| Validation harness (`rule_search`, `exit_search`, `patterns`) | **KEEP + INTEGRATE** | It becomes the promotion gate any model must clear |
| `app/monitoring/` | **KEEP** | Built at L29; watches a model when one exists |
| Any ML model | **ADD, not yet** | Nothing to retrain, replace or discard. Retraining is not applicable because no model exists |

## 8. Existing database structure

39 tables in four migrations, zero autogenerate drift, round trip verified.

| Group | Tables |
|---|---|
| Identity | users, roles, auth_sessions |
| Accounts | broker_accounts, mt5_connections, paper_accounts |
| Market | symbols, symbol_mappings |
| Strategy | strategies, strategy_versions, strategy_parameters |
| Signals | webhook_events, signals |
| Execution | orders, order_events, executions, positions, position_events, trades |
| Research | backtests, backtest_trades, replay_sessions, paper_orders, paper_positions |
| Risk | risk_rules, risk_events |
| Bots | bots, bot_runs, bot_events |
| Journal | portfolio_snapshots, journal_entries, trade_tags |
| AI | models, model_versions, training_runs, model_predictions |
| Ops | notifications, audit_logs, system_events |

Load-bearing conventions: `mode` in {paper, demo, live} on every execution
row so venues are never pooled; `Numeric` never float; `orders.intent_id`
unique; `orders.status` includes `unknown` for the timeout case;
`symbols.point_size` and `unit_class` from the start.

**Populated today:** 252 trades, 269 orders, 485 order events, 208 executions,
9 symbols, 18 mappings. The JSONL ledgers remain the system of record and
were never modified.

## 9. Problems discovered

1. **Six levels of pipeline are missing between the signal and the broker.**
   The Risk Engine, Position Sizing module, OMS, Broker Adapter, Signal Engine
   and Webhook Gateway do not exist. The position manager built at L21 can
   therefore only act through a simulator.
2. **No workers and no event bus.** Redis runs and is health-checked; nothing
   uses it. There is no background worker framework, so the bot manager and
   the position monitor have no host to run in.
3. **No realtime channel.** No WebSocket server or client.
4. **`tools/take_profit.py` and `run_overnight.py` still orphan.** Stopping a
   backgrounded loop can leave a child sending orders; documented in
   `NIGHTLY.md`, not fixed.
5. **`place()` treats an unknown send as REJECTED.** Safe for an open, wrong
   in general: an IPC timeout after the server accepted is indistinguishable
   from a rejection. The close side was fixed at L21; the open side waits for
   L19.
6. **Trading hours unavailable.** MetaTrader5 build 5.0.6090 exposes no
   per-weekday session API. Stored as null, which means "no schedule known"
   and must never be read as an open market.
7. **Frontend has no mobile navigation.** The sidebar hides below `md` with no
   drawer.
8. **Nothing is committed.** All platform work sits in the working tree.

## 10. Duplicates discovered

| Concept | Locations | Verdict |
|---|---|---|
| MT5 `connect()` | `mt5_paper`, `mt5_account`, `rule_backtest`, `symbols/sync_mt5` | **MERGE** into the broker adapter at L10. Four copies is the worst duplication in the repository |
| ATR | `indicators.atr`, `mt5_paper.atr_from`, `rule_backtest.atr_series` | **KEEP** for now: different array shapes, and one is parity-tested against the live rule |
| Live rules vs vectorised signals | `mt5_paper.rule_*` vs `rule_backtest.signals_*` | **KEEP**: the duplication is deliberate and a test asserts they agree bar for bar |
| BH-FDR | `patterns.py`, `monitoring/stats.py` | **KEEP, locked**: the backend cannot import the toolkit's dependencies, so a test asserts the two agree over 50 random inputs |
| `net_floating` | `take_profit.py`, restated as a rule in `positions/policies.py` | **KEEP**: the platform copy is a documented inheritance, not a second implementation |
| `simulate` | `rule_backtest`, `exit_search.simulate_exit` | **KEEP**: different exit structures, shared entry semantics |
| JSONL loaders | `track_record`, `tv_webhook`, `db/import_ledgers` | **MERGE** at L31 |

No accidental duplicates were found in the platform code.

## 11. Security issues

**Scan result: no hard-coded credential found** in `backend/app`,
`frontend/src` or `tools`. No `.env` exists or is committed. `data/` and
`reports/` are gitignored because account details live there.

| Control | State |
|---|---|
| Password hashing | Argon2id, 10-character floor, unknown emails burn a real verification |
| Sessions | server-side rows, opaque token, SHA-256 stored, 12 h TTL, HttpOnly, SameSite=Lax, Secure in production |
| Roles | USER < TRADER < ADMIN, one table, mirrored in the frontend for navigation only |
| Real-account refusal | in code, unaffected by any setting |
| Webhook | body secret, constant-time compare, redaction at any depth, IP allowlist, 64 KB cap, loopback bind |

Open risks, all recorded rather than hidden:

1. **No rate limiting** on login or the webhook.
2. **No CSRF token.** SameSite=Lax plus JSON bodies is the current posture; a
   double-submit token is needed before state-changing trading routes exist.
3. **No webhook replay protection.** A captured alert can be resent.
4. **Weak-auth fallback** accepts a plain-text alert containing the secret
   anywhere. Fine for a log, not for anything that reaches execution.
5. **No password reset or email verification.**
6. **No secrets manager.** Environment variables only.
7. **`urllib3-future`** in global site-packages shadows `urllib3`; the
   documented fix (a virtualenv) is not in use on this machine.

## 12. Trading safety issues

| Requirement | State |
|---|---|
| `TRADING_MODE=paper`, `LIVE_TRADING=false` by default | **met**, tested |
| Live never enabled automatically | **met**: ten gates, all false, listed on `/health`; live mode without the flag refuses to start |
| Real accounts refused in code | **met**: `assert_demo`, no setting overrides it |
| Risk Engine veto | **NOT MET** — the engine does not exist (L17) |
| AI cannot bypass risk | vacuously true; no AI layer exists (L24, L27) |
| TradingView never reaches the broker | **met** by absence, and by design in the receiver |
| Reconcile before retry | **partly**: the close path enforces it (L21); the open path does not (L19) |
| MT5 disconnect stops new orders | **NOT MET** (L10, L38) |
| Restart reconciliation | **NOT MET** (L38) |
| Kill switch | **NOT MET** (L17) |
| Order idempotency | column exists and is unique; **no OMS uses it** (L19) |
| Backtest and replay leakage prevention | **met** in the simulator: entry on the next bar's open, loss booked on an ambiguous bar |
| Monitoring cannot replace a model | **met**, tested |

The single most important line: **the platform cannot place an order at all
today.** Every unmet item above is unmet in a system with no execution path,
which is the safe direction.

## 13. Recommended migration approach

Adapter, then refactor, then migrate. Concretely:

1. Build the **BrokerAdapter** interface and move the four `connect()` copies
   behind it, promoting the existing test double to the paper broker. This
   unblocks L16, L19, L20, L21's demo path and L38.
2. Build the **Risk Engine** and **kill switch** before the OMS, so no order
   path can exist that predates a veto.
3. Build the **OMS** with the idempotency key the schema already carries, and
   fix the open-side unknown-status handling there.
4. Only then wire the **Signal Engine** and **Webhook Gateway**, so
   TradingView's first path to execution already passes through a veto.
5. AI last, and only after a dataset and a stated hypothesis exist. The
   validation harness is the gate, not an afterthought.

Nothing in `tools/` is deleted at any step. Modules move behind interfaces
and keep working from the command line.

---

## 14. TradingView autonomy audit, 2026-09-03

A second read-only audit, against the autonomous-builder brief rather than the
level ladder. Nothing was modified. All three suites were run first: research
**34 passed**, backend **900 passed / 3 skipped**, frontend **77 passed**.

### 14.1 What already exists

Of the twenty capabilities the brief names, sixteen are built. The chain from a
strategy signal to a paper position is complete and the safety properties the
brief asks for are enforced by types rather than by discipline: the OMS takes a
`risk.Approval` as its first argument and `Approval` has no constructor outside
`RiskEngine.approve`, so "nothing bypasses risk" is not a check someone has to
remember; `provider_for(mode)` takes the mode and nothing else, so
configuration cannot reach the execution route; `Candles.closed` drops the
forming bar before a strategy sees it, so no-look-ahead is structural.

TradingView specifically has three components already: the L09 server gateway
(secret, redaction, age check, idempotency, symbol resolution,
`SIGNAL_CREATED`), `tools/tv_webhook.py` as the standalone operator receiver,
and `tools/tv_import.py` for Strategy Tester CSV exports. None is a duplicate
of another and none is replaced.

### 14.2 The forbidden shape is absent, verified

Brief §33 asks whether a direct TradingView → MT5 path exists to refactor. It
does not, and this was checked by import graph rather than by reading
docstrings: no module in `app/paper`, `app/replay`, `app/backtest` or
`app/strategies` imports `app.brokers` or `MetaTrader5`; `MetaTrader5` is
imported lazily in `app/marketdata/providers/mt5.py` and
`app/symbols/sync_mt5.py` besides the adapter itself; and a test already
asserts the L09 gateway imports no strategy, risk, sizing or OMS module.

### 14.3 The gap that matters

**A saved strategy definition cannot be run.** `app/backtest/service.py:180`,
`app/replay/service.py:137` and `app/paper/service.py:351` all resolve a
strategy through `StrategyRegistry.create(key, config)`, which looks a key up
in a dict of classes registered at import time. A `BuiltStrategy` is an
instance constructed from a definition, and outside its own module it is
referenced in exactly one place — `app/api/v1/strategy_builder.py:527`, for a
preview.

So the visual builder that shipped at L13 can save a validated strategy that
nothing can backtest, and a TradingView compiler would emit output nothing
could run. It is scheduled as **A3** and placed ahead of the compiler.

### 14.4 What is genuinely missing

Pine parsing (nothing in the repository parses Pine), the compiler, the
orchestrator, the build-time LLM interpreter, per-strategy test generation, the
validation harness that wraps L14/L15/L26 into one report, Sharpe and Sortino,
and the machine-readable project state. Plus one blocked item: the alert →
execution bridge, which needs L18 and L19 first.

### 14.5 What the audit did not find

No unsafe component, no broken component, and no duplicate of any brief §9
item — no second MT5 connector, risk engine, sizing engine, OMS, strategy
engine, database model, webhook handler, auth system or market-data provider.
Nothing is marked REPLACE or REMOVE anywhere in the resulting plan.

---

## 15. Position sizing, audited at L18 (2026-09-03)

### 15.1 What existed

`app/sizing/calculator.py`: three modes, ~200 lines, one consumer
(`app/paper/engine.py` since L16), **no tests of its own**. Everything around
it was already in the right place — `app/risk/` computes no quantity,
`app/paper/oms.py` requires a risk `Approval` as its first positional
argument, `app/brokers/` is the only path to a venue, and `ContractSpec` is
the single source of broker contract terms. Brief §12's "extract sizing from
the Risk Engine" was therefore not applicable: there was nothing to extract.

### 15.2 The duplication the audit found

Two, both small and both real:

1. **Flooring.** `calculator._round_to_step` beside
   `app/symbols/precision.floor_to_step`, whose own docstring said position
   sizing "will do the same". Two functions that round a lot differently is
   how one signal sends two volumes depending on which path reached the venue.
2. **Money per price unit.** `calculator` computed
   `stop / tick_size * tick_value` inline while `value_per_price_unit` in
   `app/paper/portfolio.py` was the canonical converter used by the portfolio
   and the paper service.

Both merged. `value_per_price_unit` moved to `app/symbols/precision.py` — the
leaf module both layers may import — and is re-exported from its old home so
no existing import changed.

### 15.3 The two defects

Recorded in full in `LEVEL_18_POSITION_SIZING.md` §2. In short: a quantity
below the venue minimum was **raised to** the minimum, which silently risks
more than the configured budget and contradicted
`precision.normalize_quantity`, which already refused that exact case; and
stop direction was never validated, because the engine only ever saw
`abs(entry - stop)`.

These are the only two REPLACE decisions in the level. Everything else is
KEEP, KEEP + MODIFY, MERGE or ADD.

### 15.4 What the L18 audit did not find

No second sizing engine, no second risk engine, no second OMS, no second
symbol-metadata system, no MT5 call inside sizing, and no frontend that
computed a quantity of its own. Nothing was marked REMOVE.

---

## 16. The OMS, audited at L19 (2026-09-03)

### 16.1 What existed

More than the brief assumes, and that is the audit's most useful finding.
`app/paper/oms.py` was a complete order lifecycle: an `Approval` required as
the first positional argument of `submit`, `intent_id` idempotency backed by a
unique constraint, a transition table, and `unknown` as a real state that
nothing retried out of. `app/brokers/` had the adapter, the MT5
implementation, a fault-injecting fake and a three-state `OrderResult`.
`app/brokers/reconcile.py` reported mismatches and repaired none.
`app/models/execution.py` had the three tables. The L07 catalogue had already
declared eleven `ORDER_*` event types and named L19 as their producer.

Nothing was broken, nothing was unsafe, and nothing needed replacing except a
501 handler that said the OMS did not exist.

### 16.2 The four gaps

1. **The state machine was reachable only by the paper venue.** Correct, and
   private. Extracted to `app/oms/state.py`, re-exported from its old home.
2. **Four real outcomes had no state.** `submitting`, `cancel_requested`,
   `expired`, `failed`. The last is the important one: it is deliberately not
   a synonym for `rejected`, because "the venue never saw it" and "the venue
   refused" differ in whether a re-send is safe.
3. **No partial-fill accounting.** A single `fill` field, so requested and
   filled were the same number by construction.
4. **Ten declared events with no producer**, and an audit trail that could not
   be ordered — four transitions inside one clock reading share a timestamp,
   and the primary key is a uuid.

### 16.3 What the L19 audit did not find

No second OMS, order model, broker adapter, execution service, position
manager, reconciliation system or event bus. No MT5 call outside
`app/brokers/`. No path from a client, an alert or the AI seat to a venue that
skips the Risk Engine. Nothing was marked REMOVE.

---

## 17. Automated execution, audited at L20 (2026-09-03)

### 17.1 What existed

Almost all of it. The webhook gateway records a `Signal` with a unique key, an
auth strength and a status vocabulary. The strategy engine produces signals of
its own. The AI seat, the Risk Engine, position sizing, the OMS, the broker
adapter, the position manager and the supervised worker base all exist and are
tested. `app/paper/engine.py` is already a complete pipeline for one of the two
signal sources, and `app/paper/service.py` already runs it in background tasks
that survive a closed browser.

The legacy loop the L20 row named -- `tools/take_profit.run`,
`tools/run_overnight.py` and the `.bat` launcher -- is the research toolkit's
own demo loop. It is untouched: it is not the platform's execution path, and
rewriting it would change the tool every figure in `CLAUDE.md` was measured
with.

### 17.2 The one gap

**`SIGNAL_CREATED` had no consumer.** Two modules published it and both said so
in their own docstrings; `app/api/v1/strategies.py` said it in a route
description. A TradingView alert became a row and stopped there.

That is the gap `IMPLEMENTATION_PRIORITY.md` recorded as A8, blocked on the
order state machine. L19 built the machine, so it was buildable.

### 17.3 What the L20 audit did not find

No second execution service, signal processor, trade executor, order executor,
bot runner, scheduler, queue or worker system. No path from an alert, the AI
seat or a client to a venue that skips the Risk Engine. Nothing was marked
REPLACE or REMOVE.

### 17.4 The decision that shaped the level

`PaperEngine` could have been widened to accept an external signal. It was not,
because the two pipelines answer different questions -- one produces a signal
from bars, the other consumes a signal produced elsewhere -- and a class that
did both would be two jobs wearing one name. What they share is every gate, and
those are called, not copied. The one thing that WAS shared by extraction is
the `Outcome` vocabulary, because counters that cannot be added together are
counters nobody can build a dashboard on.


---

## 18. Position management, audited at L21 (2026-09-04)

### 18.1 What existed

The paper side, complete and correct. `app/positions/policies.py` holds seven
exit policies with an explicit `PRIORITY` order, a trailing stop that only
ratchets, and pure view types so a policy can be fed from the database, a
broker read or a replay bar. `app/positions/manager.py` evaluates, acts and
records, and its close contract already refused to mark a position closed
without a confirmed fill. `app/positions/monitor.py` is a supervised worker.
`app/brokers/reconcile.py` compares the two views and repairs nothing.

`MIGRATION_STATUS.md` recorded exactly one gap -- "demo/live executor, arrives
with L10" -- and the audit confirms it was accurate.

### 18.2 The gap

**Demo and live positions could not be closed at all.**
`UnavailableExitExecutor` returned REJECTED with "no broker adapter is built
yet (level 10)" for every attempt. L10 built the adapter and L19 built the
OMS, so the executor that uses them was buildable for the first time.

### 18.3 What else the audit surfaced

Four things the paper implementation did not need and a real venue does:

1. **No partial exits.** `quantity` was the only size field, so a scale-out
   would have overwritten what the position opened at.
2. **Three states.** `opening`, `partially_closed`, `closing` and
   `reconciling` all had to be recorded as something they were not.
3. **No record of what the VENUE holds.** `stop_loss` meant both "what we
   intend" and "what is in force", which are the same only until they are not
   -- and the case where they differ is a position running unprotected while
   the record says otherwise.
4. **No break-even policy**, though the research toolkit has measured the
   move-to-breakeven exit extensively (and found it worse than a fixed
   bracket, which is recorded with the policy).

### 18.4 What the L21 audit did not find

No second position manager, tracker, exit manager, P&L engine, reconciliation
service or fill processor. No MT5 call outside `app/brokers/`. No path from a
policy, the AI seat or a client to a venue that skips the executor. Nothing
was marked REMOVE -- `UnavailableExitExecutor` is kept, because it is still
the right answer for a mode with no adapter and deleting it would remove the
honest refusal that made the gap visible in the first place.

---

## 19. Bot management, audited at L22 (2026-09-04)

### 19.1 What existed

More than the status table said. `MIGRATION_STATUS.md` recorded L22 as **NOT
STARTED** with "one loop at a time, manual launch", and both halves of that
were wrong.

`bots`, `bot_runs` and `bot_events` have existed since L05, and `bot_runs`
already carried `pid`, `host`, `heartbeat_at`, `stop_reason` and a run summary
— the columns a supervisor needs, waiting for something to read them.
`PaperService` has run a full lifecycle since L16: `start_bot` creates a
`RunningBot` with a **frozen** config and launches an `asyncio` task,
`pause_bot`/`resume_bot`/`stop_bot` drive it, and the task is owned by the
backend rather than a browser tab. `app/workers/base.py` (L02) provides
`Worker` with a heartbeat, a staleness rule and cooperative shutdown, plus a
`WorkerRegistry`; `ExecutionWorker` (L20) and `PositionMonitor` (L21) are
already built on it.

So "bots must survive a closed browser" — the reason this item was HIGH — was
already true. What was missing was not a runner. It was something that
**checks**.

### 19.2 The gap

**Nothing measured whether a bot was actually running.** Every reader of a bot's
state read `bot_runs.status`, and a status column says what the last process to
touch it believed. A process that dies mid-run touches nothing, so every crash
leaves a row reading `running` — the one case where the column is both wrong
and reassuring. `heartbeat_at` was written and never compared to anything.

### 19.3 The defect the audit found

`PaperService.pause_bot` set the in-memory status to `paused` and wrote
**`stopping`** to the durable row, because `BOT_RUN_STATUSES` had no `paused`:

```python
bot.status = "paused"
await self._set_run_status(bot, "stopping", "paused by the user")
```

The in-memory value was right and the persisted one was wrong, which is the
worse of the two arrangements — it looks correct for exactly as long as the
process lives, and is wrong from the moment that stops being true. A paused bot
and a bot shutting down need **opposite** treatment after a restart: one should
be preserved as paused, the other is an orphan. One value for both makes that
decision unmakeable, and nothing reports the ambiguity, because from the row's
point of view there isn't one.

### 19.4 The latent bug the work exposed

Importing `app.paper.service` before anything else raised:

```
ImportError: cannot import name 'AiFilter' from partially initialized
module 'app.paper.engine'
```

`paper.engine` → `app.execution.outcome` → `app/execution/__init__.py` →
`pipeline` → back into a partially initialized `paper.engine`. A genuine cycle,
introduced at L20 and invisible only because the test suite's import order never
hit it — the kind of defect that surfaces first in production, where import
order is decided by whichever entry point runs.

Fixed by moving `AiVerdict` and `AiFilter` to `app/execution/ai.py` and
re-exporting them, not by reordering imports. Reordering would have hidden it.

### 19.5 What the L22 audit did not find

No second bot manager, bot runner, scheduler, process supervisor, worker
framework, queue or event bus. No path from `app/bots/` to the Risk Engine, the
sizing calculator, the OMS or a broker adapter — none of them is imported, and a
test parses every module in the package to keep it that way. Nothing was marked
REPLACE.

One REMOVE: `frontend/src/components/BotsTable.tsx`, a second table over the
same rows added earlier in this level's own work and folded into the existing
`BotStatus` before the level closed. A health rule rendered in two places is a
health rule that will eventually disagree with itself.

### 19.6 The decision that shaped the level

The brief names eight states including `ERROR`. This project has always called
that `crashed`, and `halted` is its word for "a kill switch stopped it" — which
is neither a crash nor a stop. Renaming existing values to match a document
would rewrite what every historical row means, so the vocabulary was extended
rather than replaced: nine states, three of them new (`paused`, `recovering`,
`disabled`), and `created` deliberately not among them because a `bot_runs` row
exists only when a run was attempted.

---

## 20. The data pipeline, audited at L23 (2026-09-04)

### 20.1 What existed

The audit's headline result is how little of this level needed building. Four
of the pieces the brief asks for were already here and already right.

**`app/marketdata/` is the canonical representation.** `Bar`, `Quote`, `Series`
and `Availability` (L08) already normalise every source into one shape with
per-field availability, which is what §5 asks for. `validation.py` already
counts invalid OHLC, duplicates, out-of-order bars, gaps, future-stamped bars
and the open-equals-close rate — and **flags rather than repairs**, which is
§9's rule stated as a design.

**`market_bars` is the raw layer §10 requires.** Keyed
`(provider, provider_symbol, timeframe, bar_time)` so ingestion is idempotent,
with database CHECKs for OHLC consistency and positive prices, and every
optional field NULL rather than defaulted — because a recorded spread of 0 is an
unrecorded spread, and `cost_profile.py` measured that averaging those zeros in
halves the apparent cost of trading.

**`app/strategies/indicators.py` is the one indicator engine.** SMA, EMA, RSI
and ATR, each with a declared `Unit`, bounded parameters and a warm-up, calling
`tools/rule_backtest.py` and `tools/rule_search.py` rather than reimplementing
them. §14 says do not create a second one, and the feature engine does not.

**`app/backtest/runner.build_signal_vector` is the causality discipline.** At
index i the strategy receives `bars[:i+1]` and nothing more. L23 extends that to
features and then checks it, rather than inventing a new convention.

### 20.2 The six gaps

Each is a real absence rather than something to rewrite: no feature engine, no
label engine, no dataset identity (manifest, version, fingerprint), no leakage
detection, no chronological split or walk-forward outside `rule_search.py`, and
no OHLCV resampling.

### 20.3 What the L23 audit did not find

No second `MarketDataService`, `DataPipeline`, `FeatureEngine`,
`DatasetBuilder`, `DataLoader`, `IndicatorEngine`, `LabelGenerator`,
`HistoricalDataService`, ML preprocessing system or storage system. No existing
AI model — `models`, `model_versions`, `training_runs` and `model_predictions`
have existed since L05 and hold nothing, so §47's "do not delete existing
models" has nothing to protect and nothing was touched. No notebook, no
`sklearn`, no `torch`, no `xgboost` anywhere in the repository.

Nothing was marked REPLACE or REMOVE.

### 20.4 The decision that shaped the level

**Every feature is dimensionless, and there is no raw price level in the
catalogue.**

This is the repository's own hardest-won lesson applied to machine learning.
`CLAUDE.md` records that pooling "points" across symbols whose median H1 ATR
runs from 160 (silver) to 9,386 (palladium) produced a +4,236 out-of-sample
headline that was an arithmetic error, and `rule_search.py` now raises a data
gap above a 5x spread. A model trained on `sma_20` as a price level learns the
price of the instrument. So a moving average appears only as a distance from it,
volatility only as a fraction of price, and the raw ATR — the unit every bracket
in this project is quoted in — is deliberately not offered as a feature at all.

### 20.5 What is verified and what is not

The pipeline is verified against a seeded synthetic series. It has **not** been
run against real broker history, because `market_bars` covers one instrument:
MT5 is not connected and no ingestion has run. "The pipeline is correct" and
"the datasets it produces from this broker's history are sound" are different
claims, and only the first is supported today.

---

## 21. AI models, audited at L24 (2026-09-04)

### 21.1 What existed

**Nothing.** No AI model exists anywhere in this repository — not a stub, not a
serialised artifact, not a notebook. A search across `backend/`, `tools/` and
`tests/` for `sklearn`, `scikit-learn`, `torch`, `tensorflow`, `xgboost`,
`lightgbm`, `catboost` and `keras` returns three hits, all of them in tests
asserting that a package does **not** import them. There is no `ai/`, `ml/`,
`training/`, `inference/`, `features/`, `experiments/` or `notebooks/`
directory, and no `.ipynb` file.

So the brief's §4 (existing model preservation), §35 (per-model KEEP/MODIFY/
REFACTOR/RETRAIN/REPLACE) and §47 ("if AI models already exist, do not delete
them") have nothing to act on. Nothing was deleted, retrained or replaced,
because there was nothing to delete, retrain or replace. Saying that plainly is
more useful than dressing it up as a migration.

What does exist and is reused: the `models` / `model_versions` /
`training_runs` / `model_predictions` tables (L05, empty), the `AiVerdict` /
`AiFilter` seat (L16, moved at L22), `app/datasets/` (L23), `app/monitoring/`'s
PSI, KS, Brier and calibration machinery (L29), and `tools/rule_search.py`'s
validation methodology (L26).

### 21.2 The defect the audit found, which is not an AI defect

**The backend Docker image cannot run the strategy engine, the backtester, the
replay engine or L23's feature engine.**

`backend/Dockerfile` runs `pip install -r requirements.txt` and copies `app`,
`alembic`, `alembic.ini` and `pyproject.toml`. Two problems:

1. **numpy was never declared** in `backend/requirements.txt`, and
   `app/strategies/indicators.py`, `app/strategies/rules.py`,
   `app/backtest/runner.py` and `app/replay/engine.py` have imported it since
   L12. It works on a developer machine only because the research toolkit's own
   root `requirements.txt` is installed globally.
2. **`tools/` is never copied.** `indicators.py::_toolkit` resolves the toolkit
   as a sibling of the backend directory — `/srv/tools` in the image — and
   imports `rule_backtest` and `rule_search` from it. That directory does not
   exist in the image, and `docker-compose.yml` mounts nothing to supply it.

Nothing catches this because the test suite runs outside Docker, and the
container's health check only touches `/health`, which imports none of it.

Found because §3 asks what the project already supports, and the honest answer
turned out to be "less than it appears to". Half fixed at this level —
`numpy>=1.24` is now declared, which is unambiguously correct either way. The
other half is a build-context change and belongs to L41; it is recorded there,
in `PROJECT_STATE.json` under `deployment_defect`, and in
`LEVEL_24_AI_MODELS.md` §2.

### 21.3 The decision that shaped the level

**No scikit-learn.** §17 asks for strong classical baselines and says to use
what the project already supports, and four things compound against adding it:
the backend does not even declare numpy (§21.2); the three models this level
needs are a few dozen lines of standard library each; a logistic model's
coefficients *are* the feature importance §23 asks for, which is the one model
class where "this feature influenced the answer" is provably true rather than a
surrogate; and determinism (§24) is easier to guarantee without a framework's
defaults.

When L25 needs gradient boosting it should be added, with a stated reason and
after the packaging gap is closed. That is a different decision from taking it
now because it is conventional.

### 21.4 What the L24 audit did not find

No second model registry (the L05 tables are reused and extended; L28 expands
them), no second prediction store, no second calibration or drift
implementation (L29's is called), no path from `app/ai/` to the OMS, the risk
engine, the sizing calculator, a position or a broker adapter — asserted by
parsing every module — and no import of `app.execution` from `app.ai`, which is
the L22 cycle not being repeated.

Nothing was marked REPLACE or REMOVE.

---

## 22. AI training, audited at L25 (2026-09-04)

### 22.1 What existed

**No training code of any kind.** A search for `train_model`, `ml_pipeline`,
hyperparameter tooling, Celery, RQ, Optuna and checkpointing across `backend/`
and `tools/` returns nothing. There is no notebook, no experiment directory and
no saved model.

`training_runs` has existed since L05 and was **created with the schema and
never used** — imported only by `app/models/__init__.py`. Its `model_version_id`
was NOT NULL, which is the shape that assumes a run already has a model; a
queued job does not, and could therefore not have been recorded at all.

What does exist and is reused:

| Component | Verdict |
|---|---|
| `BacktestService` / `ReplayService` background-task-plus-semaphore pattern (L14, L15) | **KEEP**, and copied in shape rather than replaced. §4 asks to reuse the existing job system |
| `training_runs` (L05) | **KEEP + MODIFY** — nine columns and two statuses added, one nullability relaxed |
| `app/datasets/` (L23) | **KEEP** — the dataset, the split, the scaler and the leakage verdict all come from it |
| `app/ai/` (L24) | **KEEP** — a run produces one of the three existing model classes, so the artifact is immediately usable by inference |
| `app/monitoring/stats.py` (L29) | **KEEP** — Brier, reliability bins and expected calibration error are called, not reimplemented |
| L07 realtime hub | **KEEP** — training events publish through it; §30 forbids a second realtime system |

### 22.2 The two defects L25's own tests found

**The fit starved the event loop.** `fit_weighted_logistic` is a synchronous
CPU-bound loop, and running it inside a background `asyncio` task is *not*
enough: an await-less loop in a coroutine blocks every other request in the
process for as long as it runs. §4 says training must not run inside an HTTP
request, and running it on the same thread as every HTTP request is the same
problem wearing a different hat. Now `asyncio.to_thread`.

**The cancel flag was looked up by job id.** `should_stop` did
`self._running.get(job_id).cancelled` — but the task's done-callback pops that
entry, so a cancelled task's worker thread found no handle, read "not
cancelled", and ran the fit to completion. The job was recorded as cancelled
while its thread kept burning CPU. The flag is now an object the job owns and
the thread closes over.

Both were found because the suite became flaky, not because a test targeted
them. Worth recording as an argument for running the whole file rather than the
one test that changed.

### 22.3 What the L25 audit did not find

No second queue, scheduler, worker system, dataset builder, scaler, split
implementation, metric implementation or model registry. No path from
`app/training/` to `app.execution`, `app.risk`, `app.sizing`, `app.oms`,
`app.brokers`, `app.positions` or `app.strategies` — asserted by parsing every
module — and no occurrence of the string `"promoted"` anywhere in the package.

Nothing was marked REPLACE or REMOVE.

### 22.4 The decision that shaped the level

**No hyperparameter search**, and the reason is this repository's own strongest
measured result rather than scope. `reports/bracket_sweep.json` records a
36-cell sweep in which the **random** rule scored an in-sample t of 1.76 while
the best real candidate reached 0.83, and `reports/rule_search.json` records 41
candidates whose best out-of-sample result went negative. A search is a machine
for producing winners that do not survive; building one before L26's correction
machinery exists would be building exactly the trap those numbers describe.

It belongs after validation, with the number of trials recorded and corrected
for. Stated in `AI_TRAINING_ARCHITECTURE.md` §11 rather than left as an
omission somebody later reads as an oversight.


---

## 24. AI validation, audited at L26 (2026-09-04)

### 24.1 What existed — more than at any previous level

L26 is the level with the most prior art in this repository, because the
methodology predates the platform. `MIGRATION_STATUS.md` had recorded L26 as
"COMPLETE (methodology)" since the first audit.

| Component | Where | Verdict |
|---|---|---|
| Permutation null, era blocks, walk-forward, date/symbol clustering, `t_crit_95`, `block_edges` | `tools/rule_search.py` | **KEEP** — called through `_toolkit()`, never copied |
| `simulate()`, `stats()`, `atr_series()` | `tools/rule_backtest.py` | **KEEP** — the only simulator, and the one every measured figure in `CLAUDE.md` came from |
| Brier, reliability bins, ECE, Benjamini-Hochberg, Welch's t, KS, PSI | `app/monitoring/stats.py` (L29 prep) | **KEEP** — imported |
| Six leakage checks, chronological split, scaler | `app/datasets/` (L23) | **KEEP** — the verdict is READ, not recomputed |
| `classification()`, `calibration()`, `economic()`, `compare()` | `app/training/metrics.py` (L25) | **KEEP** — read as the training record. `compare()`'s own docstring already said "this is NOT the L26 gate" |
| Background job: queue, semaphore, cooperative cancel, `_Cancellation` | `BacktestService`, `ReplayService`, `TrainingService` | **KEEP** — this is the fourth instance of one shape, deliberately not a fifth pattern |
| L07 realtime hub | **KEEP** — validation events publish through it |
| Reading a stored artifact back into a model | *(absent)* | **ADD** — `app/ai/loader.py` |
| The checks, the report, the verdict rule, the job | *(absent)* | **ADD** — `app/validation/` |
| `(bars, dataset)` from one read of the raw layer | *(absent)* | **ADD** — `build_validation_loader` |

Nothing was marked REPLACE or REMOVE, and nothing was deleted.

### 24.2 The gap the audit found in L24's own work

**`artifact_of()` had never been read back.** L24 wrote a fitted model as JSON
and nothing anywhere deserialised it — training holds its model in memory and
registers it in the same call, so the round trip was never exercised.
Validation is the first consumer that starts from a database row.

`app/ai/loader.py` closes it, and lives in `app.ai` rather than
`app.validation` because deserialising an L24 model is an `app.ai` concern; a
copy in the validation package would be a second answer to "what is
trade_probability v1.2" the moment either changed. L28's promotion will want the
same function.

It refuses rather than approximating: a missing coefficient does not become
zero and a missing feature list does not become the default set, because either
would produce a model that predicts confidently about nothing.

### 24.3 The decision that shaped the level

**There is no score**, and it is enforced rather than intended. `verdict_from()`
is precedence — `BLOCKED > FAIL > CONDITIONAL > PASS` — and
`test_the_verdict_is_not_a_score` asserts the payload carries no key ending in
`_score` and no `score` key at all.

The reason is not aesthetic. A weighted composite can always be tuned until it
hides the check that mattered, and it is exactly the shape someone reaches for
when they want a disappointing result to look acceptable. A report that says
`83/100` invites "close enough"; one that says `FAIL on significance` does not.

**BLOCKED is a first-class outcome.** FAIL says the candidate is not good
enough; BLOCKED says we could not tell. A calibration check on 40 predictions
has not measured calibration, and reporting either PASS or FAIL from it would be
a claim the sample cannot support. BLOCKED outranks FAIL for the same reason: a
report containing an unevaluable check cannot honestly say the candidate failed.

**Migration 0018 deliberately adds no `validated` status to `model_versions`.**
The obvious-looking addition would be a state nothing can enter, since
`app/validation/` writes no version status at all. L25 declined to add
`validation_passed` on identical reasoning. Declaring a state nothing can reach
makes a vocabulary a wish list.

### 24.4 What the L26 audit did not find

No second simulator, permutation implementation, clustering implementation,
calibration implementation, leakage checker, split implementation, job queue or
model loader. No path from `app/validation/` to `app.execution`, `app.oms`,
`app.risk`, `app.sizing`, `app.brokers`, `app.orders`, `app.strategies`,
`app.bots` or `app.paper` — asserted by parsing every module with `ast` — and no
write to `model_versions.status` anywhere in the package.

### 24.5 Two defects L26's own work found

**`checks.temporal` merged two different quantities under one key.** The
evidence dict spread row counts (`{"train": 682}`) and then `split.as_dict()`,
whose `train` is an index pair `[0, 682]`. The second overwrote the first, so
the number beside the sentence "train 682" was a list. Fixed by nesting the
split; a reader comparing two things called `train` is the failure mode.

**Migration 0018 first declared `sa.JSON()`** where the model uses `JSONType`,
which is JSONB on PostgreSQL. It passed on SQLite and drifted on PostgreSQL —
caught by `test_upgrade_creates_every_table_without_drift`, which only runs when
`TEST_DATABASE_URL` is set. The same class as §23's four defects, found the same
way, one level later.


---

## 25. AI strategy integration, audited at L27 (2026-09-04)

### 25.1 The audit's headline: the pipeline was already right

L27 is the level where the previous seven pay off. The audit expected to find a
place to put an AI stage and found the stage already there, in the right
position, in both pipelines:

| Component | Where | Verdict |
|---|---|---|
| `AiFilter` / `AiVerdict` — the seat and its return type | `app/execution/ai.py` (L16, moved at L22) | **KEEP** |
| `AiPolicy`, `AiGate`, `ModelBackedFilter` | `app/execution/ai.py` (L24) | **KEEP**; `AiPolicy` MOVED to a neutral module and re-exported |
| The AI stage, BEFORE risk, in both pipelines | `app/paper/engine.py` §4, `app/execution/pipeline.py` stage 4 | **KEEP** — nothing had to move |
| `Outcome.ai_rejected`, already in `NO_ORDER` | `app/execution/outcome.py` (L20) | **KEEP** |
| `Prediction`, `PredictionStatus`, `FeatureContract`, `ModelIdentity` | `app/ai/contract.py` (L24) | **KEEP** — one prediction contract |
| `ModelRegistry.get(key, version)` | `app/ai/registry.py` (L24) | **KEEP** — `latest()` is a separate call, deliberately |
| `compute_features` | `app/datasets/features.py` (L23) | **KEEP** — one feature engine, training and inference |
| `model_predictions`, idempotent on `prediction_key` | L24 | **KEEP** — the journal REFERENCES it |
| `validation_runs` | L26 | **KEEP** — it is what makes a model eligible |
| `build_signal_vector`, `simulate()` | L14 | **KEEP** — the filter applies to the vector; no second backtest |
| Modes, thresholds, the decision type, the journal, eligibility | *(absent)* | **ADD** |

Nothing was marked REPLACE or REMOVE. `ModelBackedFilter` is kept and still
tested: it works, several tests describe it, and §48 asks explicitly not to
create a second AI service or model loader.

### 25.2 The extraction, for the third time

`AiPolicy` was defined in `app/execution/ai.py` and L27's integration service —
which lives in `app.ai` — needs it. Importing `app.execution` from `app.ai`
would reverse the one dependency direction this codebase keeps, and that
reversal is precisely the cycle L22 had to break when `AiVerdict` was defined
inside the paper engine.

So the shared vocabulary moved to `app/ai/decision.py` and the old home
re-exports it. Every existing caller and test unchanged. Third instance of the
same extraction, same reason each time: **a name two packages need does not
belong inside either of them.**

### 25.3 Model selection: a boundary, not a registry

§12 asks that AI integration use validated models, that arbitrary selection be
impossible, and that no competing registry be built while L28 is outstanding.

`app/ai/eligibility.py` holds no state and stores nothing. It reads
`model_versions.status` (L24 writes `draft`; L28 will write the rest) and
`validation_runs` (L26's verdict), and answers one question. Today a version is
eligible when its most recent completed validation returned PASS or CONDITIONAL;
when L28 begins writing statuses, `USABLE_STATUSES` becomes the gate and the
change is to one function rather than a search.

**An unvalidated model is refused, and the reason is not "it is bad".** It is
that nothing has been established about it — the same distinction L26 draws
between FAIL and BLOCKED, applied one level down.

### 25.4 What the audit did not find

No second AI service, model loader, feature pipeline, strategy engine, signal
engine, prediction format or job queue. No path from `app/ai/` to
`app.execution`, `app.oms`, `app.risk`, `app.sizing`, `app.brokers`,
`app.orders`, `app.positions`, `app.paper` or `app.bots` — asserted by parsing
every module with `ast` — and no occurrence of `SizingRequest`, `lot_for_risk`,
`calculate(`, `LIVE_TRADING` or `live_trading` anywhere in the package.

### 25.5 The two defects L27's own work found

**Migration 0019 passed a pre-prefixed unique-constraint name.** The naming
convention adds `uq_<table>_`, so the migration created
`uq_ai_strategy_configs_uq_ai_strategy_configs_one_config_per_scope` while the
model declared the bare one. It is the identical defect migrations 0014 and 0015
had with `drop_constraint`, one level and one constraint type later, and it was
caught the same way: by the drift test, which only runs against a real
PostgreSQL. **The rule generalises to every constraint kind, not just checks:
pass the BARE name.**

**An out-of-range probability was reported as `PIPELINE_ERROR`.** L24's
`Prediction.__post_init__` refuses a probability outside [0,1] in its own
constructor, so a broken model raises before the integration service's own §25
check can run. The check is not dead — it covers NaN through other paths — but
the *label* was wrong: "something raised" loses which model and which field.
Now caught by type and reported as `INVALID_OUTPUT` naming both.


---

## 26. The model registry, audited at L28 (2026-09-04)

### 26.1 What existed: nearly all of it

L28 is a level of consolidation. `model_versions` has been the one table since
L05; L24 wrote provenance onto it, L25 wrote candidates into it, L26 judged
them, and L27 built the eligibility boundary and said in its own docstring that
switching it to `model_versions.status` would be one function.

| Component | Where | Verdict |
|---|---|---|
| `models`, `model_versions`, the status CHECK | L05, extended L24 | **KEEP + MODIFY** — four statuses, fourteen nullable columns, two CHECKs |
| `ModelRegistry` (in-process, loaded objects) | `app/ai/registry.py` (L24) | **KEEP** — a cache of models, not a lifecycle. Not the thing §47 forbids duplicating |
| `register_version()` writing a `draft` | `app/ai/service.py` (L24) | **KEEP** — the entry point into the lifecycle |
| `model_from_version()` | `app/ai/loader.py` (L26) | **KEEP** — the one deserialiser |
| `validation_runs` and its verdict | L26 | **KEEP** — it IS the registration gate |
| `app/ai/eligibility.py` | L27 | **KEEP + MODIFY** — the switch L27 promised |
| `AuditLog` | L05 | **KEEP** — every transition writes one |
| L07 hub, catalogue, channels | L07 | **KEEP + MODIFY** — one scope, eight event types |
| Lifecycle, artifacts, deployments, resolution, comparison | *(absent)* | **ADD** |

Nothing was REPLACED or REMOVED, and §47's list was checked item by item: no
second registry, model loader, feature pipeline, version table, prediction
format or job queue was created.

### 26.2 The defect the tests found: NULLs are distinct in a unique index

The one-active-per-scope rule (§14) was a partial unique index over
`(model_key, strategy_key, symbol, timeframe, environment)`. The test that
inserted a second active deployment expected an `IntegrityError` and got none.

**NULLs are distinct in a unique index.** So any number of rows could share the
UNRESTRICTED scope — which is the most common one. The guarantee would have been
absent exactly where it mattered most and present everywhere it was easy to
test, which is the worst shape a constraint can have.

Fixed with a derived NOT NULL `scope_key` (`"strategy|symbol|timeframe"`),
written by `Scope.key()` so no caller can produce an inconsistent one. The
nullable columns stay: they are what a reader and a query use, and "not
restricted" must remain expressible as NULL.

The general form is one this project keeps meeting: **a constraint that is easy
to test and hard to violate is not evidence.** The test that found this one is
the one that tried to violate it.

### 26.3 The defect the audit found: three levels of realtime events never fired

§27 asks to reuse the existing Redis/WebSocket infrastructure, so the audit read
it. `Hub.publish` takes ONE argument:

    async def publish(self, event: Event) -> None:

and `TrainingService._publish` and `ValidationService._publish` both passed two:

    await self.hub.publish(event, {"job_id": job_id, **payload})

Every training and validation event since L25 raised a `TypeError`, which the
surrounding `except Exception` caught and logged as a warning. **The events never
reached the bus, and nothing noticed** — because a subscriber that never fires is
indistinguishable from a market that never moved, which is the sentence
`Hub.publish` itself uses to explain why it refuses an uncatalogued type.

Even fixed, the dotted names (`training.job.started`) were not in the catalogue
and would have been refused. So L28 added a `model` scope and eight types: two
progress types rather than one per stage — a training run publishes a dozen
stage changes and cataloguing each would make the vocabulary a log format — and
one per lifecycle transition, on the reasoning that keeps `ORDER_FILLED` apart
from `ORDER_CANCELLED`.

`channels.authorize` needed a rule for the new scope; without one it would have
hit the catch-all and refused everyone. Gated at TRADER to match
`manage_ai_models` on the REST surface: a channel readable by someone the API
would refuse is a way around the API.

### 26.4 The recurring migration defect, third occurrence

Migration 0020 passed `"ck_model_versions_status"` to `drop_constraint`, and the
convention prefixed it again into
`ck_model_versions_ck_model_versions_status`. Identical to 0014, 0015 and 0019,
now on a third constraint kind. The rule is stated without an exception in the
migration's own comment: **every constraint kind, bare name, always.**

The FK is the mirror image and worth recording: the `fk` convention does NOT
interpolate a caller-supplied name at all, so `create_foreign_key` takes `None`
and lets the convention supply it — passing a name there would be a name nothing
uses.

### 26.5 The decisions that shaped the level

**Eight states, four declined IN CODE.** `app/ai/lifecycle.DECLINED` records the
reason for each, because "we thought about it and decided against" and "we
forgot" look identical in a schema. The sharpest is `VALIDATING`: nothing could
set it, since L26 deliberately writes no model status and `validation_runs`
already records that a run is running. The same fact in two places means the
copy nobody updates is the one somebody reads.

**One edge into `promoted`, from `paper`.** §12 as a property of the transition
table rather than a check in a function, so it cannot be forgotten by a new code
path. A test enumerates the sources of that edge and asserts the list is exactly
`[paper]`.

**`validated` does not serve inference.** Validation is about the EVIDENCE;
registration is about the ARTEFACT — it loads, its digest matches, its feature
contract still holds. Two checks that fail independently, and serving from a
version that passed only the first is the gap §16 exists to close.

**Superseding is not retiring**, which deviates from §14's example. Retiring the
previous version on every promotion would make every rollback a resurrection of
a terminal state, and §22 says retirement should be a deliberate separate act.
The replaced version returns to `registered`, still eligible.

### 26.6 What the L28 audit did not find

No second registry, loader, feature pipeline, version table, prediction format,
job queue or realtime system. No path from any registry module to
`app.execution`, `app.oms`, `app.risk`, `app.sizing`, `app.brokers`,
`app.orders`, `app.positions`, `app.paper`, `app.bots` or `app.training` —
parsed with `ast`. No `db.delete` anywhere in the package, and no `DELETE` route
under `/v1/ai`.


---

## 27. AI model monitoring, audited at L29 (2026-09-04)

### 27.1 The most prior art of any level, and a stale sentence

`app/monitoring/` predates L24. The audit found the statistical engine, the
checks, the findings vocabulary and the escalation ladder all present and
tested:

| Component | Verdict |
|---|---|
| `stats.py` -- PSI, KS, categorical PSI, Brier, reliability bins, ECE, Benjamini-Hochberg, Welch's t | **KEEP** |
| `checks.py` -- eight checks | **KEEP + MODIFY** (four added) |
| `findings.py` -- `Severity`, `Check`, `Action`, `FORBIDDEN_ACTIONS` | **KEEP + MODIFY** |
| `escalation.py` -- the WARNING->PAUSE->REVIEW->RETRAIN->VALIDATE ladder | **KEEP**, untouched |
| `monitor.py` | **KEEP + MODIFY** |
| `ai_decisions` (L27), `model_deployments` (L28), `trades` (L19), `validation_runs` (L26) | **KEEP** -- read, never duplicated |
| L07 hub, `Notification`, workers, RBAC | **KEEP** |
| Collection, baselines, health, alert lifecycle, snapshots, API, frontend | **ADD** |

**And it had never run.** `MonitoringInputs` was a dataclass a caller filled by
hand, and nothing filled it. `monitor.py`'s own docstring said why -- *"There is
no model in this platform yet"* -- which was true when it was written and which
L27 and L28 made false. The missing piece was not statistics; it was the module
that reads what actually happened.

### 27.2 The decisions that shaped the level

**`INSUFFICIENT_DATA` outranks `HEALTHY`.** In the precedence, and in a CHECK
constraint (`health_state <> 'HEALTHY' OR sample_count > 0`). A run in which
checks could not be evaluated is not a healthy run, and §43 is explicit that too
little data must never become a false pass. This is L26's BLOCKED-is-not-FAIL
one level along.

**`OFFLINE` outranks everything.** A model that is not serving cannot be healthy
or degraded; every other reading would be about a model nobody is using.

**`DEGRADED` is reserved for the model.** A serious reading on a feature
distribution WARNS. §42 asks that a distribution change not be read as failure,
and this is where that becomes a state an operator reacts to differently: "the
inputs moved" is the world moving and "calibration degraded" is the model
changing.

**Concept drift is inferred from exactly one pattern.** §15 forbids claiming it
from input drift alone, so `possible_concept_drift` fires only when outcomes
degraded while the inputs did NOT -- and the reverse case says "NOT concept
drift" out loud, because that is the reading a person is most likely to get
wrong.

**A severity change is a new alert.** The fingerprint includes the severity, so
WARNING -> CRITICAL cannot be suppressed by the cooldown. Suppressing it would
hide the transition an operator most needs to see.

**`availability` excludes `AI_DISABLED` from the denominator.** A strategy that
was never asked did not fail to answer, and counting those would make turning
the AI off look like perfect uptime.

**An unresolved decision is not scored as a loss.** Only a `filled` outcome
resolves. A risk veto, an AI rejection or a sizing refusal says nothing about
whether the model was right -- and counting a veto as a wrong prediction would
make a conservative risk configuration look like a broken model.

### 27.3 The defect this level found

A CHECK constraint named `a_healthy_snapshot_measured_something` produced
`ck_model_monitoring_snapshots_a_healthy_snapshot_measured_something` -- 67
characters. **PostgreSQL truncates identifiers at 63** and SQLAlchemy appends a
4-character hash when it does, so the model's resolved name and the migration's
literal name diverged and the drift test failed.

Renamed to fit. The general form is the one §23 records: a constraint that only
a real PostgreSQL can evaluate is one SQLite will not catch, and identifier
length is another way the test environment is more permissive than production.

### 27.4 What the L29 audit did not find

No second monitoring service, drift detector, metrics pipeline, alert system,
trade history, portfolio calculation or realtime system. No path from
`app/monitoring/` to `app.execution`, `app.oms`, `app.risk`, `app.sizing`,
`app.brokers`, `app.orders`, `app.positions`, `app.paper`, `app.bots` or
`app.training` -- parsed with `ast` -- and no call by name to `promote`,
`rollback`, `retire`, `deploy_to_paper` or `register`, so the registry's verbs
are unreachable from here.


---

## 28. Portfolio and exposure, audited at L30 (2026-09-04)

### 28.1 Almost everything was already here

The audit found no missing engine. It found six systems that each owned a piece
and nothing that assembled them:

| Component | Verdict |
|---|---|
| `portfolio_snapshots` (L05) — balance, equity, margin, exposure, drawdown_pct | **KEEP**, and it needed no column |
| `positions` (L21) — the open book | **KEEP**, read; no second representation |
| `trades` (L19) — the closed record | **KEEP**, read; no second history |
| `symbol_mappings` (L11) — contract size, tick size, tick value | **KEEP**, the only source of a notional |
| `app.risk.state.day_start` (L17) | **KEEP**, reused rather than redefined |
| `app.symbols.precision.value_per_price_unit` (L18) | **KEEP**, the single conversion |
| `PaperPortfolio` / `paper_accounts` (L16) | **KEEP**, authoritative for a paper account |
| `brokers.base.Account` (L10) | **KEEP**, authoritative for a live one |
| `PositionReconciler` (L21) | **KEEP**, untouched — it repairs, and this engine may not |
| `/portfolio/summary` 501 stub | **REPLACE** |
| Frontend `PlannedPage` | **REPLACE** |
| `portfolioService.summary()` returning `unavailable` | **REPLACE** |
| State, exposure, P&L, events, service, routes, dashboard | **ADD** |

**No duplicate was created.** No second position table, no second trade history,
no second snapshot table, no second reconciler, no second realtime system, no
second day boundary and no second tick-value conversion.

### 28.2 What L21 deliberately does not store, and why it mattered

`positions` has no `current_price` and no `unrealized_pnl`, and L21's own
comment says why: *"Unrealised P&L is deliberately NOT a column: it is a function
of a price that changes every tick, and a stored one is a number that is wrong
the moment it is written."*

That decision propagates straight into this level. A position is marked from a
price map the caller supplies — a live quote from the adapter, or nothing — and
a position with no quote is reported UNMARKED rather than valued at its entry.
Valuing at entry would have produced a full unrealized figure for every account,
always, and it would have been wrong by exactly however far the market had moved.

### 28.3 Two schema facts the first draft got wrong

`Position` carries `quantity`, `broker_account_id` and `paper_account_id` — not
`volume` and not one `account_id`. The two account columns are the L05 decision
§41 restates: paper and live are kept apart by TABLE and by COLUMN, not by a flag
somebody has to remember to filter on.

`Trade` carries no account column at all; it links to the POSITION, which carries
the account. So realized P&L is a join, and filtering on `mode='paper'` alone
would have pooled two paper accounts in one deployment.

Neither is documented anywhere except the models, which is why the audit step is
reading them rather than assuming.

### 28.4 No execution table carries a bot id

§17 asks for exposure per bot. Nothing in `orders`, `positions` or `trades`
records which bot opened a position — the Bot Manager owns that identity and
links it to an ACCOUNT.

So attribution runs that way, `bots.paper_account_id`, and a position whose bot
cannot be identified is grouped as `unattributed` rather than guessed at. One bot
per paper account holds today; a broker account running several would need a link
L19 does not write, and the report would say `unattributed` rather than pick one.

### 28.5 The defect this level found

`day_start` (L17) takes an AWARE UTC datetime — the risk engine hands it
`datetime.now(UTC)`. The portfolio engine works in NAIVE UTC, because that is
what the database stores. Passing a naive value straight through would have had
`.astimezone(UTC)` interpret it as LOCAL time.

That is precisely the bug `CLAUDE.md` records at length: on this UTC+5:30 machine
the trading day would have started at 18:30 the previous evening, and the same
code would have been correct on a UTC machine. Reusing the right function was not
enough; the tz-awareness boundary had to be crossed explicitly.

Caught before it shipped, by writing the day-boundary test first. A parsed test
now asserts that no module in `app/portfolio/` calls `.replace(hour=…)` at all.

### 28.6 What the L30 audit did not find

No second portfolio engine, exposure calculator, drawdown tracker, margin model,
equity history or correlation engine. No path from `app/portfolio/` to
`app.oms`, `app.orders`, `app.sizing`, `app.execution`, `app.brokers`,
`app.risk.engine`, `app.risk.service` or any position manager, executor or
reconciler — parsed with `ast` — and no reference by name to `place`,
`place_order`, `submit_order`, `cancel_order`, `modify_order`, `close_position`,
`modify_position`, `open_position`, `send_order` or `order_send`.


---

## 29. The trade journal, audited at L31 (2026-09-04)

### 29.1 The record already existed

252 rows, with bracket geometry and R, imported from `data/track_record.jsonl`.
Nine tables between them already held every fact the brief asks a journal to
answer:

| Component | Verdict |
|---|---|
| `trades` (L05) | **KEEP + MODIFY** -- 13 nullable columns |
| `positions`, `position_events` (L21) | **KEEP**, read |
| `orders`, `order_events`, `executions` (L19) | **KEEP**, read |
| `signals`, `webhook_events` (L09) | **KEEP**, read |
| `risk_events` (L17) -- decision, snapshot, configuration_version | **KEEP**, read |
| `ai_decisions` (L27) -- model, exact version, probability, regime | **KEEP**, read |
| `orders.sizing` (L18) | **KEEP**, read |
| `journal_entries`, `trade_tags` (L05) | **KEEP**, untouched |
| `ExitReason` (L21) -- eight members with an evaluation order | **KEEP**, reused |
| `/v1/trades`, `/v1/executions` | **KEEP + MODIFY** |
| `app/journal/`, `app/services/journal.py` | **ADD** |

### 29.2 The three gaps

**The live path wrote no trade.** `PositionManager._book` closes a position and
books `realized_pnl` onto the row. Nothing then created a `trades` row. Only
`app/paper/service.py` and `app/db/import_ledgers.py` ever wrote one, so a
demo or live position closing left the journal untouched -- and nothing noticed,
because an empty journal looks exactly like an account that has not traded.

**The paper path wrote one with no attribution.** No `position_id`, no
`strategy_version_id`, no account, no exit reason. A paper trade could not be
linked back to anything that produced it.

**Nothing assembled the context.** Every fact sections 8 to 12 ask for was
already recorded. What did not exist was the module that read them together.

### 29.3 The decisions that shaped the level

**One journal row per POSITION EPISODE**, enforced by a partial unique index.
Counting execution rows as trades is section 41's error, and this makes it a
database constraint rather than a convention.

**A sweep, not a hook.** Four call sites can close a position; a hook missed at
one produces a trade that silently never exists. `record_pending` asks the
database instead, and cannot miss.

**The timeline is derived and stored nowhere.** A `trade_events` table would be a
second copy of nine tables' worth of already-timestamped events, and when it
disagreed there would be no way to tell which was right. Derived, it also gets
section 27's idempotency for free.

**Realized P&L is summed from the closes, never recomputed from the weighted
average.** They agree only when the entry was never weighted, and booking both
is the double-counting section 14 forbids.

**`cancelled` and `rejected` are not trade statuses.** A journal row is written
when a POSITION closes, and a cancelled order never opened one -- `orders.status`
already carries both words.

### 29.4 The defect this level found

`op.drop_constraint("ck_trades_status", "trades", type_="check")` produced
`ck_trades_ck_trades_status`. The naming convention interpolates the prefix, so a
pre-prefixed name is doubled.

**Fourth occurrence in this repository, and the first on a DROP rather than a
CREATE.** Migrations 0019, 0020 and 0021 each hit it on a create. The rule, now
stated without exception: every constraint kind, both directions, bare name,
always. It was caught only because the round-trip downgrade test runs against a
real PostgreSQL -- SQLite would not have reached the statement.

A second, smaller one: `/trades/{trade_id}` was registered before
`/trades/statistics`, and Starlette matches in registration order, so the
parameterised route captured `statistics` as a trade id and answered 404. Caught
by the test that asserts both resolve, not by reading the code.

### 29.5 What the L31 audit did not find

No second trade table, trade history, execution history, P&L calculator, exit
vocabulary or realtime system. No path from `app/journal/` to `app.oms`,
`app.orders`, `app.sizing`, `app.execution`, `app.brokers`, `app.risk.engine`,
`app.risk.service`, any position manager, executor or reconciler, or
`MetaTrader5` -- parsed with `ast` -- and no reference by name to any
order-placing verb.


---

## 30. Analytics, audited at L32 (2026-09-04)

### 30.1 The finding: three copies of one formula

| Where | Unit | Computes |
|---|---|---|
| `app/backtest/runner.py` `compute_metrics` | points | win rate, PF, expectancy, Sharpe, Sortino, max drawdown, t |
| `app/training/metrics.py` `economic` | label returns | win rate, PF, expectancy, max drawdown |
| `app/services/journal.py` `statistics` (L31) | account currency | win rate, counts, sums |

**They agreed.** That is the dangerous case rather than the safe one: three
copies stay agreeing only until somebody changes one, and the copy nobody looked
at is the one somebody quotes. `runner.py` already carried
`MIN_TRADES_FOR_RATIO = 20` and a `NOT_AVAILABLE` sentinel, and both were
correct -- which is why §2's instruction is to REUSE rather than replace.

### 30.2 What was reused, and what changed

`app/analytics/metrics.py` is the extraction. `runner.compute_metrics` and
`training.metrics.economic` now call it, and **neither output shape changed** --
`NOT_AVAILABLE` stays `NOT_AVAILABLE` because the backtest API has served that
token since L14. 98 backtest and training tests pass unchanged, which is the
evidence the refactor moved no number.

| Component | Verdict |
|---|---|
| `trades` (L31), `orders`/`executions` (L19), `ai_decisions` (L27) | **KEEP**, read |
| `PortfolioService` (L30) | **KEEP**, delegated to -- never a second account state |
| `backtests.summary` (L14) | **KEEP**, served, never recomputed |
| `app/monitoring/*` (L29) | **KEEP**, untouched -- §53 keeps drift and outcomes apart |
| `app.risk.state.day_start` (L17) | **KEEP**, reused for the trading day |
| `compute_metrics`, `economic` | **REFACTOR** -- delegate, keep the contract |
| `/analytics/summary` 501 stub, frontend `PlannedPage` | **REPLACE** |
| `app/analytics/*`, `app/api/v1/analytics.py` | **ADD** |

### 30.3 The decisions that shaped the level

**The unit became a type.** `Series.concat` raises `UnitMismatch` rather than
pooling points with currency. The metals-points error made unrepresentable
instead of documented.

**Every summary is computed twice**, in currency and in R, each labelled. A
single figure would leave the reader to guess which they were reading, and only
one of them pools.

**`INSUFFICIENT_DATA` is a value, not an absence.** A string sentinel so it
survives JSON and cannot be mistaken for zero. A profit factor with no losers is
not infinity; that is how a run of winners looks decisive.

**Two equity curves.** §7 asks for deposits and withdrawals to be handled *if
they exist*. They do not -- there is no cash-movement table anywhere -- so the
realized curve (which cannot contain a deposit) is the performance figure, and
the account curve carries a note saying a deposit and a profit look identical in
it. Inventing a reconciliation between them would be the fabrication §48 forbids.

**No caching, and it is a measured decision rather than an omission.** §40 says
not to optimise without measuring: the largest table holds 252 rows, the summary
computes in under 10ms, and a cache adds an invalidation path that could serve a
stale equity figure as current -- which §27 itself warns against.
`calculation_ms` is on every summary so this can be revisited with evidence.

### 30.4 What the L32 audit did not find

No second analytics engine, metric module, equity calculator, drawdown tracker or
reporting service. No materialised view, no scheduled report, no cached analytics
table. No path from `app/analytics/` to `app.oms`, `app.orders`, `app.sizing`,
`app.execution`, `app.brokers`, `app.risk.engine`, `app.risk.service`, any
position manager, executor or reconciler, or `MetaTrader5` -- parsed with `ast`
-- and **no write verb at all**: no `add`, `commit`, `delete`, `flush`,
`drop_all`, `truncate` or `merge` appears anywhere in the package.


---

## 31. AI trade review, audited at L33 (2026-09-04)

### 31.1 There is no LLM in this platform

The search section 2 asks for found no `openai`, no `anthropic`, no API key
setting, no prompt template and no provider abstraction anywhere in the
repository. `app/ai/` holds logistic, regime and anomaly models written in
stdlib; none of them talks to a language model.

That is the finding L24 recorded about models -- *"No AI model existed anywhere
before it"* -- and it has the same consequence: nothing was preserved, deleted or
replaced, and the honest thing to build is the SEAT rather than an occupant.

| Component | Verdict |
|---|---|
| `trades` and its L31 attribution links | **KEEP**, read |
| `positions`, `orders`, `executions`, `risk_events`, `ai_decisions` | **KEEP**, read |
| `strategy_versions.config` | **KEEP**, read at the trade's OWN version |
| L32 analytics | **KEEP**, consumed for baselines and patterns |
| L29 monitoring, L28 registry | **KEEP**, untouched -- sections 51 and 52 |
| `journal_entries.ai_review` (a `# L33` column since L05) | **KEEP, unused** |
| `app/review/*`, `app/api/v1/reviews.py`, `trade_reviews` | **ADD** |

### 31.2 The column that was reserved for this level, and why it was not used

`journal_entries.ai_review` has carried a `# L33` comment since L05. It is a
USER's note row -- title, body, and that column -- and L31 section 39 already
separated user notes from system-generated facts.

Putting a machine review there would erase that separation the moment somebody
queried it: a table whose rows are sometimes a person's observation and
sometimes a generated assessment cannot answer "who said this?". So the column
is kept, left alone, and `trade_reviews` is a new table.

### 31.3 The decisions that shaped the level

**The decision/outcome split is structural, not procedural.** Four of the five
assessors take a `DecisionContext` and nothing else, and the type has no
`exit_price`, `net_profit`, `r_multiple`, `mae` or `mfe` on it. Section 63's test
passes because the function is not given the data.

**UNKNOWN is never POOR.** Section 12 says so about compliance; the same rule is
applied to every section, and each UNKNOWN names the field it needed. It is
L26's BLOCKED-is-not-FAIL and L29's INSUFFICIENT_DATA-is-not-HEALTHY again.

**A provider cannot return a rating.** `Narration` carries a summary and three
lists of statements and nothing else, so the hallucinations section 37 lists have
no route into the stored record before the validator even looks.

**Confidence is derived from completeness and coverage**, with no term for how
sure the narrative sounded -- section 22 says not to treat a generator's
confidence as statistical certainty.

### 31.4 The test that was wrong, and was narrowed rather than suppressed

The forbidden-name check first included `rollback` and `register`, and caught
`db.rollback()` in the idempotency handler and `WorkerRegistry.register`. Both
are unrelated to the model registry.

A rule that flags unrelated code is a rule people learn to ignore, so the name
check now lists the registry's SPECIFIC verbs -- `promote`, `retire`,
`deploy_to_paper`, `stop_deployment` -- and an import check does the real work:
`app.ai.registry_service` is never imported, so its verbs are unreachable.

The general form is one this repository has met before at L27 and L28, where a
grep-based safety test failed on a docstring explaining the rule it was
enforcing. A safety test that cries wolf stops being a safety test.

### 31.5 What the L33 audit did not find

No existing trade-review module, explanation generator, report builder, prompt
store or LLM client. No path from `app/review/` to `app.oms`, `app.orders`,
`app.sizing`, `app.execution`, `app.brokers`, `app.risk.engine`,
`app.risk.service`, `app.ai.registry_service`, any position manager, executor or
reconciler, or `MetaTrader5` -- parsed with `ast` -- and no reference by name to
any order-placing verb, any live-trading flag, or any deletion verb.

---

## 23. The first run against real services (2026-09-04)

Every gate this project had — pytest, ruff, mypy, CI — ran against SQLite and
an absent Redis. This is what happened the first time the platform was run
against the services it actually deploys on.

### 23.1 Four defects that could not be seen before

**`positions.status` was `VARCHAR(8)`.** L21 added `partially_closed` (16
characters) and `reconciling` (11) to a column sized when the vocabulary was
`open` / `closed` / `unknown`. **SQLite ignores `VARCHAR(n)`; PostgreSQL
enforces it.** So all 65 position tests passed, and a partial close or a
reconciliation pass would have raised `StringDataRightTruncationError` in
production — on the position-management path, which is the one that matters
most.

**`training_runs.status` was `VARCHAR(16)`** against L25's successful terminus
`validation_pending` (18). Identical defect, and it is how the first one was
found: an end-to-end training run against PostgreSQL failed at the `recording`
stage.

**Migrations 0014 and 0015 passed pre-prefixed constraint names to
`op.drop_constraint`.** The naming convention adds `ck_<table>_`, so the
downgrade asked Postgres to drop
`ck_training_runs_ck_training_runs_status`. Every migration from 0007 onward
passes the bare name; mine did not, and no test had ever executed a downgrade.

**Three `Money` columns were created `Numeric(18,8)`** where their models
declare `Numeric(18,4)`: `bots.max_daily_loss`, `bots.max_risk_per_trade` and
`positions.realized_pnl`. `MIGRATION_STATUS.md` has claimed "zero drift" since
L05, and the assertion that checks it had never run.

### 23.2 What was verified, not asserted

- **All 17 migrations apply, downgrade and re-apply** against real PostgreSQL,
  with **zero autogenerate drift**.
- The development database was migrated **0009 → 0017 in place**, on a
  populated schema — a real forward migration, not a fresh create.
- The backend image was **built and exercised**: `app.strategies.indicators`,
  `app.backtest.runner`, `app.replay.engine`, `app.strategies.rules`,
  `app.datasets.features`, `app.main`, `app.ai` and `app.training` all import
  and run inside it. Every one of those would have failed before §21.2's fix.
- The whole stack came up healthy — database, Redis and two workers.
- **The AI chain ran end to end over HTTP against PostgreSQL**: 700 bars stored
  through `MarketDataService.store_bars` (and a second store correctly inserted
  0), a dataset built to READY with 638 rows and 6/6 leakage checks passing, and
  a training job that finished at `validation_pending` with a **draft** model
  version. `SELECT count(*) FROM model_versions WHERE status='promoted'`
  returns **0**.
- Backend **1417 passed, 0 failed, 0 skipped**.

### 23.3 The finding worth generalising

Three of the four defects were invisible for the same reason: **the test
environment was more permissive than the production one.** SQLite accepts a
`VARCHAR` overflow, has no `ALTER` to get wrong, and never runs a downgrade.

`tests/test_models.py::test_every_status_value_fits_its_column` closes the
first class permanently — it walks every `col IN (...)` CHECK against its
column's declared length across every table. The other two are closed by the
migration tests, which now have a database to run against.


---

## 32. Notifications through recovery, audited at L34-L38 (2026-09-05)

Five levels in one pass. What every one of their audits found is the same
thing, and it is worth stating once:

**The seats were already cut.** Not the implementations -- the shapes. And in
four of the five cases the level was mostly a matter of sitting in one.

### 32.1 What each audit found already built

| Level | Found | What was actually missing |
|---|---|---|
| **34** Notifications | The L07 event bus, the hub, `NOTIFICATION_CREATED` catalogued since L07 with its `user` scope and its authorization rule, the `notifications` table from migration 0002, L29's deduplication reasoning, L02's worker base, the rate limiter, pagination, and `ResetDelivery` -- a Protocol whose only implementation refused, with a docstring naming L34 | the service, the preferences, the delivery record, and an email provider |
| **35** Discord | `Channel.DISCORD` in the enum, a preference row per category defaulting to off, a delivery CHECK already accepting it, and a factory returning NOT_CONFIGURED | one adapter and two routes |
| **36** Admin | RBAC with 16 named permissions, `require_permission`, revocable server-side sessions, `/admin/users` with last-admin protection, `audit_logs` with a scrubber, CSRF, the limiter, paginated collections, and working permission-gated surfaces for bots, models, brokers and risk | a dashboard, user search, activation, session revocation, and a page |
| **37** Monitoring | JSON logging, request and correlation ids, secret scrubbing, three-state health checks with weighted aggregation, worker heartbeats and staleness, hub counters, broker and provider health, L29's whole model-monitoring engine, and **`system_events`** -- created at L05 for "connects, disconnects, reconciliations, halts" and never written to | a metrics registry and something that pulled the rest into one answer |
| **38** Recovery | Four reconcilers (L10 broker, L19 OMS, L21 positions, L22 bots), each reporting rather than repairing; `intent_id` idempotency; L20's signal deduplication; L02's worker loop and graceful shutdown; L22's `SafetyCheck` seat defaulting to refuse-everything | **something that ran them at boot**, and a latch |

### 32.2 The finding worth generalising

**Three tables and one Protocol were built, correct, and unused for many
levels.**

* `notifications` -- migration 0002, written only by L29's monitoring path, read
  by nothing. L34 extended it rather than shadowing it with a better-named
  second table.
* `system_events` -- migration 0002, docstring naming exactly the operational
  events L37 and L38 needed, **never written to**. L37 shipped no migration
  because of it; L38 shipped none either.
* `audit_logs` -- written since L04, readable since L06, and L36's brief asked
  for an `admin_audit_logs` table beside it. Extended with one column instead.
* `ResetDelivery` -- L04's Protocol, refusing, with `(level 34)` in its
  docstring. L34 implemented it thirty levels later and a deployment with no
  SMTP host still behaves exactly as it did.

The pattern in all four: **a level that names its successor in a docstring
leaves a seat that costs the successor almost nothing.** L34 spent one file on
Discord's seat and L35 cost one adapter, two routes and no migration. The
inverse also held -- three status documents had drifted (the table count, the
`PRODUCED_NOW` set, the monitoring severity vocabulary) and each was a one-line
correction found by a test rather than a rewrite.

### 32.3 Three defects found by writing the tests

1. **L37, `Tracker.observe`.** It updated its recorded state on every healthy
   observation, including while a recovery was still being counted towards --
   which erased the "it was down" the threshold was counting against. **No
   recovery could ever have been announced.** Found by the test asserting one
   incident and then one recovery.
2. **L38, `Outcome.safe_mode`.** Added to the enum and not to `NO_ORDER`, so
   `created_order` reported True for a refusal that sent nothing: the execution
   log would have claimed an order was created when the pass stopped at gate
   zero. Found by the test exercising the gate.
3. **L34, two vocabularies in one column.** `notifications.severity` held L29's
   three lowercase words and was about to hold L34's five uppercase ones. Fixed
   by importing the enum in both places rather than spelling the words twice --
   the same move L28 made for statuses and L33 for review states.

### 32.4 What these five levels deliberately did not build

Recorded here because a refusal that is not written down reads later as an
oversight:

* **A second event bus, a second logging system, a second health
  implementation, a second monitoring stack, a second audit table, a second
  reconciler.** Each was the obvious implementation and each was refused for the
  same reason: two implementations of one fact are two answers, and the one on
  the dashboard is not the one the system acted on.
* **A feature-flag table** (L36). This platform's flags are `LIVE_GATES`, which
  are code rather than configuration on purpose.
* **An uptime percentage** (L37). Transitions are stored, not samples.
* **A backup script** (L38). One this project cannot test is one nobody should
  trust; `BACKUP_RESTORE.md` states an honest RPO of "since the last manual
  dump" instead.
* **Automatic reconnection** (L38). It belongs with a live adapter, and this
  platform has never registered one.

### 32.5 What is still not true

Unchanged by five levels, and worth repeating because the dashboards now say it
out loud:

* `market_bars` covers one instrument. `market_data.freshness` reports UNKNOWN with *"no bar
  has ever been stored"*.
* No broker adapter has ever been registered. `broker` reports NOT_CONFIGURED.
* No model is deployed. `ai.models` reports NOT_CONFIGURED.
* Every reconciliation path, every Discord response code and every failure mode
  is exercised against a fake.

**The code is now ready to be told it is wrong. Nothing has told it yet.**


---

## 33. Security and integration, audited at L39-L40 (2026-09-05)

Two levels, and one finding runs through both: **the tests that had never been
run were where the defects were.**

### 33.1 Seven real defects, and what they have in common

| # | Defect | Present since | Why nothing caught it |
|---|---|---|---|
| 1 | A concurrently re-delivered webhook answered **500** | L09 | the `signals` race was handled; the `webhook_events` insert a few lines earlier was not, and no test ran two requests at once |
| 2 | `StaleDataError` after the rollback | L09 | hidden behind (1) -- it could not surface until (1) stopped raising first |
| 3 | A **2.06-second** webhook with Redis down | L07 | correctness never depended on Redis, so every test passed; nothing measured latency |
| 4 | A single-use step-up grant burned on an impossible action | L39 | gate ordering; the tests that would have shown it were written the same day |
| 5 | A CORS origin carrying a path accepted | L39 | the validator was written and the test that broke it was written in the same session |
| 6 | **Migrations 0024 and 0025 could not be applied to PostgreSQL** | L34, L36 | the three tests that check this had been SKIPPING since L02 for want of `TEST_DATABASE_URL` |
| 7 | The new security middleware doubled WebSocket flakiness | L39 | `test_realtime.py` needs Redis and had never run on this machine |

Three of the seven (1, 2, 6) were invisible because a **test existed and did
not run**. Two more (3, 7) were invisible because the *conditions* had never
been created -- a running Redis for (7), a stopped one for (3).

**The generalisable finding: a skipped test is worse than a missing one.** A
missing test is a known gap. A skipped test is a gap that reports as a pass. The
suite had said "4 skipped" for thirty-eight levels, and three of those four were
covering the single most production-critical path in the repository -- whether
the schema can be applied to the database it will actually be applied to.

### 33.2 The migration defect, because it is the one that mattered

`op.f()` marks an Alembic constraint name as final. Migration 0002 used it;
migrations 0024 and 0025 did not, so the metadata naming convention
`ck_%(table_name)s_%(constraint_name)s` prefixed an already-prefixed name and
produced `ck_notifications_ck_notifications_severity`.

**SQLite never notices**, because `batch_alter_table` rebuilds the table instead
of issuing `ALTER TABLE ... DROP CONSTRAINT`, so dropping a constraint that is
not there is silent. PostgreSQL refuses. The entire test suite runs on SQLite.

A deployment running `alembic upgrade head` would have failed inside migration
0024 with the schema half-applied. That is the failure mode this project's own
`BACKUP_RESTORE.md` was written to survive, and it would have been reached on
the first real deploy.

The fix also produced a smaller lesson worth keeping: **the `uq` convention
carries no `%(constraint_name)s` token**, so an explicitly named unique
constraint keeps its literal name and must NOT be wrapped in `op.f`. The first
attempt wrapped it and failed; the name was then read out of `pg_constraint`
rather than reasoned about.

### 33.3 What L39 found already correct

Worth recording, because the audit's most common outcome was "leave it alone":
no `eval`/`exec`/`subprocess`/`pickle`/`yaml.load`/`shell=True` anywhere; no
f-string SQL; no `dangerouslySetInnerHTML`, `innerHTML` or `localStorage` in the
frontend; no file-upload surface at all; a non-root container on a slim base
with every port bound to loopback; and **no tracked secret, no `.env` file, and
no credential anywhere in the tree**.

The controls that were genuinely absent were response security headers (there
were *none*), a CORS wildcard refusal, step-up re-authentication, a security
event vocabulary, and a per-user socket cap.

### 33.4 The seat pattern, again

L34 defined `Category.security` and left it with no rule, noting that the event
type filling it *"should be defined by L39"*. It was, and L39 shipped **no
migration** as a result -- the notification tables, the audit trail, the event
bus, the channel authorization and the severity vocabulary all already existed.
This is the fifth level in a row where the predecessor's docstring made the
successor cheap.

### 33.5 What is still not true

Unchanged, and repeated because L40's green suite could easily be read as more
than it is:

* **Nothing has ever been told it is wrong by a real venue.** Every
  reconciliation, disconnect, rejection and unknown-state recovery is exercised
  against a fake this repository wrote.
* **True request concurrency is untested.** SQLite with `StaticPool` shares one
  connection, so two of the seven defects were found at the edge of what the
  harness can express -- and a third may be behind them.
* **The frontend is tested against mocks.** A renamed Pydantic field would
  break the running application with no test failing.
* **`mypy` reports 16 errors and `ruff format --check` reports 12 files**, all
  pre-existing in L34-L38 code. CI gates on both, so **CI is red and was red
  before L39**.


---

## 34. Deployment, audited at L41 (2026-09-05)

The audit found a good development stack and no production story. Most of what
existed was kept; the two things that were wrong were both **invisible from a
developer's machine**, and that is the finding worth generalising.

### 34.1 Both defects were hidden by a development convenience

| Defect | Hidden by |
|---|---|
| nginx dropped the WebSocket upgrade on `/api/` | the API is also published on `127.0.0.1:8000`, and that is the address a developer's browser connects to. The proxied path -- the production path -- was never the one exercised. |
| the production overlay could not close a port | `docker compose config` was never read. The overlay *said* `ports: []` and everyone believed the file rather than the merged output. |

Both were found by **using the production path** rather than the development
one: the WebSocket by handshaking through nginx, the ports by parsing the
merged config. Neither needed new tooling.

This is the same shape as L40's finding about skipped tests. There it was *a
test that existed and did not run*; here it is *a path that existed and was not
taken*. In both cases the reassuring signal -- a green suite, a file that reads
correctly -- was measuring something other than what mattered.

### 34.2 The A/B that made it a fact rather than a claim

The WebSocket defect was proven, not reasoned about:

```
pre-L41 nginx config : HTTP 404 Not Found
post-L41 nginx config: HTTP 101 Switching Protocols
```

Same running API, same handshake, config swapped and swapped back. It is worth
recording the method: an inherited config that *looks* reasonable is best
tested by reverting to it deliberately.

### 34.3 A correction to an earlier audit finding

Section 21 recorded that the backend image did not carry `tools/` and that
numpy was undeclared. Both were fixed at L24 and this level verified it:
`tools/` is at `/srv/tools`, numpy is installed, and **all 293 `app.*` modules
import inside the image**. The two toolkit modules the app actually loads --
`rule_backtest` and `rule_search` -- both work.

`tools/indicators.py` does need pandas, which is absent. That is **not** a
defect: no application code path imports it. The near-miss is worth recording,
because a bare `import tools.indicators` inside the image looks exactly like a
broken deployment and is not one. CI now checks the modules the app actually
loads, rather than the directory.

### 34.4 What was refused, and why

* **Kubernetes.** Five containers on one host, one of which must not be
  replicated and one of which cannot be containerised at all. Its main benefit
  is the thing the worker constraint forbids.
* **Blue/green and canary.** Not "too small" -- two application versions
  running at once means two safe-mode latches and two startup reconciliations
  against the same account, and L40 established that concurrency is the
  least-tested area of the codebase. A brief controlled outage is the safer
  deployment for this application.
* **An automated backup script.** L38's refusal stands: one this project cannot
  test is one nobody should trust. What L41 added is the WAL configuration that
  makes a real backup possible.
* **A deploy stage in CI.** No registry, no target host.

### 34.5 What is still not true

* **No backup has ever been taken or restored.** This is now the top blocker.
* **No broker adapter has ever been registered.** Still the largest gap in the
  platform, and still one no test can close.
* **CI is red** on 16 `mypy` errors and 12 unformatted files from L34-L38.
* **The worker container cannot be scaled**, and the platform's safety at scale
  currently rests on nobody trying.

---

## 35. Hardening and go-live readiness, audited at L43-L44 (2026-09-05)

Two levels whose job was to close what L42 found, and whose most useful output
is the same in both: **an audit is only as good as the pattern it searches
for.**

### 35.1 The scan that missed an adapter

L42 asserted, and L44's own first draft repeated, that **no MT5 adapter
existed**. The evidence was a grep for `import MetaTrader5` across `app/`,
which found two read-only modules.

`app/brokers/mt5.py` is a complete demo adapter and was in neither result,
because it does not import MetaTrader5 directly -- it calls
`tools/mt5_paper.py`, which does. A second scan for `order_send` found it
immediately, and found that **it is the only module in the entire codebase that
calls that function.**

Two lessons, and the second is the useful one:

* The correction *improves* the platform's position. What is missing is a
  connection, not the code.
* **A clean scan is evidence about the pattern, not about the codebase.** L44
  now runs its bypass check two independent ways for exactly this reason, and
  the same doubt should attach to every other "none found" in these documents.

### 35.2 Two design errors, both caught by running rather than reading

Shadow mode was built twice before it worked, and neither failure was visible
in the source:

1. `mode = "shadow"` -- the RiskEngine checks `proposal.mode in ("paper",
   "demo")` and vetoes anything else, so every shadow signal died at the risk
   gate and a shadow run recorded **nothing**.
2. Zero equity -- risk then vetoed for insufficient equity. Same symptom, same
   cause: a plausible-sounding safety choice that stopped the pipeline before
   the thing it was meant to observe.

The fix for (1) could have been to add `"shadow"` to the RiskEngine's
allow-list. It was refused, and the principle is worth keeping: **that list is
a control that fails closed on an unknown mode, and widening a safety control
so a new feature fits is backwards. The feature adapts to the control.**

### 35.3 The verdict that was captured and thrown away

`ExecutionResult` carried `risk: RiskVerdict | None` and `as_dict()` rendered
the AI verdict, the sizing result and the order -- but not the risk verdict. So
every serialised decision showed what the AI thought and not why the *mandatory*
gate approved or vetoed.

This is the same shape as three defects L34-L38 produced: **a value added to a
structure without adding it to the thing that must cover the structure.** It is
now the fourth, and it suggests the pattern is worth a test rather than
vigilance.

### 35.4 What L43 closed, and what it could not

Closed: CI (mypy clean over 346 files), the `test_realtime.py` flake (root
cause: a database connection shared across two event loops), true request
concurrency (37/37 on PostgreSQL -- **and no third defect was hiding behind
L40's two**), and Redis outage latency (0.53s -> 0.02s).

Refused, with reasons: frontend contract tests (a feature), dependency pins
(changes every developer's install), worker leader election (unnecessary at one
replica), and any optimisation without evidence -- the frontend bundle and the
`notification_deliveries` index were both investigated and both left alone.

### 35.5 What is still not true

Unchanged across six levels of auditing, and worth repeating because 2386
passing tests can be misread as more than they are:

* **No terminal has ever been connected.** The demo adapter exists, is
  demo-fenced, and has never spoken to MT5.
* **No backup has ever been restored.**
* `market_bars` covers one instrument; no model is deployed; every AI test drives a stub.
* The frontend is tested against mocks.

**The platform is ready to be told it is wrong. Nothing has told it yet.**
