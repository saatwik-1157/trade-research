# DEMO_GUIDE.md

How to see the whole execution flow work, in about ten seconds, with no
broker, no database and no credentials.

```bash
cd S:\PROJECTS\trade-research
backend\.venv\Scripts\python.exe demo.py
```

Exit code `0` means every scenario reached its expected outcome — **including
the eight expected to refuse.** A demo that cannot fail demonstrates nothing.

```
python demo.py --list      # scenario names
python demo.py -k risk     # just the risk scenario
NO_COLOR=1 python demo.py  # no ANSI codes, for piping to a file
```

## What it actually runs

The real production modules, not stand-ins:

| Real | Simulated |
|---|---|
| `app.execution.ExecutionPipeline` | `app.brokers.fake.FakeBroker` (paper mode) |
| `app.risk.RiskEngine` + `KillSwitches` | |
| `app.oms.OrderManagerRegistry` | |
| `app.sizing` fixed-risk calculator | |
| `app.research.opportunity`, `app.portfolio.allocation` | |

No `MetaTrader5` import, no network, no database, no credential appears in
`demo.py`. The venue is a simulator this repository already ships and tests.

**Why a script and not a running server:** the full stack needs Postgres and
Redis via Docker, and the Docker daemon is not always up. The pipeline needs
neither. Running it directly shows the same code that runs in production
without standing up infrastructure that would add nothing to what is shown.

## The ten scenarios

Every one prints the same trace, because that is the point — the same gates
run each time and only the refusing gate differs.

| # | Scenario | Outcome | Order created |
|---|---|---|---|
| 1 | Valid trade | `filled` | **yes** — 0.20 lots, risk 100.00 |
| 2 | Risk rejected (`max_open_positions=0`) | `risk_vetoed` | no |
| 3 | Duplicate signal | `duplicate_signal` | no |
| 4 | Stale signal (3600s vs 120s limit) | `signal_stale` | no |
| 5 | Venue unavailable | `no_venue` | no |
| 6 | Kill switch | `kill_switch` | no |
| 7 | Strategy disabled | `strategy_disabled` | no |
| 8 | No stop loss | `sizing_refused` | no |
| 9 | Symbol not mapped | `spec_incomplete` | no |
| 10 | Research → `NO_ACTION` | `NO_ALLOCATION` | no |
| 11 | Safety invariants | 5 checks | — |

Scenario 1 is the only one allowed to have created an order, and the harness
checks **both** conditions on every scenario: the outcome name *and*
`created_order`. Checking the name alone would pass if a refusal were renamed
while still sending an order, which is the failure that matters.

## Reading a trace

```
DEMO 2 - RISK REJECTED
  WHY DID IT NOT TRADE?
    TradingView   : accepted (source=tradingview, auth=strong)
    Strategy      : valid
    AI advisory   : no opinion (advisory seat empty)
    RISK          : veto
    SIZING        : not reached
    OMS / venue   : no order created
    OUTCOME       : risk_vetoed
    detail        : max_open_positions: open position count is unknown
    execution_id  : c6be4b8b-1d26-4f78-a71d-6bd022e00b61
    order created : False
```

`execution_id` is the correlation id. It is minted once when a signal is
picked up and travels through the risk decision, the sizing result, the
order's intent, the logs and the events — so *"why did this happen"* has one
string to follow.

Note the detail on scenario 2: **"open position count is unknown"**, not
"0 positions open". That is L60's rule showing through — missing is never
safe, and an unknown count refuses rather than assuming room.

## Crash recovery

Not in `demo.py`, because faking a restart in a script would demonstrate the
fake. The real evidence is a dedicated suite:

```bash
cd backend
.venv\Scripts\python.exe -m pytest tests/test_execution_durability.py -v
```

27 tests, all passing, including:

- `test_an_unknown_order_is_not_resent_after_a_restart`
- `test_a_completed_order_is_not_repeated_after_a_restart`
- `test_the_order_is_recorded_before_the_venue_is_called`
- `test_an_order_that_cannot_be_recorded_is_never_sent`
- `test_the_durable_guard_is_never_more_permissive` — parametrised over all 13 order states
- `test_the_restart_regression_is_capable_of_failing` — a test that proves the test can fail

## What the demo does not show

- A live MT5 connection (needs the terminal and a demo login)
- The web dashboard (needs Docker for Postgres and Redis)
- The overnight harness — that path is `start-trading.bat`, and it is **not**
  the pipeline shown here. See `FINAL_SYSTEM_REPORT.md` on the two paths.
