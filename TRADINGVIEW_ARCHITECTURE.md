# TRADINGVIEW_ARCHITECTURE.md

The TradingView → execution chain, the Autonomous Trading Orchestrator that
drives it, the KEEP/MODIFY/ADD decision for every capability the brief names,
and the incremental ladder that builds it. Written 2026-09-03 against the
autonomous-builder brief, after a read-only audit of the whole repository.

Baseline at the time of writing, all green: research **34**, backend **900
passed / 3 skipped**, frontend **77**.

---

## 1. The chain

```
TradingView
    ↓  alert webhook (L09)        |  Pine source / CSV export (inbox, A0)
DISCOVERY            app/tradingview/discovery.py            A0
    ↓
PARSE                app/tradingview/pine/                   A1
    ↓
UNDERSTAND           TradingViewSpec + AI interpreter seat    A1 / A5
    ↓
NORMALIZE            spec persisted + hashed                 A2
    ↓
COMPILE              StrategyCompiler                        A4
    ↓
StrategyDefinition   app/strategies/definition.py            L13  exists
    ↓
BuiltStrategy        app/strategies/built.py                 L13  exists
    ↓
Strategy engine      app/strategies/engine.py                L12  exists
    ↓
AI filter seat       app/paper/engine.py: AiFilter            L16  seat exists
    ↓
RISK                 app/risk/engine.py                      L17  exists
    ↓
SIZING               app/sizing/calculator.py                L18  partial
    ↓
OMS                  app/paper/oms.py / L19                   L16 paper / L19 partial
    ↓
Execution router     app/paper/router.py: provider_for()      L16  exists
    ↓
BROKER ADAPTER       app/brokers/base.py                     L10  exists
    ↓
MT5                  app/brokers/mt5.py (demo-fenced)        L10  exists
    ↓
RECONCILIATION       app/brokers/reconcile.py + L38          L10 / L38 not started
    ↓
MONITORING           app/monitoring/                         L29 / L37 partial
    ↓
ANALYTICS            tools/trade_stats.py, L32               CLI, API pending
    ↓
AI LEARNING          L23–L28                                 not started
```

**The forbidden shape does not exist and was verified, not assumed.** No module
in `app/paper`, `app/replay`, `app/backtest` or `app/strategies` imports
`app.brokers` or `MetaTrader5`; `MetaTrader5` is imported lazily in exactly two
places (`app/marketdata/providers/mt5.py`, `app/symbols/sync_mt5.py`) plus the
adapter itself. `tools/tv_webhook.py` holds no broker credentials and places
nothing. The L09 gateway writes a `Signal` and publishes `SIGNAL_CREATED`, and
nothing consumes it. So there is **no TradingView → MT5 path to refactor**
(brief §33's last clause); the work is to build the chain above, not to unpick a
shortcut.

## 2. Component decisions

Brief §8's vocabulary, applied to every capability the brief names. The
overwhelming majority is KEEP, which is the point of an integration brief.

### KEEP unchanged

| Capability | Where | Why untouched |
|---|---|---|
| Alert receiver (operator, standalone) | `tools/tv_webhook.py` | stdlib, records only, right tool for a watched run |
| Alert gateway (server) | `app/webhooks/` L09 | constant-time secret, 7-word vocabulary, redaction at depth, age check, idempotency, symbol resolution. Already the alert half of discovery |
| TradingView CSV import | `tools/tv_import.py` | tolerant layout detection; becomes the reconciliation input at G5 |
| Strategy contract, registry, engine | `app/strategies/{base,registry,engine}.py` L12 | no-look-ahead by construction; nothing executes code from a name |
| Declarative definition + evaluator | `app/strategies/{definition,built}.py` L13 | this **is** the compiler's target; it validates, round-trips and refuses `PRICE > RSI` |
| Indicator catalogue | `app/strategies/indicators.py` | four indicators, called from the toolkit, not reimplemented |
| Backtest engine | `simulate()` + `app/backtest` L14 | standing rule 4; the runner already delegates to it |
| Replay | `app/replay` L15 | proved trade-for-trade identical to `simulate()` |
| Paper pipeline | `app/paper` L16 | already data → strategy → AI seat → risk → sizing → OMS → fill → position |
| Execution router | `app/paper/router.py` | `provider_for(mode)` takes the mode and nothing else; configuration cannot reach it |
| Risk engine | `app/risk` L17 | 26 checks all reported, request-hash-bound approvals, fail-closed, latched locks |
| Broker adapter + MT5 + FakeBroker | `app/brokers` L10 | demo-fenced on connect; the only venue path |
| Position policies | `app/positions` L21 | 7 exit policies, priority-ordered, trailing that only ratchets |
| Symbol mapping and precision | `app/symbols` L11 | one canonical normalizer; ambiguity refused |
| Validation methodology | `tools/rule_search.py` L26 | permutation null, era blocks, walk-forward, date clustering, unit fences |
| Worker base | `app/workers` L02 | the orchestrator runs on this loop, not a new one |
| Auth, sessions, permissions, audit | `app/auth` L04 | the orchestrator's routes sit behind `require_permission` |

### KEEP + MODIFY

| Capability | Change | Why |
|---|---|---|
| Strategy resolution in the three runners | add a resolver so a **stored definition** can be run, not only a registered class | see §3 — this is the load-bearing gap |
| `strategy_versions` | additive column for the §32 lifecycle state | `draft/validated/retired` cannot express BACKTESTED or PAPER |
| Backtest metrics | add Sharpe and Sortino with their assumptions stated | brief §14 names both; the module has the rest |
| `PaperEngine` | an injection point for an externally supplied signal | `process()` takes bars and derives the signal itself, so a webhook alert has no seat. A8 only |
| `.env.example` | `TV_INBOX_DIR`, and the existing `TV_WEBHOOK_SECRET` documented for the server path too | |

### ADD

| Capability | Level | Note |
|---|---|---|
| Source discovery + inbox | A0 | no second webhook handler, no second CSV parser |
| Pine parser and declared subset | A1 | the one genuinely new body of work |
| Spec persistence + hashing + versioning | A2 | `tv_strategy_sources`, additive migration |
| Definition resolver (the bridge) | A3 | unblocks L13 as well as every compiled strategy |
| StrategyCompiler + test generator | A4 | |
| AI interpreter seat | A5 | proposes mappings; never in the runtime path |
| Validation harness + report | A6 | wraps L14/L15/L26, adds the TradingView divergence |
| Orchestrator | A7 | the state machine |
| Alert → paper execution bridge | A8 | **blocked** on L18 and L19 |
| Machine-readable project state | A9 | `PROJECT_STATE.json`, brief §42 |

### REPLACE / REMOVE

None. Nothing found in the audit is unsafe, broken or incompatible, and no
duplicate implementation of a brief §9 component exists to merge. `tv_webhook.py`
and `app/webhooks/` look like a duplicate pair and are not: one is a standalone
operator tool with no database and no framework, the other is the server path.
Both are documented as such and both stay.

## 3. The gap the audit found

**All three runners resolve a strategy the same way:**

```python
strategy = self.registry.create(config.strategy_key, config.strategy_config)
```

— `app/backtest/service.py:180`, `app/replay/service.py:137`,
`app/paper/service.py:351`. `StrategyRegistry.create` looks a key up in a dict
of **classes registered at import time**. A `BuiltStrategy` is not a registered
class; it is an instance constructed from a definition, and `BuiltStrategy` is
referenced in exactly one place outside its own module —
`app/api/v1/strategy_builder.py:527`, for a preview.

So today **a saved L13 definition cannot be backtested, replayed or
paper-traded.** That is a real integration gap, and it is not a TradingView
gap: it already limits the visual builder that shipped at L13. Everything the
brief asks for after compilation — automatic backtest, automatic replay,
automatic paper — runs through those three call sites, which means the
compiler's output would be unrunnable the day it existed.

This is why **A3 comes before A4**. The project's own doctrine is to build the
veto before the path it guards; the same reasoning applied to plumbing says
build the runway before the aircraft. A3 is small — a resolver that accepts
either a registry key or a stored definition reference, one call site changed in
each of three services — and it is the highest-value item in the ladder because
five brief sections depend on it and one already-built level is waiting on it.

## 4. The Autonomous Trading Orchestrator

Brief §33. One component, one job: know what state every strategy is in, and
run the next stage when its predecessor has passed.

**Where it runs.** A worker on the L02 `Worker` base — the same loop, heartbeat
and cooperative shutdown every other worker uses, so the health check sees it
and a stop is not a kill. Not a new scheduler.

**What it holds.** No trading logic whatsoever. It calls the existing services
and records their verdicts. If the orchestrator were deleted, every stage would
still be individually runnable through its own API — which is the test of
whether the wiring stayed wiring.

**The state machine** (brief §32):

```
DISCOVERED → PARSED → NORMALIZED → COMPILED → VALIDATED → BACKTESTED
          → REPLAYED → PAPER → APPROVED → DEPLOYED → MONITORED
                                                   ↘ PAUSED / RETIRED
```

Rules the machine enforces, each of them a refusal rather than a convention:

1. **No skipping.** A transition whose predecessor is not recorded as passed is
   refused. There is no `force` parameter.
2. **No jump to live.** `APPROVED` requires an operator action carrying a user
   id; `DEPLOYED` is `DEPLOYED (paper)` unless every live gate is true, which
   they are not. The orchestrator has no code path that writes `live`.
3. **Every transition is an audit row** (brief §36): timestamp, component,
   old state, new state, decision, reason, files or artifacts, tests run, test
   result, risk level. Written to the existing `audit_logs` table, which L04
   already writes every security action to.
4. **A failure stops that strategy, not the loop.** A worker that dies stops
   watching; the L02 base already logs a failing tick and continues.
5. **Backwards transitions are explicit.** A re-dropped source with a changed
   hash starts a *new version* at DISCOVERED; it never moves an existing
   version backwards.
6. **PAUSED is reachable from anywhere and needs no reason.** Stopping is never
   harder than starting.

**The decision logic** the brief sketches in §33, made concrete against what the
audit found:

| Condition | Action |
|---|---|
| a Pine source exists and the parser supports it | parse; else record `unsupported_features` and stop at DISCOVERED |
| the indicator is one of the four | reuse `app/strategies/indicators.py` |
| the indicator is not | **refuse**, and name the four. Do not add an indicator to make a strategy compile |
| the strategy engine exists | reuse L12 — it does |
| the risk engine exists | integrate L17 — it does, and the OMS cannot be called without its `Approval` token |
| the MT5 adapter exists | reuse L10 — it does |
| a direct TradingView → MT5 path exists | none exists (§1) |
| sizing is incomplete | A8 blocks; A0–A7 do not depend on it |

## 5. The autonomy ladder

Brief §43: not in one operation. Each level is AUDIT → PLAN → MODIFY → ADD →
INTEGRATE → TEST → VERIFY → DOCUMENT → UPDATE STATUS, all three suites before
and after, both counts recorded in `PROJECT_PROGRESS.md`.

| Lvl | Work | Depends on | Blocked? |
|---|---|---|---|
| **A0** | Source discovery, capability inventory, `data/tradingview/` inbox | L09 ✓, `tv_import` ✓ | no |
| **A1** | Pine lexer/parser → `TradingViewSpec`; the declared subset; refusal on everything else | A0 | no |
| **A2** | Spec persistence, source hashing, version minting (additive migration) | A1, L05 ✓ | no |
| **A3** | **Definition resolver** — stored definitions runnable in backtest, replay and paper | L12 ✓, L13 ✓ | no |
| **A4** | `StrategyCompiler` + preservation report + generated tests | A2, A3 | no |
| **A5** | AI interpreter seat: LLM proposes mappings, deterministic validator decides | A1 | no |
| **A6** | Validation harness: G4–G7, the report, the TradingView divergence, Sharpe/Sortino | A4, L14 ✓, L15 ✓, L26 ✓ | no |
| **A7** | Orchestrator: state machine, audit rows, worker, API | A6, L02 ✓ | no |
| **A8** | Alert → paper execution bridge (signal consumer, idempotent) | **L18, L19** | **yes** |
| **A9** | `PROJECT_STATE.json` + autonomy health states, wired to L29/L37 | A7 | no |

A0–A7 and A9 can proceed against the repository as it stands. **A8 cannot**:
brief §20 requires the 13-state order machine with idempotency and
reconcile-before-retry, which is L19 and currently PARTIALLY COMPLETE, and
sizing modes beyond fixed quantity, which is L18. Building A8 first would mean
either a second OMS (brief §9 forbids it) or an execution path predating the
state machine that guards it.

Sequencing across the two ladders, then, is: **L18 → L19 → A0…A7, A9 → A8 →
L38**. L38 (startup and disconnect reconciliation) remains CRITICAL and remains
ahead of anything that touches a real venue.

## 6. Where autonomy stops, and why that is the design

The brief authorizes automating engineering work and explicitly withholds
authority to bypass safety controls (§40). Three boundaries follow, and none is
a limitation of the implementation:

- **Autonomy ends at PAPER.** G1–G8 run unattended; APPROVED requires a person.
- **Live stays refused.** `TRADING_MODE=paper`, `LIVE_TRADING=false`, seven of
  ten `LIVE_GATES` false. No validation result, no orchestrator transition and
  no configuration reachable from this chain changes any of them.
- **The AI never executes.** The `AiFilter` seat can only decline or lower
  confidence, it runs before risk and cannot see it, and the LLM interpreter
  operates at build time on source text, never at runtime on a signal.

And one boundary the brief does not mention but this repository has earned:
**a validated strategy is not a good strategy.** Everything measured in this
project across 41 candidates, 10 families, 8 exits, 4 universes and 3
timeframes has failed to separate from its own permutation null. A pipeline
that turns a TradingView strategy into a paper deployment in one unattended
pass is a fine piece of engineering and is not evidence about the strategy. The
validation report says so in those words, so that the number a strategy arrives
with never becomes the number it is trusted on.
