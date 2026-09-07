# EXECUTION_WORKER_ACTIVATION.md

The execution worker was started, in paper, on 2026-09-06. What that changed,
what it did, and what it did not.

**Every figure below was read from the running deployment, before and after.**

---

## What was started

`EXECUTION_WORKER_ENABLED=true`, a new setting. It is **the only worker flag
that defaults false**, and the asymmetry is deliberate:

* `notifications_enabled` and `monitoring_enabled` default **true** — a
  platform whose monitoring starts only when somebody remembers to switch it on
  is a platform that misses the first outage.
* `execution_worker_enabled` defaults **false** — this is the worker that
  *acts* on trading signals, and a platform that begins consuming them because
  somebody deployed it is a platform that traded without anybody deciding to.

It starts **after** the 16-step recovery sequence, never before. A worker that
consumed a signal first would act on a platform whose unresolved orders had not
yet latched safe mode — and since the L45 C-1 fix that sequence can finally see
those orders, because the pipeline now writes the rows it reads.
`test_the_execution_worker_starts_after_the_recovery_sequence` asserts the
ordering against the source.

**Starting it cannot enable live trading**, and a test says so:
`TRADING_MODE` stays `paper`, `LIVE_TRADING` stays `false`,
`live_execution_allowed` stays false, and the RiskEngine refuses any mode
outside paper and demo regardless.

---

## What it did on the first pass

**119 signals were retired.** 118 had been recorded before the L45 F-1 fix, so
their `metadata` carries none of the five keys the worker reads — they are not
routable and never were. The worker marked each `vetoed` with
`signal_not_executable`.

That is a one-way state change on historical rows, so a snapshot was taken
first: `backups/signals-status-before-worker-20260906T055822Z.csv`, 118 rows of
`id,status`. Only `status` changed; every row, its metadata and its
`webhook_events` parent survive.

It is also the honest state. Those signals cannot execute, and leaving them at
`new` forever was the lie.

| | Before | After |
|---|---|---|
| signals `new` | 118 | **0** |
| signals `vetoed` | 0 | 119 |
| signals `expired` | 0 | 1 |
| **orders** | **270** | **270** |
| **trades** | **252** | **252** |
| positions | 1 | 1 |
| executions | 209 | 209 |

**Nothing traded.** Not one order, trade, position or execution was created.

---

## The live chain, observed end to end

Two alerts were posted through nginx to the running deployment.

**First — no bot runs that strategy:**

```
metadata.internal_symbol  EURUSD              <- never written before F-1
metadata.strategy_id      rsi_reversion       <- never written before F-1
metadata.account_id       (none)
metadata.not_executable   "no enabled bot runs strategy version sv-l41 in
                           paper; the alert is recorded and nothing executes it"
status                    vetoed
```

Before the F-1 fix this signal would have sat at `new` forever with the reason
recorded nowhere.

**Second — with a bot configured for that strategy version:**

```
metadata.account_id       0c4480c9-...        <- resolved from the bots row
metadata.bot_id           l51-demo-bot
metadata.risk_amount      100.0000            <- the bot's max_risk_per_trade
metadata.bracket_source   none
metadata.bracket_detail   "this bot has not opted into the alert's bracket, and
                           the platform has no bracket policy of its own"
```

The worker claimed it and the pipeline returned **`no_venue`**:

> no order manager is registered for account 0c4480c9-…. An adapter is
> registered by an operator, not by the API booting.

`no_venue` is **parked**, not retired — a condition that can clear. The signal
was reoffered every 2s and then **expired on its own** at 120s:

> the signal is 120s old against a 120s limit; a late alert describes a market
> that has moved

So a parked signal does not loop forever, and the staleness gate (stage 1)
correctly precedes the venue gate (stage 5). That was checked rather than
assumed.

The demonstration bot was removed afterwards.

---

## The remaining blocker: no adapter can be registered

**There is no route, admin action or configuration by which an operator can
register a broker adapter.** `OrderManagerRegistry` is empty at startup by
design — "registering an adapter is an operator action rather than a side
effect of the API booting" — but no operator action exists.

So the chain is complete and live **up to the venue boundary**, and the venue
seat cannot be filled through any interface. It is the same shape as the
defects this session fixed: a seat built, and nobody able to sit in it.

Closing it means deciding what an operator may point the platform at, which
touches the `BROKER_CREDENTIALS` step-up scope. **It was not built unprompted.**

---

## A defect this activation found

Migration `0026` used `server_default=sa.text("0")` for a boolean column. SQLite
accepts it; PostgreSQL refuses:

> column "use_alert_bracket" is of type boolean but default expression is of
> type integer

The migration tests skip without a scratch PostgreSQL, so **the first thing to
catch this was applying it to the running database**, where it failed cleanly
and rolled back. Fixed to `sa.false()`, which renders correctly per dialect,
and the image was rebuilt so the repository and the deployment agree.

---

## Operational notes

* The flag lives in a gitignored `.env`, so it survives a restart. **No secret
  is written there** — `TV_WEBHOOK_SECRET` must be exported in the shell that
  runs `docker compose up`, and unset means the gateway refuses every alert,
  which is the correct default.
* To stop the worker: remove or set `EXECUTION_WORKER_ENABLED=false` and
  restart the API. The log line `execution_worker_idle` names the reason.
* The worker logs `execution_worker_started` at WARNING with `trading_mode`,
  `live_trading`, `safe_mode` and the registered `order_managers`, so the
  question "could this have reached a venue?" is answered on one line.
