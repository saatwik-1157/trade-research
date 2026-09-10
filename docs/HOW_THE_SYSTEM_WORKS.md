# HOW_THE_SYSTEM_WORKS.md

Plain language. Every flow below was verified against the code, and where a
flow does not exist it says so rather than drawing it.

---

## The one thing to understand first

**There are two separate paths to a broker order, and they do not share a
single line of code.**

```
PATH A -- the research harness                PATH B -- the platform
start-trading.bat                             TradingView alert
  run_overnight.py                              POST /v1/webhooks/tradingview
    take_profit.py                                webhooks/gateway.py
      mt5_paper.py                                  execution/pipeline.py
        mt5.order_send()   x4                         risk -> sizing -> oms
                                                        brokers/mt5.py
  imports NOTHING from app/                               order_send()   x1
  no RiskEngine  no OMS
  no kill switch  no journal                  every gate, every time
  a timer is the only trigger                 no valid signal = no trade
```

**Path A is the one that has actually traded.** 476 trades on the demo
account over six days. It bypasses the RiskEngine, the OMS, the kill switch
and the trade journal, because it predates them and was written as a
measurement harness, not as a trading system.

**Path B is the one that is built correctly** and has never placed an order at
this venue — no broker account is registered, so it refuses with `no_venue`.

Everything else in this document is about Path B.

---

## Trading flow (Path B)

```
TradingView alert
  |
  v  POST with a shared secret
WEBHOOK GATEWAY                    app/webhooks/gateway.py
  body size cap
  parse
  authenticate (hmac)              <- wrong secret stops here, learning nothing
  validate schema
  age check                        <- a late alert describes a market that moved
  idempotency                      <- same signal twice = one order
  symbol resolution
  strategy lookup
  write Signal(status=new) + commit
  |
  v  ExecutionWorker claims a batch (polls; supervised loop, not the browser)
EXECUTION PIPELINE                 app/execution/pipeline.py
  validate the signal
  validate the strategy            <- disabled or quarantined stops here
  AI advisory seat                 <- may only DECLINE; absence = no opinion
  RISK ENGINE                      <- the ONLY producer of an Approval
  POSITION SIZER                   <- the ONLY producer of a quantity
  RISK binds the sized order       <- request_hash, re-checked by the OMS
  OMS                              <- the ONLY path to a venue
  |
  v
BROKER ADAPTER -> MT5 -> broker
  |
  v
fill -> position manager -> trade journal -> portfolio -> monitoring
```

Two properties are worth stating plainly:

**The veto is a token, not a check.** The pipeline cannot proceed without an
`Approval` object that only the RiskEngine can produce. This is stronger than
"the pipeline calls the risk engine" — there is no code path that forgets to.

**Every stage fails closed.** A stage that raises produces a refusal carrying
its reason, never a pass. `test_the_pipeline_never_raises` holds that line,
because a bot loop needs a decision it can record, not an exception it has to
classify.

## Why did it trade / why did it not

Both questions are answered by the same renderer, deliberately — a separate
renderer for refusals is how refusals end up with less detail than fills, and
refusals are what you debug. `python demo.py` prints it. One `execution_id`
threads the risk decision, the sizing result, the order intent, the logs and
the events.

## Risk flow

`app/risk/engine.py` holds 30+ named veto codes, among them: three kill
switch scopes, trading mode, market open, signal freshness, duplicate signal,
max open positions, one-position-per-symbol, max trades per day, max daily
loss, max drawdown, max exposure, max leverage, max risk per trade, spread,
margin, cooldown, stop-loss-required, max position size, max concentration.

Every DENY carries a reason code. **Missing is never safe:** an unknown open
position count refuses rather than assuming room — visible in the demo as
`max_open_positions: open position count is unknown`.

Not present: weekly loss, consecutive losses as a veto, correlation as a veto.

## Order lifecycle

```
intent -> submitting -> submitted -> accepted -> partially_filled -> filled
                                  \-> rejected   (the venue refused)
                                  \-> expired    (the venue confirmed; never inferred)
                                  \-> failed     (never reached the venue)
                                  \-> unknown    (we do not know what the venue did)
```

`unknown` is **reconciled, never retried**. An IPC timeout after `order_send`
looks exactly like a rejection from this side, so resending is how one
intention becomes two positions.

## Crash recovery flow

```
crash (any point)
  |
  v  restart
load persisted state
  |
  v
for each order not in a terminal state:
    ask the venue what it actually did
    reconcile
    NEVER resend on the assumption it failed
  |
  v
resume normal processing
```

Proven by `tests/test_execution_durability.py` — 27 tests, including one that
asserts the regression test is itself capable of failing.

The order row is written **before** the venue is called, and an order that
cannot be recorded is never sent. That ordering is what makes recovery
possible: a crash between the two leaves a record to reconcile, not a silent
position.

## Research flow

```
EDGAR / market data  ->  Evidence (source + tz-aware stamp, both enforced)
  -> Opportunity (deduped on the EVENT, not the detection run)
  -> assess()  ->  materiality / divergence / freshness / false-opportunity
  -> edge: no_edge UNLESS app.research.selection beats its own null
  -> below 50% component coverage: NO SCORE AT ALL
  -> PortfolioDecisionContext -> fit -> marginal -> hard constraints
  -> PortfolioActionProposal  (advisory, carries an expiry)
```

Research **cannot** reach a venue. Asserted by parsing the import graph, not
by grep: `app.oms`, `app.brokers`, `app.execution`, `MetaTrader5` are all
forbidden imports in `app/research/opportunity.py` and
`app/portfolio/allocation.py`.

The default answer is `NO_ACTION` and it needs no justification. Given six
search families across five universes have failed to clear their own
permutation nulls, that is the ordinary path, not the exception.

## AI flow

The AI occupies an **advisory seat** in the pipeline and may only *decline*.
Its absence means "no opinion", and no opinion is not approval — approval is
the RiskEngine's and nothing else's. There is no code path from a model to a
venue.

## Windows application flow

**Does not exist.** No PyInstaller spec, no Inno/NSIS script, no
electron-builder, no MSIX. Today the system runs as Docker Compose (postgres,
redis, api, worker, frontend, nginx) plus a `.bat` for Path A.

The stack constrains what is possible: FastAPI + Postgres + Redis + Next.js
does not become one `.exe`. The realistic target is an installer that
provisions the runtime and registers a Windows Service. That is its own level.

## Where things are

| | |
|---|---|
| Configuration | `.env` (gitignored), `.env.example`, `backend/app/core/settings.py` |
| Logs — platform | container stdout; `logs/` for harness sessions |
| Logs — harness | `logs/overnight-*.log` |
| Ledger | `data/track_record.jsonl` (gitignored) |
| Research output | `reports/*.json` (gitignored) |
| Migrations | `backend/alembic/versions/` — 27, head `0027_capital_reservations` |
