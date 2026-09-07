# ARCHITECTURE_MIGRATION.md

Current architecture, target architecture, and the decision recorded for every
component. Rewritten 2026-09-02 after Levels 00–05, 11, 21 and 29.

Method: **adapter → refactor → migration**, never delete → rebuild. Nothing in
`tools/` has been deleted or rewritten, and every command in `README.md` and
`NIGHTLY.md` still works.

---

## 1. Current architecture

```
RESEARCH TOOLKIT (CLI, unchanged, 24 modules)
  data:      market.py (yfinance+cache) · edgar.py (SEC) · crypto_market.py (ccxt)
  features:  indicators.py · score.py
  contract:  snapshot.py -> five Claude agents -> verify.py (sourcing gate)
  research:  rule_backtest.simulate -> rule_search · exit_search · bracket_sweep
             cost_hurdle · cost_profile · swap · patterns · backtest
  execution: mt5_paper.py (ONLY order sender, demo-fenced)
                 ^-- take_profit.py (harvest) <-- run_overnight.py (launcher)
  reading:   mt5_account.py (read-only) -> track_record.py -> data/*.jsonl

PLATFORM (new, beside it)
  frontend (Next.js 16, 20 routes)
        |
      nginx
        |
      api (FastAPI)
        |- /v1 (25 tag groups; real reads for orders, executions, trades,
        |       positions, symbols, specs, accounts, audit; 15 groups gated
        |       and answering 501 with the level that builds them)
        |- auth/ (Argon2id, server-side sessions, USER<TRADER<ADMIN)
        |- admin/ (users, roles, audit trail)
        |- services/ (domain layer; no Request, no cookie, worker-callable)
        |- realtime/ (29-event catalogue, channel authz, hub, WebSocket)
        |- marketdata/ (normalized Quote/Bar, validation, staleness,
        |               MT5 + yfinance + simulator adapters, bar cache)
        |- webhooks/ (TradingView gateway: auth, validation, idempotency,
        |             symbol + strategy mapping, Signal emission)
        |- brokers/ (BrokerAdapter, MT5Adapter, FakeBroker, per-account
        |            registry, order validation, reconciliation report)
        |- strategies/ (Strategy contract, registry + factory, engine;
        |               the 3 live rules wrapped, never copied; the
        |               declarative definition + its interpreter)
        |- backtest/ (config with every assumption reported, prefix-walk
        |             signal builder, metrics, async jobs; wraps simulate())
        |- core/ (settings+LIVE_GATES, JSON logs, health, readiness)
        |- symbols/ (source->internal->broker, broker contract specs,
        |            precision + status + the one pre-trade gate)
        |- positions/ (7 exit policies, close contract, paper executor, monitor)
        |- monitoring/ (drift, calibration, escalation)
        `- SQLAlchemy async -> PostgreSQL (40 tables, 6 migrations)
      redis (event bus; a critical health check from L07)
```

**The gap, stated plainly:** between a signal and a broker there should be six
stages. None exists. The platform cannot place an order.

## 2. Target architecture

```
TradingView ─┐
             ├─> Webhook Gateway -> Validation -> Idempotency
Strategy ────┘        |
                      v
                 Signal Engine
                      |
                 Strategy Engine
                      |
                   AI Layer          (may lower confidence; may never approve)
                      |
                  Risk Engine        (veto; nothing passes without approval)
                      |
                Position Sizing
                      |
            Order Management System  (intent -> submitted -> filled/rejected)
                      |
                 Broker Adapter
                      |
                  MT5 -> Broker
                      |
            Execution Confirmation   (a fill, never a quote)
                      |
                Position Manager     (built: L21)
                      |
                 Trade Journal
                      |
                   Analytics
                      |
                AI Monitoring        (built: L29)
                      |
                Model Training
```

## 3. Current → target component mapping

### Already in the target shape

| Existing | Target stage | Decision |
|---|---|---|
| `app/positions/policies.py` (7 exit types) | Position Manager | **KEEP** |
| `app/positions/executor.py` (confirmed/rejected/unknown) | Execution Confirmation | **KEEP** |
| `app/monitoring/` (8 checks, escalation) | AI Monitoring | **KEEP** |
| `app/symbols/` (three-name resolution + specs) | Symbol layer under Market Data | **KEPT; extended at L11** with precision, status and the trading gate |
| `app/auth/`, `app/admin/` | platform identity | **KEEP** |
| `app/core/settings.py` (`LIVE_GATES`) | safety gate for every stage | **KEEP** |
| `app/models/` (39 tables) | persistence for every stage | **KEEP** |
| 4 Alembic migrations | schema history | **KEEP** |
| Next.js shell, `Unavailable`, `ModeBadge`, `RouteGuard` | UI | **KEEP** |

### Toolkit modules the target reuses unchanged

| Existing | Target stage | Decision | Reason |
|---|---|---|---|
| `rule_backtest.simulate()` | Backtesting core | **KEPT, unchanged, wrapped at L14** | Next-bar-open entry, loss booked on an ambiguous bar, swap-aware. `app/backtest/runner.py` calls it and a test asserts it defines no second simulate |
| `rule_search.py`, `exit_search.py`, `bracket_sweep.py` | Backtesting + Model Validation | **KEEP** | Permutation nulls, era blocks, walk-forward, date clustering. This is the promotion gate |
| `cost_hurdle.py`, `cost_profile.py`, `swap.py` | Cost model | **KEEP** | Spread hurdle and financing in convertible units |
| `patterns.py`, `backtest.py` | Analytics / research | **KEEP** | BH-FDR and sign-flip detection |
| `indicators.py`, `score.py` | Feature engine | **KEEP** | Parity-tested |
| `market.py`, `edgar.py`, `crypto_market.py` | Market Data providers | **KEPT, wrapped at L08** | `app/marketdata/providers/` calls them; the fetchers are unchanged. `validate_ohlcv`'s per-ticker report is surfaced rather than recomputed |
| `trade_stats.py` | Analytics | **KEEP** | One stats engine for both trade sources |
| `tv_import.py` | Journal importer | **KEEP** | Column sniffing that refuses rather than guessing |
| `verify.py`, `snapshot.py`, five agents | Market Intelligence | **KEEP** | The sourcing gate |

### Toolkit modules that change shape

| Existing | Target stage | Decision | Reason |
|---|---|---|---|
| `mt5_paper.connect`, `mt5_account.connect`, `rule_backtest.connect`, `symbols/sync_mt5.connect` | Broker Adapter | **MERGE** | Four copies of one routine. One adapter, four callers |
| `mt5_account.*` readers, deal pairing | `get_account`, `get_positions`, `get_orders`, `get_order_history` | **KEEP + WRAP** | Logic is correct; only the interface is missing |
| `mt5_paper.place()` construction (`filling_for`, absolute SL/TP, MAGIC, deviation) | OMS order building | **KEEP + REFACTOR** | Bug-hardened by three live defects. Keep verbatim, add intent state and idempotency |
| `mt5_paper.place()` unknown-send handling | OMS + reconciliation | **KEEP + MODIFY** | Currently treats unknown as rejected. Correct for an open; must reconcile at L19 |
| `mt5_paper.bracket_is_sane` + repair | Execution Confirmation | **KEEP + MOVE** | Already checks the fill, not the quote |
| `mt5_paper.lot_for_risk` | Position Sizing | **KEEP + MOVE** | Refuses on missing tick data; step rounding fixed and tested |
| `mt5_paper.assert_demo` | Risk Engine fence | **KEEP + MOVE** | Stays a code-level gate no setting can override |
| `mt5_paper.server_now/history_end/server_day_start` | Risk clock | **KEEP + MOVE** | Timezone reasoning is subtle and documented |
| `mt5_paper.RULES`, `rule_backtest.signals_*` | Strategy Engine | **KEPT; wrapped at L12** | `app/strategies/rules.py` calls the live functions rather than reimplementing them, and a regression test asserts the wrapper agrees on every series. `signals_*` untouched; `test_indicators_match_live` still passes |
| `rule_search.build_candidates()` (41 families) | Strategy registry, research tier | **KEEP** | Registered as research-only until a search report is attached |
| `rule_search.make_*` factories | Strategy Builder | **KEPT, untouched** | L13 took a different route: the builder produces a declarative definition walked by a fixed evaluator, rather than parameterising these factories. The factories stay as the research search's own machinery, which is what they are |
| `take_profit.harvest`, `net_floating` | Position Manager policy | **KEEP + REFACTOR** | Becomes a policy object beside the seven built at L21 |
| `take_profit.run`, `run_overnight.py` | Bot runner + config | **REFACTOR** | Same settings; add PID, heartbeat, stop file, restart |
| `track_record.py` merge + significance | Trade Journal + Analytics | **KEEP + MOVE** | Idempotent merge is exactly the journal contract |
| `tv_webhook.py` | Webhook Gateway | **KEPT; gateway added at L09** | Auth and redaction were correct and are reused as rules, not copied code. `app/webhooks/` adds schema validation, the timestamp check, replay protection, idempotency, symbol and strategy mapping and Signal emission. The CLI receiver is unchanged and still runs |
| `tests/test_rule_backtest.FakeMT5` | Paper broker | **KEEP + MOVE** | The test double becomes the runtime simulator |
| `Desktop\start-trading.bat` | launcher | **KEEP → REPLACE** | Only after a supervised runner exists |

### Missing entirely — ADD

| Component | Level | Depends on |
|---|---|---|
| ~~Backend API surface (24 route groups)~~ **built at L06** | 06 ✓ | 05 ✓ |
| ~~Realtime: Redis event bus + WebSockets~~ **built at L07** | 07 ✓ | 06 ✓ |
| ~~Unified market data provider~~ **built at L08** | 08 ✓ | 11 ✓ |
| ~~Webhook Gateway with replay protection~~ **built at L09** | 09 ✓ | 06 ✓, 07 ✓ |
| ~~**BrokerAdapter + MT5Adapter + FakeBroker**~~ **built and tested at L10** | 10 ✓ | 11 ✓ |
| ~~Strategy interface, registry, Signal~~ **built at L12** | 12 ✓ | 10 ✓ |
| ~~Visual strategy builder~~ **built at L13** | 13 ✓ | 12 ✓ |
| Market replay driver | 15 | 10, 12 |
| Paper trading through the pipeline | 16 | 10, 12, 17, 18, 19 |
| **Risk Engine + kill switches** | 17 | 10 |
| Position sizing module | 18 | 11 ✓, 17 |
| **OMS with idempotency and reconciliation** | 19 | 10, 17, 18 |
| Automated execution wiring | 20 | 19 |
| Bot manager and workers | 22 | 07, 20 |
| AI data pipeline, models, training, integration, registry | 23–28 | 31, 26 |
| Portfolio, journal, analytics services | 30–32 | 06 |
| AI trade review | 33 | 31 |
| Notifications, Discord | 34, 35 | 07 |
| Admin control centre | 36 | 04 ✓, 06 |
| System monitoring, recovery | 37, 38 | 06, 10 |
| Security hardening | 39 | 06 |
| Integration testing, deployment, final audit | 40–42 | all |

### REMOVE

**Nothing.** No component in this repository is scheduled for removal. The
one replacement (`start-trading.bat`) happens only after its successor works,
and the file stays until then.

## 3a. API conventions, settled at L06

1. **One version, two spellings.** `/v1` on the application; `/api/v1` in the
   browser, because nginx serves the API under `/api/` and strips it. Paths
   are relative to the API root on both sides, which is where they already
   were, so no proxy or Compose change was required.
2. **An alias is a second path, never a second implementation.** The pre-v1
   `/auth/*` and `/admin/users` are the same router objects mounted twice. A
   test asserts both paths answer identically, so a divergence is a failure
   rather than a discovery.
3. **Health is infrastructure, not API.** `/health*` stays at the root because
   the Compose healthcheck and nginx probe it there. `/v1/system/health` reads
   the same functions.
4. **A collection is never unbounded, and never silently truncated.** Limit
   defaults to 50, caps at 200, and a request above the cap is refused naming
   the cap.
5. **A query string cannot reach SQL.** Sort and filter fields are keys into a
   dict of ORM columns; an unknown key is refused with the allowed list.
6. **A 501 sits behind its real gate.** Every unbuilt group is registered with
   the authorization it will have, so the gate is enforced and tested before
   the body exists — and it names the level that builds it.
7. **The idempotency key is validated now and stored at L19.** Nothing records
   it today, because a stored key implying replay protection that does not
   exist is worse than no key at all.

## 3b. Realtime conventions, settled at L07

1. **The catalogue owns the routing.** A type's scope decides its channel, so
   a publisher cannot put a private fill on a public channel by passing the
   wrong argument.
2. **Authorization reads the database on every subscribe.** Never the frame,
   never a cache on the connection. An unowned id and a missing id answer
   identically, so the endpoint is not a membership oracle.
3. **The socket has no publish verb.** Delivery is one-directional; the only
   client verbs are subscribe, unsubscribe and ping.
4. **The bus is a nudge, not a record.** The database and the broker stay
   authoritative. A consumer that needs a value reads it back.
5. **Duplicates are expected.** Redis can redeliver and clients replay
   subscriptions on reconnect, so consumers deduplicate by event id *and*
   check their own domain state.
6. **A realtime failure never becomes a trading action.** Silence is not a
   signal, and no path exists from a dropped frame to an order.

## 3c. Market-data conventions, settled at L08

1. **Absent is None, never zero.** A field a provider does not supply carries
   an explicit availability (AVAILABLE / NOT_AVAILABLE / DERIVED). A recorded
   zero spread is an unrecorded spread, not a free trade.
2. **Validation flags; repair is explicit.** `inspect_series` measures and
   changes nothing. The quality report travels with the bars, because a report
   nobody reads is a report that did not happen.
3. **Staleness scales with the timeframe**, and a closed market is not a stale
   feed. An unknown session is treated as open.
4. **A stale or invalid feed never triggers an action.** It is a fact attached
   to the answer; the veto lives in the risk engine.
5. **The broker's clock is measured, not assumed.** MT5 renders the server
   wall clock as a UTC epoch; the offset is measured per connect and removed,
   and a feed too quiet to measure it is refused rather than guessed at.
6. **Failover is opt-in and named.** Bars from two providers are never merged,
   and a substitution is reported in the response.
7. **`provider` is part of a bar's identity.** Two providers' "EURUSD H1" are
   different measurements of different books and are never averaged.
8. **TradingView is not a market-data provider.** It is an alert path (L09).

## 3d. Webhook conventions, settled at L09

1. **An unset secret refuses everything.** The CLI tool may warn and continue;
   a server endpoint may not.
2. **Only listed actions are executable words.** Arbitrary text is never read
   as a trading command, and CLOSE means flat, not sell.
3. **Payload risk parameters are recorded and never obeyed.** Size comes from
   Risk -> Sizing, never from the sender.
4. **Idempotency ignores arrival time.** The key is the sender's id, or a hash
   of what makes the alert *that* alert. Including receipt time would make
   every retry unique.
5. **A missing timestamp is refused, never defaulted.** A default makes every
   replay look fresh.
6. **Rejections are stored.** "Never received" and "received and refused" are
   different answers.
7. **The response vocabulary contains no trading status.** A 200 means
   recorded.

## 3e. Broker conventions, settled at L10

1. **A result is what the venue said.** `OrderStatus.unknown` is a first-class
   outcome, never folded into rejected, and it is resolved by reconciling
   against the venue rather than by retrying.
2. **Health is observed, not assumed.** `health()` reads the account, because
   the only way to know a link works is to use it. Connected-and-unreadable and
   connected-with-trading-disabled are both `degraded`.
3. **Reconciliation reports; it never repairs.** Closing an unexpected position
   closes somebody's manual trade; re-sending a missing one doubles it.
4. **Validation refuses; it never rounds.** Rounding up silently risks more
   than was budgeted, which undoes position sizing.
5. **One adapter per account, no global connection and no default.** A default
   is what a bug reaches for.
6. **The broker API surface is read-only.** A write has to be added in the
   level that adds the veto in front of it, not before.

## 3f. Symbol conventions, settled at L11

1. **Three names, none derived from another.** Source, internal and broker are
   separate strings resolved by table lookup. No suffix is ever added or
   removed to find a broker symbol.
2. **One normalizer.** Quantity and price rounding lives in
   `app/symbols/precision.py` and every layer imports it. Two implementations
   is how one signal sends two different volumes.
3. **Refuse, do not round.** A quantity off the step grid is refused by
   default and named; flooring is opt-in and always reported. A quantity below
   the venue minimum is never raised to it.
4. **Only `active` is tradable.** UNKNOWN is never read as ACTIVE, and
   `status_of` never raises so a broken symbol is visible rather than absent.
5. **One pre-trade gate.** `validate_for_trading` checks every condition at
   once and returns the spec, so no two callers assemble different subsets.
   Passing it is not authorization.
6. **Mappings are disabled, never deleted.** A mapping explains where a
   historical order went.
7. **Specs come from the venue, not from a form.** `spec_source` has to stay
   truthful, so the admin API cannot set contract terms.

## 3g. Strategy conventions, settled at L12

1. **A strategy creates a signal, never an order.** Nothing in
   `app.strategies` imports a broker adapter, risk, sizing or the OMS.
2. **No look-ahead, enforced by construction.** The forming bar is removed
   before a strategy sees the data; it cannot read what it does not have.
3. **Signals fire on a closed bar**, and the timing is declared per strategy
   rather than assumed, so a backtest and the live loop cannot silently
   disagree.
4. **An error is not a neutral signal.** A crashing strategy that returned
   HOLD would read as a quiet market.
5. **Existing rules are called, not copied**, and a regression test proves the
   wrapper agrees with the toolkit function it wraps.
6. **Randomness is seeded from the bar.** Same bar, same draw — otherwise a
   replay cannot reproduce a run.
7. **Every built-in is `research_only`.** Raising a tier is a deliberate act
   backed by a validation report, not a configuration change.
8. **No strategy asserts a confidence.** None of them has a measured basis for
   one, and an invented number reads downstream as evidence.

## 3h. Builder conventions, settled at L13

1. **A definition is data, walked by a fixed evaluator.** It never becomes
   source, and `code_ref` is one constant string for every built strategy.
2. **Operands must be the same quantity to be compared.** `PRICE > RSI` is
   refused; a constant is compatible with anything.
3. **Unknown fields are refused, not dropped.** A silently ignored field means
   the user is running a strategy they did not write.
4. **A validated version is immutable.** Editing one creates a new version, so
   a recorded signal can always be explained by reading its version.
5. **The lifecycle refuses states nothing enforces**, and names the level that
   would.
6. **What is stored round-trips.** Derived views belong in the API response,
   not in the persisted definition.
7. **The server validates.** Client-side checks are hints.
8. **Nothing pretends.** A backtest that has not run says NOT RUN.

## 3i. Backtesting conventions, settled at L14

1. **One engine.** `tools/rule_backtest.simulate` is it. The runner adapts a
   Strategy into its input and never reimplements its semantics.
2. **The signal vector is built from prefixes.** At bar i the strategy sees
   `bars[:i+1]`. Computing once over the whole array is faster and is how
   look-ahead gets in.
3. **No zero-cost default.** The spread must be stated. Cost drag is the one
   effect this project has measured large enough to see.
4. **Slippage is always adverse.** A slippage model that could help is one that
   flatters.
5. **A metric that cannot be computed reports NOT_AVAILABLE**, with the reason.
   Sharpe and Sortino are per trade, never silently annualised.
6. **Every assumption is in the report**, with a fingerprint two identical
   configurations share.
7. **A failed run is never `finished`.** Status is written before the work
   starts.
8. **Corrupt bars refuse the run.** A backtest over impossible OHLC
   manufactures findings.

## 3j. Replay conventions, settled at L15

1. **Simulated time is the dataset.** `now()` is the timestamp of the bar last
   revealed. The machine's clock is consulted in exactly one function —
   `pacing_delay()` — and cannot reach a trading decision.
2. **Speed paces the sleep and nothing else.** `advance()` never consults it,
   so 1x and 100x produce identical trades. A test asserts it.
3. **An incremental engine must be proved equal to the batch one.** The trade
   lists must match exactly, over several dataset lengths and with financing
   on. Where the two could reasonably differ, the batch engine's answer wins
   and the reason is written at the code.
4. **A shared cost model means a shared request body.** `/v1/replay/sessions`
   imports L14's `CostsIn`. A field on one endpoint and not the other is a
   silent divergence, not a difference anyone would notice.
5. **Simulated execution is structural, not configured.** The package holds no
   broker adapter and imports none, so no setting can point a replay at a
   venue. A test parses every module rather than grepping it.
6. **Session state uses the schema's existing vocabulary.** A parallel set of
   names would need translating at every boundary.
7. **Illegal transitions are refused, never ignored.** A completed session that
   could return to running would produce a second, different result under one
   id.
8. **Sessions live in the backend.** The frontend controls one; it does not
   host one. Closing the browser does not stop a replay.
9. **Corrupt bars refuse the session.** L08's validator runs before the first
   bar is revealed, and the refusal names the count.

## 3k. Paper-trading conventions, settled at L16

1. **The execution provider is a function of the mode and nothing else.**
   `provider_for(mode)` takes no settings, credentials or override, so there is
   no parameter through which configuration could change the route. A test
   asserts the signature is exactly `["mode"]`.
2. **`PROVIDER_FOR_MODE` is total over `ExecutionMode`.** No default, because a
   default here would be a default execution path.
3. **An order cannot exist without a `risk.Approval`.** `PaperOMS.submit` takes
   one as its first positional argument and `RiskEngine.approve` is its only
   producer. Manual orders included; there is no branch that skips it.
4. **The AI seat runs before risk and can only decline.** It cannot see the
   risk engine, the kill switches or the OMS, so no ordering lets it overturn a
   veto.
5. **Domain time is aware, storage time is naive.** `app/paper/clock.now_utc`
   for anything compared with market data; `app.auth.models.utcnow` for a
   `DateTime` column; an explicit conversion at the boundary.
6. **One unified execution record.** `mode='paper'` on the existing `orders`,
   `positions` and `trades` tables, not a parallel `paper_*` schema. A fill from
   the simulator carries `fill_source='simulator'`, never `'broker'`.
7. **The signal key IS the order's intent id**, and `orders.intent_id` is
   unique. A duplicate signal and a duplicate order are one fact, and the
   database enforces what the process enforces.
8. **One pass is one transaction.** Order, events, execution, position, trades
   and balance are written together or not at all.
9. **The spread is charged by filling at the correct side of the book**, once
   per round trip. A modelled book is stamped `bid_ask=not_available` and never
   reads as measured.
10. **Money arithmetic refuses a missing contract term.** `tick_value /
    tick_size` is measured; absent, the calculation is refused rather than
    defaulted.
11. **A kill switch never closes a position.** It stops new orders and stops
    the bots it covers; closing would be trading a decision nobody made, at the
    moment something is known to be wrong.
12. **A paper record is archived, never hard-deleted.** A reset restores the
    capital and leaves the history in place.

## 3l. Risk conventions, settled at L17

1. **Every limit appears in every decision.** A limit that is absent cannot be
   told apart from one that passed. A completeness pass adds an
   `enforced=False` record for anything no configuration set, so a limit added
   later is covered without anyone remembering.
2. **An approval is bound to its order and expires.** `request_hash` digests
   the ten fields that make the order what it is; changing any of them requires
   a fresh evaluation. An approval computed against a portfolio that has moved
   is not evidence about now.
3. **A breach latches into a state.** A check is recomputed from a snapshot and
   a snapshot moves; a lock does not. Locks are persisted and read back before
   the service evaluates anything.
4. **No dangerous state clears itself.** Only a daily lock clears, and only
   when its trading day has ended. Every other release is authorised and names
   who authorised it.
5. **The trading day is explicit UTC** with a configured boundary hour, never
   the machine's local midnight.
6. **The most restrictive limit wins, not the most specific.** A cap combines
   by minimum, a floor by maximum, a restricting boolean by OR. A more specific
   scope can tighten and can never loosen.
7. **Configuration is refused, not clamped.** An out-of-range percentage or a
   negative cap is an error; normalising one produces a system trading under
   limits nobody chose. An unknown limit name is refused too.
8. **An approval reserves.** Exposure is held until the order resolves or the
   approval expires, so two concurrent orders cannot take the same headroom.
9. **Failure is closed.** A raising check, an unloadable configuration and a
   failed write all refuse. An approval nobody can audit is not an approval.
10. **Risk runs in every mode**, and in a simulation `now` is the SIMULATED
    time — measured against the wall clock, every historical signal is stale.
11. **A preview is a different method, not a flag.** `check` cannot reserve,
    latch or record, so it cannot become an execution by argument.
12. **A rejection names itself.** `RejectionCode` is closed and total over
    `LimitKind`; a caller never receives "risk rejected".

## 4. Important architectural decisions

1. **The research stance is the safety layer.** The toolkit's refusals — demo
   only in code, dry-run default, "the receiver records and never trades" — are
   the target's Step 13 already built. They are kept and moved, not relaxed.
2. **Three modes, not two.** PAPER is the internal simulator, DEMO is the MT5
   demo account at the real broker, LIVE is a real account that does not
   exist. The 252 historical trades are DEMO. Every execution row carries
   `mode` so they can never be pooled.
3. **Live is unreachable by configuration alone.** Ten named gates in
   `LIVE_GATES`, all false, listed by `/health`. A test fails if any is
   flipped outside the level that builds it.
4. **Refuse rather than default.** Missing contract specs, unknown floating
   P&L, samples too small to measure: each returns a refusal naming what is
   missing. This is `lot_for_risk`'s rule generalised.
5. **"Not measured" is not "fine."** Monitoring has `insufficient_data` as a
   severity distinct from `ok`, and it is never actionable.
6. **Contract specs belong to the broker, not the instrument.** They live on
   the mapping row. Measured proof: DE40 has contract size 1 and minimum
   volume 0.1 where the pairs have 100,000 and 0.01.
7. **The AI layer is defined as pass-through until evidence exists.** It may
   lower a signal's confidence; it may never place, size or approve. No model
   exists, and the validation harness is the gate for any that arrives.
8. **asyncpg, not psycopg**, because the MT5 worker must run on Windows and
   psycopg's async mode refuses uvicorn's Proactor loop there.
9. **The MT5 adapter cannot be containerised on Linux.** It runs on the
   Windows host with the terminal and speaks to the rest over Redis and the
   database.

## 5. Migration dependencies

```
05 ✓ ── 06 ── 07 ── 22
         │      └── 34 ── 35
         ├── 36
         └── 30/31/32 ── 33
11 ✓ ── 08
   └── 10 ── 12 ── 13
         │     └── 15
         ├── 17 ── 18 ── 19 ── 20 ── 16
         │                      └── 21 ✓ (demo path)
         └── 38 ── 37
                    └── 39 ── 40 ── 41 ── 42
23 ── 24 ── 25 ── 26 ── 27 ── 28 ── 29 ✓
```

**L10 landed on 2026-09-03**, unblocking the seven levels behind it. The
adapter reads and reconciles; it has no route that can write, and the OMS that
would call `place_order` is L19 — which in turn waits on the Risk Engine at
L17, because the veto is built before the path it guards.

## 6. Breaking-change watch list

1. Moving toolkit files breaks `sys.path` imports and every skill command.
   Mitigation: shims in `tools/` for one release, tested under both paths.
2. Adding `intent_id` to order-log rows: readers must keep tolerating rows
   without it (`track_record.order_geometry` already does).
3. Decomposing `cycle()`: the `skipped_*` and `no_signal` status strings are
   read by humans in the overnight logs. Keep them.
4. Any change to `assert_demo`, `bracket_is_sane`, `lot_for_risk`,
   `filling_for`, `simulate` or `server_day_start` runs
   `tests/test_rule_backtest.py` before and after.
5. `server_day_start` is timezone-sensitive; its test must run on a non-UTC
   machine or mock the offset.
6. Auto-mode classifier refusals on backgrounded `--live` launches are
   operational and unchanged by any of this. `NIGHTLY.md` remains the
   procedure.
7. Adding a lifecycle state to `strategy_versions` must be additive. The
   existing `draft/validated/retired` values stay valid and keep their meaning;
   a second column carries the A7 state, because overloading `status` would
   make every existing row claim a lifecycle position it never had.
8. Changing the three `registry.create()` call sites (A3) touches backtest,
   replay and paper at once. All three keep accepting a registry key
   unchanged; the resolver adds a second accepted form. A test asserts the key
   path still works before the definition path is added.

## 3m. TradingView autonomy conventions, settled at the autonomy audit (2026-09-03)

Full design in `TRADINGVIEW_ARCHITECTURE.md`. The conventions other levels need
to know about:

- **Two strategy contracts, both kept.** `TradingViewSpec` is faithful to the
  source and records what cannot be run; `StrategyDefinition` (L13) is the
  platform subset and is the only one anything executes. Neither is replaced by
  the other. `StrategyDefinition` is unchanged by this work.
- **The compiler targets the existing interface.** A TradingView strategy
  becomes a `strategy_versions` row with
  `code_ref = "app.strategies.built:BuiltStrategy"` and the definition in
  `config` — the same shape the L13 builder already writes. There is no second
  strategy engine, no second evaluator and no generated code.
- **Brackets are not strategy rules.** Pine's `strategy.exit(stop=, limit=)`
  becomes bracket configuration for the L21 position manager, not a field in
  the definition. One position, one exit authority.
- **Declared sizing is recorded, never obeyed** — the L09 rule for an alert's
  advisory quantity, extended to Pine's `default_qty_type`. Sizing is Risk →
  `app.sizing`, which refuses on missing tick data.
- **The indicator catalogue does not grow to make a script compile.** Four
  indicators, called from the toolkit. An unsupported `ta.*` is a refusal.
- **The LLM operates at build time only**, proposing mappings that the
  deterministic parser and compiler still have to accept. No LLM is in any
  runtime signal path, and the runtime `AiFilter` seat can only decline.
- **Autonomy ends at PAPER.** G1–G8 run unattended; APPROVED needs an operator
  action carrying a user id, and no orchestrator transition can write `live`.
- **`data/tradingview/` only ever gains files.** Standing rule 3 holds: a
  processed source is moved between subdirectories, never edited.


---

## Position sizing, as built (L18, 2026-09-03)

```
signal -> AI seat (may only decline)
       -> value_per_price_unit(spec)        one conversion, shared
       -> bracket (entry, stop, target)
       -> PositionSizingEngine              app/sizing/calculate  <- PURE
            direction validated
            budget -> risk_per_unit -> raw quantity
            floor to venue step (app/symbols/precision, shared)
            below venue minimum -> REFUSE (never raised to it)
            actual risk recomputed and compared at full precision
       -> RiskEngine.approve                the ONLY producer of an Approval
       -> PaperOms.submit(approval, ...)    Approval is its first argument
       -> execution -> position -> trade
```

**The boundary that makes this hold is a type, not a convention.** `submit`
cannot be called without an `Approval`, and `Approval` has no constructor path
in this codebase other than `RiskEngine.approve`. Sizing therefore *cannot*
reach the OMS on its own, whatever a future caller intends.

**Dependency direction.** `app/sizing` imports `app.symbols` and nothing else
from the platform — not `app.brokers`, not `app.paper`, not `app.risk`. A test
parses every module in the package and fails the build if that changes. The
engine receives *normalized* broker constraints (`ContractSpec`), which is what
keeps it broker-independent: it has never seen MetaTrader5 and cannot.

**Where sizing runs.**

| Path | Sizing | Note |
|---|---|---|
| Paper bots | `app/sizing/calculate` | since L16; L18 added the bracket levels so direction is validated |
| Backtests | `app/sizing/calculate` | L18. Equity walked forward, stop distance from `atr[entry-1]` |
| Replay | fixed quantity | refused rather than faked; see `LEVEL_18_POSITION_SIZING.md` §3.5 |
| API preview | `app/sizing/calculate` | `POST /v1/position-sizing/calculate`, creates no order |
| Frontend | none | the browser computes nothing; every figure is the backend's |

One engine, four callers, zero second formulas.


---

## The order lifecycle, as built (L19, 2026-09-03)

```
Approval (only RiskEngine.approve makes one)
   |
   v
OrderManager.create      intent            <- persisted
   |
   v
OrderManager.submit      submitting        <- persisted BEFORE the venue call
   |
   v
BrokerAdapter.place_order
   |
   +-- accepted ------->  submitted -> accepted -> (partially_)filled
   +-- rejected ------->  submitted -> rejected
   +-- BrokerError ---->  failed             the venue never saw it
   +-- anything else -->  unknown            it may have
                             |
                             v
                        reconcile()          asks the venue; NEVER re-sends
                             |
              +--------------+--------------+
              v              v              v
           filled        cancelled        failed
                                       (the ONLY state a fresh
                                        order for this intent
                                        may follow)
```

**Twelve states, one machine.** `app/oms/state.py` holds it; `app/paper/oms.py`
imports and re-exports it. A test asserts the objects are literally identical,
because two machines that disagree about whether `accepted -> cancelled` is
legal is how a cancel succeeds in paper and corrupts an order in demo.

**One OMS, controlled by mode.** There is no `DemoOMS` and no `LiveOMS`. Demo
and live differ only in which adapter an operator registered against the
account.

| Path | Lifecycle | Venue |
|---|---|---|
| Paper bots | shared state machine + fill accounting | in-process execution provider |
| `POST /v1/orders` | `OrderManager` | the account's registered adapter |
| Demo / live | `OrderManager`, unchanged | MT5 adapter, once registered |
| Backtest / replay | neither | no OMS at all; they simulate |

**What cannot happen, by construction rather than by discipline:**

- an order without an `Approval` -- `create` will not take anything else;
- an approval reused for a different order -- `request_hash` binds it;
- a second order for one intent -- `by_intent`, `guard_resend`, and
  `orders.intent_id UNIQUE`;
- a retry out of `unknown` -- its exit set contains no sendable state;
- a filled order returning to pending -- terminal states have no outgoing
  edges at all;
- a requested quantity read as a filled one -- `filled_quantity` moves only
  when an execution is recorded, and `average_fill_price` is `None` rather
  than 0 until one is.


---

## Automated execution, as built (L20, 2026-09-03)

```
TradingView alert                       strategy over bars
      |                                        |
      v                                        v
Webhook gateway (L09)                  PaperEngine (L16)
      |  writes a `signals` row               |  produces its own signal
      |  publishes SIGNAL_CREATED             |
      v                                        |
ExecutionWorker (L20)                          |
      |  claims the row, FOR UPDATE SKIP LOCKED|
      v                                        |
ExecutionPipeline  <---- one Outcome vocabulary ---->  the same gates
      |
      +-> validate the signal        untrusted external input
      +-> validate the strategy      exists / enabled / not paused
      +-> AI seat                    advisory; may ONLY decline
      +-> app.sizing                 the only producer of a quantity
      +-> app.risk                   the only producer of an Approval
      +-> app.oms                    the only path to a venue
              |
              v
        BrokerAdapter -> MT5
```

**Two pipelines, one set of gates.** `app/paper/engine.py` drives bars and
runs a strategy; `app/execution/pipeline.py` drives a recorded signal and runs
no strategy at all. Neither computes a risk limit, a quantity or a fill. Both
report in `app/execution/outcome.Outcome`, which is what makes their counters
addable -- and is why that enum was extracted rather than copied.

**The gap this closed.** `SIGNAL_CREATED` was published by the gateway (L09)
and by the strategy engine (L12) and consumed by nothing, which both modules
said in their own docstrings. It now has exactly one consumer.

**Where the browser is.** Nowhere in the execution path. `/v1/execution` is a
control plane: start, stop, status. There is no route that takes a payload and
trades it, so a recorded signal is the only way in.

| Property | Mechanism |
|---|---|
| No duplicate execution | row status, then `seen`, then `guard_resend`, then `intent_id UNIQUE` |
| No blind retry | `execution_unknown` does not consume the signal; the OMS refuses a second order for the intent |
| No browser dependency | `app.workers.Worker` with a heartbeat, started by an operator |
| Traceable | one `execution_id` on the signal, the sizing snapshot, the order and every log line |
| Fails closed | every stage that raises becomes a recorded refusal, never a pass |


---

## Position management, as built (L21, 2026-09-04)

```
                        market data
                             |
                             v
                     PositionMonitor          a supervised Worker;
                             |                a closed browser stops nothing
                             v
                      PositionManager
                             |
        +--------------------+--------------------+
        |                    |                    |
   stop movers          the policies          protection gap
   (trail, break-even)  (7, in PRIORITY)      (intended vs venue)
        |                    |                    |
   MOST PROTECTIVE      first by priority     recorded, every pass
   proposal wins             |
        |                    v
        +-------------> ExitDecision  (quantity=None means all of it)
                             |
                             v
                        ExitExecutor           chosen by MODE alone
                     /                \
            PaperExitExecutor    BrokerExitExecutor
             (the simulator)      -> OrderManager -> BrokerAdapter -> MT5
                             |
                             v
                       CloseOutcome
              CONFIRMED / REJECTED / UNKNOWN
                             |
        +--------------------+--------------------+
        |                    |                    |
   book the fill       leave it open        park it; NEVER retry
   (partial or full)   with the reason      -> PositionReconciler
```

**The venue is authoritative for existence.** `PositionReconciler` reads it,
closes what it does not hold, and records what it says about levels and sizes
BESIDE what we intended -- never over it. When the venue cannot be read it
concludes nothing.

**Two facts, two columns.** `stop_loss` is what this platform intends;
`broker_stop_loss` is what the venue last reported. A disagreement between
them is a position running unprotected while the record says otherwise, which
is the most dangerous thing this layer can observe -- so it is recorded on
every pass, logged at ERROR when the venue holds no stop, and shown as `✗` in
the table rather than as a number that looks fine.

**Seven states, four of them added here.** `opening` (nothing confirmed),
`partially_closed` (open at a smaller size than was risk-sized), `closing` (we
asked, the venue has not answered), `reconciling` (somebody is looking right
now -- distinct from `unknown`, which means nobody is). A management pass acts
on `open` and `partially_closed` and refuses the rest.

| Property | Mechanism |
|---|---|
| A close is an order | `BrokerExitExecutor` reaches the venue only through the account's `OrderManager` |
| No blind retry | UNKNOWN parks; the manager refuses every unsettled state |
| No over-close | refused in the executor, in the API, and by a DB CHECK |
| No silent stop removal | `protect` cannot clear a level; a missing one is recorded and rendered |
| Deterministic stop priority | the most protective proposal wins, not the first |
| No browser dependency | `PositionMonitor` is a supervised `Worker` |

---

## Bot supervision, as built (L22, 2026-09-04)

```
                        bot_runs row                 heartbeat
                     (what was believed)         (what is still alive)
                             \                    /
                              \                  /
                               v                v
                            BotSupervisor.sweep()
                                     |
                     +---------------+---------------+
                     |                               |
              heartbeat fresh                heartbeat silent
                     |                               |
                  leave it                     mark `crashed`
                                                     |
                                            (a SEPARATE pass)
                                                     |
                                             BotSupervisor.recover()
                                                     |
                                       safety check wired?
                                        /                  \
                                      no                   yes
                                       |                     |
                                REFUSE, and say         ask it, and
                                which check is          refuse on its
                                missing                 answer
```

**A status column is a claim; a heartbeat is evidence.** Nothing here trusts
`bot_runs.status` to answer "is this running?" — a process that dies mid-run
touches nothing, so every crash leaves a row reading `running`, which is the one
case where the column is both wrong and reassuring.

**Marking dead and deciding to restart are two passes.** Folding them into one
loop would make the second decision invisible, and restarting an automated
trader is precisely the decision that must be visible.

**Nine states.** The five that existed (`starting`, `running`, `stopping`,
`stopped`, `crashed`, `halted`) plus `paused` — which the paper service was
recording as `stopping`, so a paused bot and one shutting down were the same row
— plus `recovering` (somebody is acting NOW, distinct from `crashed` where
nobody is, the same distinction `reconciling` draws from `unknown`) and
`disabled` (barred until explicitly re-enabled, distinct from `stopped`, which
anyone may start).

`MAY_TRADE` is the single-element frozenset `{running}`. Every other state —
pausing, stopping, and all four failure states — means no new trades, and
expressing that as "not stopped" would let a crashed bot keep trading.

**Limits combine; they are not validated.**

```
   bot.max_daily_loss  ----\
                             tighter()  ---->  effective limit
   account.max_daily_loss --/

   bot.cooldown_seconds ---\
                             looser()   ---->  effective cooldown
   account.cooldown_seconds /             (longer IS stricter)
```

The invariant "a bot may never exceed its account" holds by the shape of the
function rather than by a check somebody must remember to run. Validating at
write time fails silently later: a limit that was legal when saved becomes
illegal the moment the account tightens, and nothing re-checks it.

| Property | Mechanism |
|---|---|
| Cannot bypass risk, sizing or the OMS | `app/bots/` imports none of them; a test parses every module |
| Cannot reach a venue | no adapter is importable from the package |
| A paused or stopped bot cannot trade | `MAY_TRADE == {running}` |
| A bot cannot widen an account limit | `effective()` cannot return a looser figure than either input |
| Counters survive a restart | read from the database every check, never cached |
| Recovery is never a default | refused, with the missing safety check as the reason |
| A kill switch is not recoverable | `halted` is absent from `RECOVERABLE` |
| Restart preserves intent | `plan_restart` keeps `stopped`, `paused` and `disabled`; only an orphaned `running` becomes `crashed` |
| Live stays shut | `preflight` refuses a live bot and names `LIVE_TRADING`, not the request |
| No browser dependency | runners and the supervisor are `app.workers.Worker`s |

**Where the AI seat lives now.** `AiVerdict` and `AiFilter` moved from
`app/paper/engine.py` to `app/execution/ai.py`, re-exported from their old home.
They sat inside one of the two pipelines that depend on them, which was a real
import cycle (`paper.engine` → `execution/__init__` → `pipeline` →
`paper.engine`) that only the test suite's import order had been hiding.

---

## The data pipeline, as built (L23, 2026-09-04)

```
   market_bars  (RAW; written by app.marketdata, never by this package)
        |
        v
   quality.assess          what this series can support, and what it cannot
        |                  corrupt -> BLOCK. gap -> warn. anomaly -> keep.
        v
   resampling.resample     only when a timeframe must be built
        |                  bucket = f(timestamp), never f(position in list)
        v
   +----------------------+----------------------+
   |                      |                      |
   v                      v                      v
 features.compute      labels.compute       (nothing else reads the bars)
 bars <= T             bars > T
   |                      |
   |                      +--> AMBIGUOUS when one bar holds both barriers
   |                      +--> None for the last `horizon` bars
   v                      v
   +----------> builder.build <----------+
                     |
        alignment: row i = features(<= i) + labels(> i), stamped bar i
                     |
        splits.chronological  ->  splits.walk_forward
                     |
        scaler.fit(rows, split)   reads split.train and nothing else
                     |
        leakage.report            six checks
                     |
        +------------+------------+
        |                         |
    all passed                 any failed
        |                         |
      READY                    CLEAN, with the failure attached
```

**Features read `<= T`, labels read `> T`, and the two never meet except in one
row.** That is the whole design; everything else is making sure it stays true.

**Causality is proved, not asserted.** `no_future_influence` computes over a
prefix, appends the rest, recomputes and requires *exact* equality — and a
second test feeds the same check a deliberately forward-reading feature and
requires it to FAIL. A check that cannot fail proves only that it ran.

**The normalisation mistake has no spelling.** `Scaler.fit()` takes rows *and a
split*, and reads `split.train`. There is no function that takes a whole dataset
and returns a scaler, so §17's error cannot be written; a caller who wants one
has to build a split that says so, which is then visible in `fitted_rows` and
caught by the leakage check.

**Every feature is dimensionless.**

```
   NOT a feature:  sma_20        (a price level: a model learns the instrument)
   IS  a feature:  sma_distance_20 = close / SMA(20) - 1
   NOT a feature:  atr_14        (the unit every bracket here is quoted in)
   IS  a feature:  atr_pct_14    = ATR(14) / close
```

The metals run is why: +4,236 pooled out-of-sample points across symbols whose
median H1 ATR spans 59x, which was an arithmetic error rather than a finding.

**A dataset is a recipe.** No table stores a training row. The fingerprint
covers the configuration **and a digest of the raw bars**, so the same recipe
over different data is a different dataset; rows are rebuilt on demand, which
means there is no second copy of `market_bars` to drift from it.

| Property | Mechanism |
|---|---|
| Raw data is never overwritten | the package has no write path to `market_bars`; two tests enforce it, one by reading the table back after a build |
| Future data cannot become a feature | `no_future_influence`, plus a negative control |
| Labels stay out of the features | `labels_are_not_features`; the engines take disjoint bar ranges |
| The scaler never sees the holdout | `fit()` requires a split; `refit_changes_nothing` verifies |
| Test is later than train | `splits_are_ordered`; unordered input is refused, never sorted |
| A dataset reproduces | fingerprint over config + bars; two builds compared row for row |
| Invalid cannot be READY | the builder decides; no PATCH/PUT/DELETE exists on the router |
| Nothing is fabricated | rows are dropped and counted; no imputation exists anywhere |
| Nothing is trained | the package imports no model, no framework and no venue |

**Where the four new tables sit.** `feature_sets` and `label_sets` are immutable
versions; `datasets` holds one manifest per `(key, version)` with the constraint
`status <> 'READY' OR (fingerprint IS NOT NULL AND row_count > 0)`; and
`dataset_checks` keeps every finding, including the passing ones — a dataset
whose leakage report is absent and one whose report passed look identical from a
status column, and only one of them has been looked at.

---

## The AI layer, as built (L24, 2026-09-04)

```
   app/datasets/ (L23)          causal, dimensionless, versioned features
        |
        v
   BaseModel.predict            the gate, in a FIXED order
        |
        |-- not fitted?               -> MODEL_UNAVAILABLE
        |-- feature version unlisted? -> FEATURE_VERSION_MISMATCH
        |-- a required feature null?  -> MODEL_INPUT_ERROR (names them)
        |-- older than the limit?     -> STALE_FEATURES
        v
   +----------+----------------+
   |          |                |
 regime   probability      anomaly
 quantile  logistic         median / MAD
 cuts      coefficients     robust z
   |          |                |
   +----------+----------------+
        |
     Prediction        one shape; a refusal carries NO value
        |
        v
   app/execution/ai.py          ModelBackedFilter -> AiVerdict
        |
        |-- AI_REQUIRED  + no answer -> accept=False   (NO TRADE)
        |-- AI_OPTIONAL  + no answer -> accept=True    (to the risk engine)
        v
   RiskEngine -> PositionSizing -> OMS -> BrokerAdapter -> MT5
        ^
        |
   unchanged, and still the only authority
```

**The dependency runs one way.** `app.execution` imports `app.ai`; `app.ai`
imports nothing from `app.execution`. That is the L22 cycle not repeated, and a
test parses every module in the package to keep it true. It is also why
`ModelBackedFilter` and `AiPolicy` live in `app/execution/ai.py` beside the
seat rather than in `app/ai/`.

**A refusal carries no value**, in the type and in the database:

```python
Prediction(status=MODEL_UNAVAILABLE, value="UP")   # raises ModelError
```
```sql
CHECK (status = 'OK' OR prediction IS NULL)        -- ck_model_predictions_...
```

The column uses `NullableJSONType`, not `JSONType`: SQLAlchemy's default JSON
serialises Python `None` as the JSON literal `null`, which is not SQL NULL and
would pass or fail that check for the wrong reason. Found by the check firing
on a refusal.

**A prediction's identity covers its input.**
`sha256(model, version, at, symbol, timeframe, features_at, input_digest,
status)` — the digest is what stops two different feature vectors at one
timestamp collapsing into a single row, which they did until a test counted
three predictions and found one. `model_predictions.prediction_key` is UNIQUE
over it, so recording is idempotent the way `orders.intent_id` is.

| Property | Mechanism |
|---|---|
| AI cannot execute, size, approve or enable live | `app/ai` imports none of it; `AiVerdict` has four fields and none can |
| A missing feature is never a fake value | `MODEL_INPUT_ERROR` naming them; nothing in the package imputes |
| An incompatible feature version blocks | compatibility must be declared; the default is to refuse |
| A model version is never silently replaced | duplicate `(key, version)` refused; `latest()` is a separate call from `get()` |
| A promoted version states its provenance | DB CHECK: `status <> 'promoted' OR (feature_version IS NOT NULL AND params IS NOT NULL)` |
| No secret in an artifact | `register_version` walks the params for credential-shaped keys and refuses |
| Inference is deterministic | no clock, no RNG, no mutation in `predict`; the fit is full-batch and fixed-iteration |
| Future data cannot move a past prediction | tested end to end at 5x prices appended after the bar |
| AI failure is explicit | two policy values, no implicit third, applied in one place |

**Regime thresholds are fitted, not chosen.** Four quantiles of the *training*
segment, because a 0.3% ATR is high volatility in EURUSD and quiet in BTC — the
same unit lesson L23 applies to features, one level up. Volatility wins at the
top of its range: a market moving violently in one direction is more usefully
described as violent than as trending.

**A probability is P(a named label).** `label_definition` travels on every
prediction, and `calibrated` is false until something measured it — an
uncalibrated probability is a ranking, not a frequency.

---

## The training engine, as built (L25, 2026-09-04)

```
   POST /v1/ai/training/jobs      validates, writes a row, returns at once
        |
        v
   TrainingService.queue()        2 concurrent, 3 per user; a duplicate
        |                         RECIPE already running is refused (S38)
        v
   asyncio task  (the BacktestService pattern, not a new queue)
        |
        +-- loading_dataset    -> L23 rebuilds rows; FINGERPRINT LOCKED
        +-- quality_gates      -> READY? leakage? rows? order? features? classes?
        +-- splitting          -> L23's chronological split. Nothing re-splits.
        +-- preprocessing      -> Scaler.fit(rows, split)  reads split.train
        +-- fitting_baseline   -> the TRAINING prior, not a coin flip
        +-- fitting_candidate  -> asyncio.to_thread; cancellable per iteration
        |                         early stopping on VALIDATION only
        +-- evaluating         -> ML metrics | economic metrics  (never merged)
        |                         fingerprint RE-DERIVED and compared
        +-- recording          -> register_version(status="draft")
        v
   status = validation_pending   ->   L26 validates   ->   L28 promotes
```

---

## The validation engine, as built (L26, 2026-09-04)

```
   POST /v1/ai/validation/runs    names a model version AND a dataset.
        |                         There is no "latest" for either.
        v
   ValidationService.queue()      2 concurrent, 3 per user; the same recipe
        |                         already running is refused
        v
   asyncio task  (the same TrainingService shape; still not a new queue)
        |
        +-- loading         -> model version + (bars, dataset) from ONE read
        +-- data_integrity  -> L23's verdict, not a second opinion
        +-- artifact        -> model_from_version(); features; fingerprint lock
        +-- leakage         -> L23's six checks, READ
        +-- temporal        -> train < validation < test, proved from timestamps
        |
        |   ---- everything below is on the TEST segment only ----
        |
        +-- baseline        -> candidate vs the majority baseline L25 recorded
        |                      AUC; and train-vs-test AUC, both MEASURED here
        +-- calibration     -> ECE + reliability bins  (a WARNING, never a FAIL)
        +-- economic        -> tools/rule_backtest.simulate, spread charged,
        |                      entry at the NEXT bar's open
        +-- walk_forward    -> AUC per SEQUENTIAL block; never shuffled
        +-- robustness      -> threshold +/- , cost x1.5
        +-- regime          -> terciles of atr_pct_14; a thin cell gets a count
        |                      and no verdict
        +-- significance    -> permutation null over the model's own
        |                      predictions, Bonferroni over candidates_tried
        +-- reporting       -> fingerprint RE-DERIVED; a dataset that moved
        |                      BLOCKS the report
        v
   validation_runs row: verdict + full report
        v
   A HUMAN reads it.        ->   L28 promotes   ->   L27 wires it into a strategy
```

---

## AI in the strategy pipeline, as built (L27, 2026-09-04)

```
   STRATEGY produces a signal on CLOSED bars
        |
        |   the SAME Candles object goes to both. They cannot diverge.
        v
   SignalContext           strategy, symbol, timeframe, bar_time, side,
        |                  levels, strategy_score, and the closed bars.
        |                  NO labels. NO future. Nothing fetches data.
        v
   AI SEAT   (app/execution/ai.py -- unchanged position since L16)
        |
        +-- AI_DISABLED   -> returns before doing anything. In a paper bot the
        |                    seat is None: there is no AI object at all.
        +-- AI_ADVISORY   -> runs, records, returns NEUTRAL. Never ACCEPT.
        +-- AI_FILTER     -> runs, may REJECT.
        +-- AI_SCORING    -> min(s,ai) | weighted | product, then a threshold.
        |
        |   every failure -> ONE function -> AI_REQUIRED rejects,
        |                                    AI_OPTIONAL proceeds. Never implicit.
        v
   AiVerdict  { accept, confidence, reason, model }   <- four fields. None of
        |                                                them names a limit, an
        |                                                approval, a quantity or
        |                                                an account.
        v
   RISK ENGINE     <- unchanged, and still the ONLY producer of an Approval.
        v                It may reject an AI-accepted signal and often should.
   POSITION SIZING <- unchanged, and still the ONLY producer of a quantity.
        v
   OMS -> BROKER ADAPTER -> MT5
```

**The AI can only subtract.** In a backtest a bar the strategy left flat is
never offered to the layer, so there is no branch that could turn a 0 into a
+/-1. In a live pass the strongest outcome is `accept=False`.

**A model must be VALIDATED to be named**, at an exact version.
`app/ai/eligibility.py` reads L26's verdict today and L28's status tomorrow.

**Every decision is journalled**, including the ones that changed nothing, with
what happened AFTERWARDS on the same row -- so "the AI accepted and risk vetoed"
is one fact rather than a correlation somebody reconstructs.

---

## The model registry, as built (L28, 2026-09-04)

```
   TRAINING writes a candidate                          model_versions.status
        |                                                       = draft
        v
   L26 VALIDATION  ->  PASS | CONDITIONAL  ------------------> validated
        |                                                          |
        |  BLOCKED / FAIL --> rejected (terminal, NOT a deletion)   |
        |                                                          v
   REGISTRY re-reads the verdict from the TABLE, then:          registered
        +-- feature set matches this deployment                     |
        +-- every declared feature is one the engine computes       |
        +-- artifact inspected, HASHED, and LOADED                  |
        |     (a digest proves the bytes did not change;            |
        |      only loading proves they were ever a model)          |
        v                                                           v
   DEPLOY TO PAPER  (one active deployment per scope,            paper
        |            enforced by a PARTIAL UNIQUE INDEX             |
        |            over a derived NOT NULL scope_key --           |
        |            NULLs are distinct, so the nullable            |
        |            columns could not carry the rule)              |
        v                                                           v
   PROMOTE  <-- the ONLY edge into this state, and it starts    promoted
        |        at `paper`. A newly trained model cannot            |
        |        arrive here because no arc would let it.            |
        v                                                            v
   ROLLBACK -> the previous version is RE-CHECKED as a fresh    rolled_back
               deployment would be. If it cannot be restored,        |
               the rollback is REFUSED and the current version       v
               stays active: an active model that is wrong is   registered
               bad; no active model at all is worse.            or retired
```

**Resolution**: `Request(model, strategy, symbol, timeframe, environment)` ->
the active deployment whose scope COVERS it, most specific first -> five checks
(status serves, names its validation run, digest matches, features match, scope
compatible) -> a `Resolution` carrying every check it made. Never `latest`.

**Caching**: keyed by VERSION ID, never by scope. A scope's answer changes on
every promotion; a version's artifact does not, because a changed model is a new
version. So a lifecycle change needs no invalidation -- the resolution asks for a
different key.

**L27 goes through it.** `RegistryResolver` resolves once when a bot starts, so
the version a run used is a fixed fact for its life -- and a version mismatch is
REFUSED rather than substituted, because a decision must name the model that
made it.

**Every transition** writes a `model_lifecycle_events` row AND an `AuditLog`
row, records both halves plus the actor and the reason, and publishes through
L07's hub. `promote`, `rollback`, `stop` and `retire` all refuse an empty reason.

---

## Model monitoring, as built (L29, 2026-09-04)

```
   ai_decisions (L27)   model_deployments (L28)   trades (L19)   validation_runs (L26)
          |                      |                     |                 |
          +----------+-----------+---------+-----------+                 |
                     v                     v                             v
              collect.py  (reads; computes NOTHING)              baselines.py
                     |                                        (VALIDATION preferred;
                     |                                         a missing one is None,
                     |                                         never zeros)
                     v
        monitor.evaluate()   pure: no session, no clock
                     |
                     v
        twelve findings, each with its own SAMPLE SIZE
                     |
      +--------------+---------------------+------------------------+
      v                                    v                        v
  health.derive()                    snapshot row              alert lifecycle
  OFFLINE > CRITICAL > DEGRADED    (window bounds, baseline,   (fingerprint incl.
   > WARNING > INSUFFICIENT_DATA    per-family blocks; a       SEVERITY, cooldown,
   > HEALTHY                        block never computed is    recovery rows, one
   -- no score, ever                NULL, not {})              open per condition)
      |                                    |                        |
      +------------------+-----------------+------------------------+
                         v
                 ALERT  ->  a HUMAN reviews
                         (the ladder tops out at `validate`, never `deploy`)
```

**Never DRIFT -> REPLACE.** `FORBIDDEN_ACTIONS` has forbidden `promote`,
`deploy`, `replace_production_model` and `rollback` since before this level, and
L29 adds a parsed test that no monitoring module even NAMES the registry's verbs.

**Never in the execution path.** This reads rows that were already written. No
execution code calls it, so a drift calculation has never delayed an order.




**The verdict is precedence, not arithmetic.** `BLOCKED > FAIL > CONDITIONAL >
PASS`. There is no score, and a test asserts there is no key that could become
one.

**BLOCKED is not FAIL.** One says the candidate is not good enough; the other
says we could not tell. A missing check is never a passing one.

**A PASS authorises consideration and nothing else.** `model_versions.status` is
never written by this package; the run status is `completed`, never `passed`;
and the router has no PATCH, PUT or DELETE and no path containing promote,
activate, deploy or approve.


**A successful run ends at `validation_pending`, and there is deliberately no
`completed`.** §25: "candidate model successfully trained" is not "model
approved for trading", and a status called `completed` would be read as the
second. Three statuses §5 lists are absent because nothing can reach them —
`paused` (no checkpointing, and these fits are sub-second), and
`validation_passed` / `validation_failed`, which belong to the level that writes
them.

**§6 is a check, not a rule.** A version string records what data was *called*;
L23's fingerprint covers the configuration **and a digest of the bars**. The job
locks it before fitting and re-derives it after, and fails if it moved rather
than recording a model against provenance that is a guess.

**The leak has no spelling.** `Scaler.fit()` takes rows *and a split*. There is
no shuffle option in `TrainingConfig`, in `SplitConfig`, or in
`app.datasets.splits`. Early stopping reads the validation segment; the test
segment is never consulted for any decision. Nothing resamples — class
weighting instead, because duplicating rows in a time series creates the same
bar twice at one timestamp.

| Property | Mechanism |
|---|---|
| Training cannot promote, deploy or trade | the package imports none of it; `"promoted"` does not appear in it; no PATCH/PUT/DELETE on the router |
| A failed or cancelled job leaves no candidate | `_fail` and `_finish` write terminal states and never a version |
| Cancellation is cooperative | a flag the job OWNS, checked between stages and between gradient iterations |
| Training does not block the API | the fit runs on a worker thread, not the event loop |
| A duplicate job is refused | `config_fingerprint` over active rows |
| Bounded resources | 2 concurrent, 3 per user, and `shutdown()` drains on exit |
| A candidate names what it was fitted on | feature, label, dataset and preprocessing versions plus the dataset fingerprint, and the DB refuses to promote one that cannot |
| ML accuracy is never trading performance | two dictionaries, never merged; accuracy always beside the majority share |

**`model_version_id` had to become nullable**, and it is the load-bearing part
of migration 0015: a job is created when it is *queued*, and the candidate does
not exist until the fit finishes. Under the old NOT NULL a caller would have had
to invent a model version before training one.

---

## Portfolio and exposure, as built (L30, 2026-09-04)

```
   broker adapter (L10)      paper_accounts (L16)      positions (L21)   trades (L19)
   get_account/get_quote      balance · equity          the open book    the closed record
   get_positions              updated_at = as_of              |               |
            |                        |                       |               |
            +-----------+------------+          joined to symbol_mappings (L11)
                        v                       contract_size · tick_size · tick_value
                  AccountState                              |
            every field optional; source                    v
            and FRESHNESS carried per read             PositionView
                        |                                   |
                        +-----------------+-----------------+
                                          v
                                    PortfolioView
        +-------------+-------------+-----+-------+-------------+-------------+
        v             v             v             v             v             v
    exposure       P&L         drawdown       margin        health      reconciliation
  gross AND net  all-or-      peak only     reported,    PRECEDENCE     compared,
  refuses w/o    nothing      ever rises    never         never a       NEVER repaired
  contract size                             enforced      score
        |             |             |             |             |             |
        +-------------+------+------+-------------+-------------+-------------+
                             v
              +--------------+---------------+
              v              v               v
        14 GET routes   4 events on     to_risk_state()
        (no other       account:<id>          |
         verb exists)   what CHANGED,         v
                        never a timer   RiskEngine DECIDES
```

**The arrow into risk points one way.** `to_risk_state()` returns a MAPPING, not
`PortfolioState`, so this module imports no risk decision — and a parsed test
asserts it imports no OMS, sizer, broker or order path either. Risk reads
portfolio; portfolio cannot reach risk.

**Every value in that handoff may be `None`, and L17 treats a needed `None` as a
VETO.** A stale or unreadable portfolio therefore makes trading MORE conservative,
not less, which is the opposite of the intuitive fear. The nine `PortfolioState`
fields this engine does not own are declared in `NOT_SUPPLIED` with their owner,
so adding a risk field forces a decision rather than producing a silent null.

**No migration.** `portfolio_snapshots` (L05) already had balance, equity,
margin, free_margin, open_positions, exposure and drawdown_pct.

---

## The trade journal, as built (L31, 2026-09-04)

```
  webhook_events   signals    risk_events   ai_decisions   orders     positions
     (L09)          (L09)       (L17)         (L27)       orders.     position_
       |              |           |             |         sizing       events
       |              |           |             |         (L18/19)     (L21)
       +--------------+-----------+-------------+------------+-----------+
                                        |
                     +------------------+------------------+
                     v                                     v
             record_pending()                        timeline_for()
        a SWEEP over finished positions        DERIVED at read time,
        with no journal row -- a hook          stored NOWHERE, so it
        missed at one of four close            cannot disagree with
        sites would silently produce           its own sources
        no trade at all                              |
                     |                               v
                     v                        context_for()
              closes_from()                 read from the row written
        weighted exit; realized SUMMED       THEN, never recomputed now
        from the closes, never                     |
        recomputed from the average                |
                     |                             |
                     v                             |
                 trades  <-- ONE ROW PER POSITION EPISODE
              UNIQUE(position_id) WHERE NOT NULL
                     |                             |
        +------------+-------------+---------------+
        v            v             v
   8 GET routes   3 events on   quality.check()
   (no other      account:<id>  detect and FLAG,
    verb)                        never correct
```

**Never a second history.** No new table: `trades` gained 13 nullable columns.
The timeline is derived, the context is referenced, and every fact stays owned by
the system that wrote it.

**Never in the execution path.** This reads rows that were already written, and a
parsed test asserts `app/journal/` imports no OMS, sizer, broker, risk decision
or position manager, and names no order-placing verb.

---

## Analytics, as built (L32, 2026-09-04)

```
   trades (L31)      orders/executions (L19)   portfolio_snapshots (L05)
   ONE ROW PER        counts, latency,          equity over time
   POSITION EPISODE   slippage in POINTS        (can contain a deposit)
        |                     |                          |
        |            ai_decisions (L27)                  |
        |            1:1 join, cannot fan out            |
        +----------+----------+---------------+----------+
                   v                          v
            Scope (environment,        PortfolioService (L30)
            account, window,           DELEGATED, never a second
            strategy, symbol,          account state
            bot, model, ai_mode)              |
                   |                          |
                   v                          |
          app/analytics/metrics.py            |
     THE definitions -- extracted from the    |
     THREE copies the audit found, and now    |
     called by backtest/runner.py and         |
     training/metrics.py as well              |
                   |                          |
     +-------------+--------------+           |
     v             v              v           v
  summary       equity.py     windows.py   exposure
  currency      TWO curves,   half-open    (portfolio's
  AND r,        never merged  [start,end)   own answer)
  each          + drawdown    L17's day
  labelled      periods       boundary
     |             |              |           |
     +-------------+------+-------+-----------+
                          v
                  11 GET routes
              (a parsed test asserts the
               package has NO write verb)
```

**One trade row is one trade.** The AI join is 1:1 and breakdowns group in Python
over the rows the scope already selected, so a grouped total cannot disagree with
the ungrouped summary -- and an end-to-end test asserts `/analytics/summary` and
`/trades` report the same count for the same account.

**The extraction is the level.** Three implementations of win rate, profit factor
and maximum drawdown became one, and the two existing callers kept their output
shapes exactly -- including `runner.py`'s `NOT_AVAILABLE` sentinel, which the
backtest API has served since L14.

---

## Deployment, as built (L41, 2026-09-05)

Three compose files, and the split is the design:

```
docker-compose.yml            the stack. Publishes NOTHING.
docker-compose.override.yml   development publishes. Loaded AUTOMATICALLY.
docker-compose.prod.yml       production. Loaded only when named on the CLI.
```

Being private in production is therefore a property of **how the files load**
rather than of anybody remembering to override something. The earlier attempt
-- `ports: []` in the production overlay -- did not work, because Compose
merges port lists by appending, and the production stack was still exposing
PostgreSQL, Redis and the API. CI now parses the merged config and fails if
anything but nginx publishes a port.

**The API and the workers are the same image, split by one variable.**
`WORKERS_ENABLED` is false on the API and true on a single worker container,
because before L41 the API process ran the notification consumer and the
monitoring worker in-process -- so a second API replica was a second monitoring
worker. `RECOVERY_STARTUP_CHECKS` runs the other way: true on the API, which
gates order submission, and false on the worker, because two processes
reconciling the same account at boot would produce two safe-mode latches that
cannot see each other.

**A fourth health state was added.** `/health/live` is the process,
`/health/ready` is its dependencies, and `/health/trading` is whether the
platform should be trading -- a process can be perfectly alive and perfectly
ready while safe mode is latched. It is a **report, not a gate**: nothing
consults it before trading, which is precisely what makes it safe to compute.
It answers 503 in the normal steady state (no broker adapter), so the container
healthcheck deliberately probes `/health/ready` instead.

**Release identity** comes from build arguments, never from git at runtime: a
production container has no `.git` and no git binary, and a version function
that tries and fails is one that raises inside a health check. An image built
without them reports `"stamped": false` and is not deployable -- a release you
cannot identify is one you cannot roll back to.

---

## Security, as built (L39, 2026-09-05)

`app/security/` sits beside `app/auth/`, not on top of it. Authentication,
sessions, RBAC and CSRF stay where L04 and L06 put them; what is here is the
set of controls the L39 audit found genuinely absent.

```
request
  -> SecurityHeadersMiddleware   pure ASGI, outermost, non-HTTP passes through
  -> request-id middleware
  -> CSRF middleware
  -> router
       require_permission(...)          L06, unchanged
       Confirmation.check(...)          L36, unchanged
       check_set_active(...)            deterministic refusals, L40
       require_step_up(...)             L39 -- the last gate, because it is spent
  -> service
```

**Step-up is the only genuinely new authority check, and it can only refuse.**
It never approves anything, never grants a permission and never widens a limit
-- the same rule L38 wrote for the safe-mode latch, and the rule that makes it
safe to add a fourth gate to a system that already had three. A test walks the
package's imports to keep it true.

**Two event types, split by authorization rather than by taxonomy.** A
`system`-scoped security alert carries a count and a class and never a subject,
because the `system` channel reaches every signed-in browser; anything naming a
person is `user`-scoped. The payload keys are an allow-list, because a
deny-list has to predict the name of the field that leaks.

**The middleware is pure ASGI and that was measured, not stylistic.** The first
implementation used `app.middleware("http")` and doubled the WebSocket
flakiness in `test_realtime.py` (8 flaky failures against 2). `BaseHTTPMiddleware`
wraps each request in an anyio task group; this application already had two, and
a third was enough to make an existing timing sensitivity fire. The ASGI version
adds no task, no stream, and returns immediately for a WebSocket handshake.

**Grants live in the process**, for the reason L38's latch does: a five-minute
credential that survives a restart is one nobody revoked. A second API process
needs a shared store -- a row and a lease -- and that is recorded in
`SECURITY.md` as a known limit rather than left to be discovered.

---

## Integration testing, as built (L40, 2026-09-05)

One file, `backend/tests/test_integration.py`, and no new infrastructure. It
reuses `test_execution.py`'s `SPEC` and pipeline shape, `app/brokers/fake.py`'s
fault injector, and the in-memory-SQLite app fixture every other module uses.

What it tests is the layer nothing else could: **the handoffs.** Not "risk
vetoes" but "risk vetoes and therefore the venue received nothing". Not "an
order is created" but "the order names the signal, the sizing snapshot names
the execution, and the risk decision id is set".

That distinction is what found the defects. Six of the seven were at a seam:
two at the webhook gateway's two unique keys, one at the bus client's timeout,
one at the ordering of two gates, one at a migration's constraint naming, and
one at a middleware's interaction with the socket layer. None was inside a
subsystem.

---

## Recovery and reconciliation, as built (L38, 2026-09-05)

```
   PROCESS STARTS
        |
        v
   create_app lifespan
        |
        +-- hub.start()            L07
        +-- notification consumer  L34
        +-- monitoring worker      L37
        |
        v
   RecoveryManager.run_startup     <-- L38. Before anything consumes a signal:
        |                              the execution worker and the bot
        |                              supervisor are registered and NOT started.
        |
        1 configuration      settings + the live gates, read and reported
        2 database           SELECT 1, through L02's own check
        3 migrations         every expected table present -- never migrates
        4 event_bus          the hub's reader is running
        5 market_data        provider usability + newest stored bar
        6 webhook_gateway    is a shared secret configured at all
        7 broker             L10's registry.health()
        8 oms_reconciliation orders whose venue state was never established
        9 position_recon     positions the platform could not settle
       10 risk_engine        L17's own status; kill switches READ, never touched
       11 bot_recovery       L22's plan_restart -- asked, not told
       12 notifications      L34's channels
       13 monitoring         L37's collector
       14 operational_mode   the mode this process runs in, and why
        |
        v
   a blocking step LATCHES SAFE MODE with that condition as its reason
        |
        +--> system_events row per step        (the table L05 built)
        +--> one SYSTEM_ALERT per blocking step -> L34 -> in-app/email/Discord
        |
        v
   +----------------------------- SAFE MODE ------------------------------+
   | BLOCKS                              | STILL WORKS                    |
   |   POST /v1/orders          -> 409   |   monitoring                   |
   |   ExecutionPipeline gate 0 -> refuse|   reconciliation               |
   |   bot recovery SafetyCheck -> refuse|   position and order visibility|
   |                                     |   risk evaluation              |
   |                                     |   the recovery routes          |
   +---------------------------------------------------------------------+
        |
        v
   POST /v1/recovery/safe-mode/exit
        |
        +--> RE-RUNS THE SEQUENCE, then clears only the latches whose
             condition has actually gone. The response says which are left.
```

**The gap L38 closed was never the checking.** Four reconcilers already
existed -- `app/brokers/reconcile.py` (L10), `OrderManager.reconcile` (L19),
`app/positions/reconciler.py` (L21), `app/bots/supervisor.py` (L22) -- and
`MIGRATION_STATUS.md` had said since L30 that *the pieces exist; nothing calls
them first*. So `app/recovery/reconciliation.py` contains no comparison logic:
a second implementation of "do these positions match" is a second answer, and
the one on the dashboard would eventually disagree with the one the OMS acted
on.

**Safe mode is a SECOND refusal.** It is checked before risk and adds a veto;
it never approves anything, never releases a kill switch and never widens a
limit. A submission that gets past it faces every check it faced before -- which
is rule 2 of step 37 held as a structural property rather than a convention.

**L22's seat, filled without lowering the bar.** `BotSupervisor` has taken a
`SafetyCheck` since L22 and defaulted to refusing everything. L38 supplies one
whose every branch is a refusal: safe mode, a kill switch, an unresolved order,
an unsettled position, an unusable adapter. `None` means "these five found
nothing", and L22's own gates still run afterwards.

**Nothing here can trade, and that is enforced.** `app/recovery` imports no
order manager class, no adapter class, no risk engine and no sizer, and four
tests hold it: an AST walk over imports and called verbs, and three greps -- for
live-trading assignment, for destructive SQL, and for a notification provider.

---

## Monitoring and observability, as built (L37, 2026-09-05)

```
      the systems that OWN each fact                    L37 reads them
      ------------------------------                   --------------
      app/core/health.py   (L02)  ------------------->  infrastructure
      app/workers/base.py  (L02)  ------------------->  workers
      app/realtime/hub.py  (L07)  ------------------->  event_bus, realtime
      app/marketdata       (L08)  ------------------->  market_data.providers
      market_bars          (L08)  ------------------->  market_data.freshness
      app/brokers/registry (L10)  ------------------->  broker
      orders / positions   (L19,21) ----------------->  oms, positions
      app/risk/service.py  (L17)  ------------------->  risk_engine
      bot_runs             (L22)  ------------------->  bots
      registry + alerts    (L28,29) ----------------->  ai.models
      channels + deliveries(L34,35) ----------------->  notifications.*
                                                        monitoring.self
                                    |
                            ObservabilityService.collect
                                    |
                        aggregate (weighted, never averaged)
                        trading_safety (derived, with reasons)
                                    |
                          Tracker: 2 consecutive, then raise
                                   2 consecutive, then clear
                                    |
                    +---------------+----------------+
                    v                                v
            system_events row              one SYSTEM_ALERT on L07's bus
            (the table L05 created                     |
             and nothing wrote to)         L34 notification service
                                                       |
                                           preferences -> in-app / email / Discord
```

**Every arrow points inward.** Nothing in `app/observability` calls back out:
no restart, no reconnect, no retry, no order, no limit change. Two AST tests
enforce it -- one over imports, one over called method names -- and the second
list is also section 47's boundary: L37 detects, L38 recovers. A monitor that
restarts the thing it watches cannot tell you it failed to restart it.

**Nothing new was created where something existed.** L02's check functions are
called rather than re-implemented, so the dashboard and `/health/ready` cannot
disagree about the database. `system_events` is the incident table, so L37
shipped no migration. `SYSTEM_ALERT` is the alert, so L37 added no event type.
`manage_system_settings` is the gate, so L37 added no permission.

**Five states rather than three, and the two extra ones are the point.**
`UNKNOWN` is *not observed* -- a failed probe, or a fact that does not exist
yet -- and is never treated as healthy. `NOT_CONFIGURED` is *deliberately
absent*, and never moves the overall state, because an unconfigured optional
integration must not make a working platform report unhealthy.

**Trading safety is derived here and is not a gate.** It reads the
authoritative components and always carries its reasons. Nothing consults it
before trading -- the RiskEngine vetoes, the OMS reconciles, the adapter
refuses -- which is precisely what makes it safe to compute a summary at all.

---

## Administration, as built (L36, 2026-09-05)

```
                         ADMIN PANEL  (/admin)
                                |
                         GET /v1/admin/*
                                |
              +-----------------+------------------+
              |                                    |
        READS (7 routes)                    WRITES (3 routes)
        dashboard, permissions,             activate / deactivate /
        integrations, configuration,        revoke-sessions
        contract, users, audit-logs                |
              |                              reason (>= 8 chars)
        counts + states read from            + the subject's own email
        the system that owns each                  |
                                             audit.record_admin
                                             actor, action, resource,
                                             environment, reason,
                                             before, after, request id
                                                   |
                                             audit_logs (L04, extended)

        =======  and NO door to any of these, deliberately  =======

        /v1/bots        start, pause, disable, limits     BotManager (L22)
        /v1/ai/models   register, deploy, promote,        registry   (L28)
                        rollback, retire
        /v1/brokers     status, reconcile                 adapter    (L10)
        /v1/risk        limits, kill switches             RiskService(L17)
        /v1/strategies  enable, disable                   L12
        /v1/notifications  channels, Discord test         L34, L35
```

**The shape of the level is that arrow that is not there.** Section 1 says the
panel must not become a second trading engine, section 10 says operate through
existing services, section 49 says never bypass the authoritative ones. The
strongest reading -- and the one taken -- is that the admin API has no route
that touches any of them: not a wrapper, not a proxy, not a convenience
endpoint. Each of those surfaces already enforces its own permission, and a
second door in front of one would be a second authorization surface to keep in
step with the first. The one that drifts is always the one nobody is watching.

Three tests hold it: an AST walk proving the admin modules import no risk
engine, order manager, broker adapter, sizer or execution pipeline; an
enumeration of every write-shaped route showing exactly three, all about a
user's access; and a grep for any assignment to `live_trading`, `trading_mode`
or a `LIVE_GATES` entry.

**`audit_logs` was extended rather than duplicated.** Section 31 names an
`admin_audit_logs` table and section 51 says not to create duplicates. This
table has been the administrative trail since L04 -- `audit.record` writes it,
`/v1/admin/audit-logs` reads it -- so 0025 adds an `environment` column and
three indexes to the one that exists. A platform with two audit tables has two
partial answers to "what happened".

**Immutability is a property, not a habit.** There is no DELETE and no UPDATE
against `audit_logs` anywhere in the application, and a test greps every module
for one. An administrator cannot erase their own trail through the API they
administer.

---

## Discord, as built (L35, 2026-09-05)

```
   the whole of L35 is the box on the right

   NotificationService  ->  PREFERENCE ENGINE  ->  CHANNEL ROUTER
                                                        |
                          +-----------------------------+------------------+
                          v                v                               v
                       IN_APP            EMAIL                         DISCORD
                       (L34)             (L34)                    DiscordWebhookChannel
                                                                          |
                                                                one webhook POST
                                                                          |
                                                              +-----------+-----------+
                                                              |                       |
                                                        204 delivered           classified failure
                                                                                      |
                                                            retryable: 429 (retry_after), 5xx,
                                                                       timeout, network
                                                            permanent: 400, 401, 403, 404,
                                                                       not-a-webhook
```

**What L35 did NOT touch, and that is the design.** No migration. No new
delivery status. No new preference column. No new event type. No second event
bus. No second retry policy. `Channel.DISCORD` has been in the enum, the
preference grid, the delivery table and the channel registry since L34, and the
level is one adapter plus two routes.

**Webhook rather than bot**, because the requirement is outbound notification:
no gateway connection, no long-lived process to supervise, and a credential
scoped to one channel rather than a guild. Commands were refused -- `/portfolio`
is a read of private trading data authorized by a Discord identity this platform
has never seen, and that is a separate security design rather than a feature.

**System-wide rather than per-user**, stated rather than assumed (section 34).
One webhook serves the deployment, which is why the Discord channel defaults to
off for every category and every user: enabling it is consent to appearing in a
shared channel. A per-user webhook is a bearer credential per user and needs
encryption at rest; storing one in plaintext because the feature was convenient
would have been the worse outcome.

**The secret's blast radius is one object.** The URL is read from settings in
the constructor and held on the instance. `describe()` has no branch that
includes it, not even redacted, and `_redact()` removes both the URL and its
token segment from any provider message before that message is logged or written
to a delivery row -- because Discord's error bodies echo the request and a
delivery's failure reason is readable by its owner.

---

## Notifications and alerting, as built (L34, 2026-09-05)

```
  RiskEngine   OMS   BotManager   Journal   Review   Monitoring   Portfolio
      |         |         |          |        |          |           |
      +---------+---------+----------+--------+----------+-----------+
                                  |
                       publishes a DOMAIN EVENT
                                  v
                    app/core/events.py   -- L07's bus. ONE bus.
                          |                        |
        +-----------------+                        +-----------------+
        v                                                            v
  app/realtime/hub.py                            app/notifications/worker.py
  fans out to browsers.                          NotificationConsumer.
  Deliberately dumb: it                          A second SUBSCRIBER, not a
  holds no session and                           second bus. It holds the
  cannot write a row.                            session the hub must not.
                                                             |
                                                             v
                                              app/notifications/service.py
                                               1  rule            catalogue.py
                                               2  recipients      from the CHANNEL,
                                                                  never the payload
                                               3  severity        rule, or the event
                                               4  environment     payload, else 'unknown'
                                               5  message         templates.py
                                               6  dedup           event_id
                                               7  cooldown        (type, entity, severity)
                                               8  PERSIST   <---- before any delivery
                                               9  preferences     with a floor
                                              10  queue           one row per channel
                                                             |
                    +----------------------------+-----------+--------------+
                    v                            v                          v
              IN_APP                       EMAIL                      DISCORD
              the row IS the delivery.     EmailProvider port.        the SEAT.
              Publishes NOTIFICATION_      SMTP behind it.            Reports
              CREATED on user:{id} --      Failures classified        NOT_CONFIGURED;
              a type catalogued at         retryable or not.          L35 fills it.
              L07 with no producer
              until now.
```

**The three boundaries this diagram is drawn to show.**

*The hub does not write.* Its own docstring says the hub delivers and never
decides. Handing it a database session so it could also persist notifications
would put a write path inside the object whose entire design property is that it
has none. So the consumer takes its own subscription. One Redis, two readers.

*Persistence precedes delivery.* Step 8 is above step 10 for a reason: a browser
that was offline when the frame went out reads the row on its next request. That
is why an in-app "delivery failure" is not a thing -- the durable part already
happened, and the socket is a nudge on top of it.

*Recipients come from the routing, not the body.* An `account:` channel resolves
through `paper_accounts`/`broker_accounts` to an owner; a `bot:` channel through
`bots`. A payload that named a `user_id` would be a payload that could name
somebody else's -- the rule `app/realtime/channels.py` already applies when it
refuses to authorize from the subscribe frame.

**What L34 added to the schema, and what it did not.** `notifications` has
existed since migration 0002 and is EXTENDED, not shadowed by a better-named
second table: nine nullable columns, a widened severity CHECK, and `UNIQUE
(user_id, dedup_key)` -- which constrains nothing on the pre-L34 rows because
both columns are NULL there and SQL treats NULLs as distinct. Two tables are
new. Nothing is dropped and no row is deleted.

**Where the level deliberately stopped.** `Channel.DISCORD` exists in the enum,
the preference grid, the delivery table and the registry, and its adapter says
NOT_CONFIGURED. L35 replaces one function body and changes no schema, no
preference column and no delivery status.

---

## AI trade review, as built (L33, 2026-09-04)

```
                        trades (L31) + its attribution links
                                       |
        +------------------------------+------------------------------+
        v                                                             v
  DecisionContext                                              OutcomeContext
  known AT OR BEFORE the entry                        everything after the entry
  entry, planned stop/target, strategy                exit, P&L, R, fills,
  definition AT ITS OWN VERSION, signal,              slippage, latency,
  AI decision + exact model version,                  MAE/MFE (null -- no
  risk snapshot, sizing record                        intratrade series here)
        |                                                             |
        |  <<<<<<<<<<<<<<  THE WALL  >>>>>>>>>>>>>>                   |
        |   four assessors take the LEFT side and                     |
        |   nothing else. The outcome is not on the                   |
        |   object they are given, so section 63's                    |
        |   test passes structurally.                                 |
        v                                                             v
  strategy_alignment   entry_quality   risk_quality            execution_quality
                                  \                           /
                                   exit_quality  (both, but rated
                                   against the PLAN, never against
                                   how far price later travelled)
                                          |
                                          v
                              deterministic TradeReview
                     every rating, every OBSERVED line, compliance,
                     the outcome, and the confidence figure
                                          |
                     ReviewProvider.narrate()  <-- summary, key factors,
                     (default: DETERMINISTIC,      lessons, questions ONLY.
                      not a language model)       No rating, no price, no
                                          |        model version to return.
                                          v
                          validation: structural + referential
                     every decimal in the narrative must appear in
                     the facts; an invented price, symbol or model
                     version FAILS the review rather than repairing it
                                          |
                                          v
                        trade_reviews  UNIQUE(trade_id, review_version)
                        regeneration adds a version; none is overwritten
```

**Never a second source of truth.** Baselines and patterns come from L32
analytics; model identity from L28; drift from L29, kept separate per section 51.

**Never in the execution path.** A parsed test asserts `app/review/` imports no
OMS, sizer, broker, risk decision, position manager or model registry service --
so "do not automatically replace models" is a property of the import graph
rather than a promise in a docstring.
