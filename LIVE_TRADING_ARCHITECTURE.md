# Live trading architecture

What was added at L70, where it sits, and — as importantly — what it is not
allowed to do.

## The one-paragraph version

The platform already had a complete execution path with the RiskEngine as its
final veto. L70 did not touch it. It added a layer **above** that path which
decides whether live execution may be turned on at all, and that layer can only
ever refuse: it holds no adapter, mints no approval, and has no code path to an
order.

## Two different questions

    LiveTradingGate    may live execution be TURNED ON?
    RiskEngine         may THIS ORDER be sent?

They are not substitutes and the gate never runs in the engine's place. Passing
the gate authorises a session; every individual order inside that session still
needs an `Approval` that only `RiskEngine.approve` can mint, and the OMS still
refuses to submit without one.

## Where the layer sits

```
  operator
     |
     v
  ActivationMachine        DISABLED -> PRECHECK -> READY -> ARMED
     |                              -> ACTIVATING -> ACTIVE
     |  (each step consults)
     v
  LiveTradingGate  --reads-->  settings, allowlist, connected account,
     |                          venue, market data, strategy, model,
     |                          risk, OMS, positions, platform health
     v
  READY_FOR_LIVE / NOT_READY_FOR_LIVE
     |
     |  ... and only then does the EXISTING path run, unchanged:
     v
  Signal -> StrategyEngine -> AI seat -> RiskEngine -> Sizing
         -> RiskEngine binds -> OMS -> BrokerAdapter -> MT5 -> broker
         -> PositionManager -> Journal -> Portfolio -> Analytics
         -> Monitoring -> Recovery / Reconciliation
```

Everything below the dashed transition is `app/execution/pipeline.py` and was
built at L20–L38. The activation layer added nothing to it and removed nothing
from it.

## The new modules

| Module | Lines | What it does |
|---|---|---|
| `backend/app/live/allowlist.py` | ~90 | which account identifiers may trade live; empty permits none |
| `backend/app/live/gate.py` | ~700 | 34 checks in 9 groups, each PASS / FAIL / WARNING |
| `backend/app/live/state.py` | ~280 | the twelve-state activation machine and its legal moves |
| `backend/app/live/preflight.py` | ~250 | `python -m app.live.preflight` |
| `backend/tests/test_live_gate.py` | ~600 | 82 tests, every one a refusal test |

**Containment is asserted, not asserted-to.** `test_live_gate.py` parses every
module in `app/live/` and fails if any of them imports `app.oms`,
`app.brokers`, `app.risk`, `app.execution`, `app.paper`, `app.positions` or
`app.bots`. A module in the activation layer that could import an order manager
could also call one, and this package would have become a second execution
path — the exact thing the brief forbids.

## The state machine

```
  DISABLED ──> PRECHECK ──> READY ──> ARMED ──> ACTIVATING ──> ACTIVE
                  │            │         │           │            │
                  └─> PRECHECK_FAILED    │           │            ├─> PAUSED
                             │           │           │            │
                  every live state ──────┴───────────┴────────────┴─> SAFE_MODE
                                                                  └─> EMERGENCY_STOP
                                                                          │
                                              DEACTIVATING <──────────────┘
                                                    │
                                              DEACTIVATED ──> DISABLED
```

Three absences carry the design:

- **No edge `DISABLED -> ACTIVE`.** Nor `DISABLED -> ARMED`, nor
  `READY -> ACTIVE`. The table is consulted, so a caller cannot skip a step by
  asking politely.
- **`PRECHECK_FAILED` never advances.** Its only moves are back to `PRECHECK`
  or out to `DISABLED`. "It failed, arm it anyway" is not expressible.
- **`EMERGENCY_STOP` has one exit and it is downward.** Getting back to trading
  means walking the whole path again from `DISABLED`.

`arm()` refuses a gate report that did not pass. `activate()` requires the
machine to be in `ARMED` *and* a fresh passing report — the one `arm()` saw is
evidence about when `arm()` ran. Every transition requires an actor and a
reason of at least eight characters; a transition nobody signed is one nobody
can be asked about.

## Restart behaviour

A new process starts at `DISABLED`. That is deliberate and it is the answer to
§20: no restart, redeploy, container bounce or browser refresh can reach
`ACTIVE`, because the only path there runs through a fresh gate pass and two
signed transitions.

State is held in memory, exactly as `app.recovery.safe_mode.SafeMode` holds its
latch and for the same reason: a stored "we were ACTIVE" flag outlives the
process that meant it, and the first thing a crashed live deployment must not
do is come back trading because a row said it used to be.

Carrying an operator's intent across a restart is possible — it needs a
persisted record *plus* a fresh gate pass *plus* a human — and it is not built.
What is built is the guarantee that no restart alone reaches `ACTIVE`.

## Configuration added

All fail closed, and **none of them can enable live trading**: `LIVE_GATES` is
untouched by every one, so `live_execution_allowed` stays False whatever they
say.

| Variable | Default | Meaning |
|---|---|---|
| `LIVE_ALLOWED_ACCOUNT_IDS` | `""` | comma-separated. **Empty permits nothing** |
| `LIVE_EXPECTED_ACCOUNT_ID` | `""` | compared against the *connected* account |
| `LIVE_EXPECTED_SERVER` | `""` | compared against the *connected* server |
| `ENABLE_LIVE_TRADING_CONFIRMATION` | `""` | must equal `YES_I_UNDERSTAND` |
| `LIVE_QUOTE_MAX_AGE_SECONDS` | `60` | quote freshness for the gate |

`LIVE_ALLOWED_ACCOUNT_IDS` is a comma-separated **string**, not a `list[str]`.
Measured, not assumed: pydantic-settings parses a list field from the
environment as JSON only, and `CORS_ORIGINS=a,b` raises `SettingsError` at
startup rather than splitting. An allowlist that refuses to boot on the obvious
spelling is an allowlist somebody clears to make the process start. (The
`cors_origins` description claimed "comma-separated or JSON"; it was wrong and
has been corrected.)

## What was deliberately not built

- **A live broker adapter.** `MT5Adapter` is demo-only and calls
  `tools/mt5_paper.assert_demo`, which refuses `trade_mode != 0` in code.
- **Broker credential storage.** The registration route stores none and says so.
- **A persisted live session record.** It needs a migration and a table; the
  activation machine's `history` carries the transitions in the meantime.
- **An HTTP activation endpoint.** Arming over the API needs step-up
  re-authentication wiring and a role; the CLI is the surface today.

Each of these is named in `LIVE_TRADING_READINESS_REPORT.md` as an outstanding
item rather than quietly omitted.
