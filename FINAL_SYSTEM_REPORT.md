# FINAL_SYSTEM_REPORT.md

2026-09-10, amended 2026-09-11 · every figure below was measured, not estimated.

## SYSTEM STATUS: `DEMO_READY`

Not `PAPER_READY`, and the distinction is the whole report.

The **execution pipeline** is demonstrable end to end, right now, with one
command: 11 of 11 scenarios reach their expected outcome, one fill and eight
distinct refusals, exit code 0.

The **overnight harness** — the only thing that has actually traded — now
finishes a session. Eight of eight runs had died before their deadline when
this report was written; on the night of 2026-09-10 one ran the full 433
minutes and flushed at 05:59:24 (§9 amendment). What still holds `PAPER_READY`
back is finding #1 below, not stability: the path that traded is not the path
the safety machinery sits on.

---

## 1. Architecture

FastAPI + SQLAlchemy 2 async + asyncpg + Alembic + Redis; Next.js/React
frontend; PostgreSQL; Docker Compose; MetaTrader5 via its Python package.

| | Measure |
|---|---|
| Backend | 327 modules · 90,084 lines |
| Frontend | 122 sources · 16,879 lines · 25 routes |
| Tests | 79 backend files · ~2,750 functions · 8 standalone toolkit gates |
| Migrations | 27, head `0027_capital_reservations` |
| Research toolkit | 27 scripts, own CI, no execution authority |
| Docs | 180 root markdown + 4 in `docs/` |

## 2. What was broken

| # | Finding | Severity | State |
|---|---|---|---|
| 1 | **Two independent paths to a broker order.** The harness (`take_profit.py` → `mt5_paper.py`, 4 × `order_send`) imports nothing from `app/` — no RiskEngine, no OMS, no kill switch, no journal. It is the only path that ever traded. | CRITICAL | **reduced 2026-09-11** — the decision was taken (route through RiskEngine) and built: `place()` sends no live order without an `Approval`. Still no OMS and no `orders` row on this path |
| 2 | **A dropped MT5 terminal read as a formatting error.** `account_info()` returns `None`; `bal.balance` raised `AttributeError`; caught as "a line of log is not worth a session". Ran 23 blind passes with `--max-daily-loss` unable to evaluate. | CRITICAL | **fixed** |
| 3 | **Risk limits failed open.** `history_deals_get(...) or []` turned a disconnect into 0.00 P&L, so the daily-loss limit silently stopped being a limit. The docstring had warned of exactly this. | CRITICAL | **fixed** |
| 4 | **Unguarded startup account read** — a terminal already down produced a stack trace instead of a reason. Found *by writing the test* for #2. | HIGH | **fixed** |
| 5 | **8/8 sessions die before their deadline**, so the `--flat-by` flush — the one mechanism bounding the losing tail — has never run. | CRITICAL | **closed 2026-09-11** — session `20260910-224639` reached its deadline and flushed 6 |
| 6 | Foreign keys unenforced across the suite (SQLite pragma off; only `orders.signal_id` covered). | HIGH | **open** — documented in `KNOWN_TEST_LIMITATIONS.md` |
| 7 | `webhooks/gateway.py` docstring claimed risk/sizing/OMS "are not built"; all three exist, and it also imports `execution.routing` now. | MEDIUM | **fixed** |
| 8 | Demo MT5 login number in 14 tracked documents. | LOW | **open** |
| 9 | `OrderStatus` defined twice under one name — `brokers/base.py` (3 venue values) and `oms/state.py` (13 platform values). Different layers, genuinely different types. | LOW | **documented, not changed** |

### Crash root cause

Three hypotheses, two refuted by evidence:

- **Not sleep.** Windows `Kernel-Power` shows no Modern Standby during the
  three most recent sessions. The keep-awake hold works.
- **Not an unhandled exception.** Zero `Traceback` and zero
  `KeyboardInterrupt` across all 27 session logs; the `finally` that releases
  the keep-awake never printed.
- **External termination.** Logs stop mid-pass between 20-second ticks with no
  final line. A closed console, not a fault in the program — which is why more
  exception handling inside the loop would not have saved one of the eight.

Which of those ended each session was unmeasured, because nothing recorded
it. `CRASH_REPORTS/` now does — see 9b.

## 3. Loss analysis

476 closed trades read from the venue, 2026-09-04 to 09-10:

```
net                  -10.51 USD on a 100,000 demo deposit   (-0.0105%)
win rate             80.5%  (383W / 87L)
average win / loss   0.73 / -3.35    payoff ratio 0.218
break-even win rate  82.1%
max drawdown         -65.50  (-0.065%)
```

**Of 470 decided trades, 386 wins were needed to break even and 383 arrived.**
Three trades in 470. The cause is the `--min-profit 0.50` harvest geometry
against a 1.5×ATR stop — a ~1:6 reward shape needs ~82% accuracy, and
`--rule random` supplies about 80.5%.

Classification: **BAD STRATEGY**, not BAD SOFTWARE. No AI was in the path (no
model is consulted anywhere in it). No martingale, no size escalation.
**EXCESSIVE RISK is not present** — loss control held throughout.

## 4. What was fixed

- `tools/mt5_paper.py` — `VenueUnreadable`; `realised_today()` and
  `own_positions()` fail closed. An empty tuple still means empty; only `None`
  raises. The distinction between "nothing is open" and "we could not ask" is
  the entire fix.
- `tools/take_profit.py` — three unguarded `account_info()` sites; a **separate**
  failure budget for blind passes (`misses` resets on every good harvest, and
  harvest kept succeeding — a shared counter would refill forever and never
  fire).
- `tests/test_rule_backtest.py` — 2 regression tests modelling the observed
  2026-09-10 sequence.

## 5. What was cleaned / merged / removed

**Merged:** `fix/continuous-session` → `main` (fast-forward, 26 commits, linear
history preserved).

**Cleaned:** four duplicated `sys.path` hacks reaching into `tools/` — three
different nesting depths, two calling the repo root `root` and two calling
`backend/` the same — replaced by one `app/core/toolkit.py` that prefers an
installed `tr_toolkit` and falls back to the sibling directory. `tools/` is now
an installable package (`pip install -e ./tools`, wheel builds, 27 modules).

**Removed:** nothing. Per §47, uncertain items were kept and documented.

**Single source of truth verified:** one `RiskEngine`, one
`OrderManagerRegistry`, one `ExecutionPipeline`, one `Settings`, one broker
adapter (the two `order_send` calls in `brokers/mt5.py` are submit and cancel).
Zero bare `except:`. One `except Exception: pass`, at `api/v1/brokers.py:432`,
carrying a `noqa` and a stated reason.

## 6. Demonstration results

`python demo.py` — **11/11, exit code 0.** Real `ExecutionPipeline`,
`RiskEngine`, `OrderManagerRegistry` and sizing against a paper simulator. No
`MetaTrader5` import, no network, no database, no credentials.

| # | Scenario | Outcome | Order |
|---|---|---|---|
| 1 | Valid trade | `filled` | **yes** — 0.20 lots, risk 100.00 |
| 2 | Risk rejected | `risk_vetoed` | no |
| 3 | Duplicate signal | `duplicate_signal` | no |
| 4 | Stale signal | `signal_stale` | no |
| 5 | Venue unavailable | `no_venue` | no |
| 6 | Kill switch | `kill_switch` | no |
| 7 | Strategy disabled | `strategy_disabled` | no |
| 8 | No stop loss | `sizing_refused` | no |
| 9 | Symbol not mapped | `spec_incomplete` | no |
| 10 | Research → NO_ACTION | `NO_ALLOCATION` | no |
| 11 | Safety invariants | 5/5 | — |

Each scenario checks **both** the outcome name and `created_order` — the name
alone would pass if a refusal were renamed while still sending an order.

Five of these expectations were initially wrong *in the demo's favour*: I
predicted `risk_vetoed` where the pipeline actually refuses earlier and more
specifically (`signal_stale`, `no_venue`, `strategy_disabled`,
`sizing_refused`). The expectations were corrected to what the system does;
no assertion was weakened, and each still requires `created_order=False`.

**Crash recovery:** `tests/test_execution_durability.py` — 27/27 passing,
including `test_an_unknown_order_is_not_resent_after_a_restart` and
`test_the_restart_regression_is_capable_of_failing`.

## 7. Test results

| Suite | Result |
|---|---|
| 8 toolkit gates (py3.14) | **8/8 PASS** |
| `demo.py` | **11/11**, exit 0 |
| `test_execution_durability.py` | **27/27** |
| L83 + L86 + portfolio + selection | **179 passed** |
| `ruff check` / `ruff format` | clean, 412 files |
| `mypy` | **no issues in 411 source files** |
| Full backend suite | **2,976 passed, 0 failed**, 7 skipped, 21m33s |

## 8. Security audit

Clean: no credential-shaped assignment in any tracked source; `.env` not
tracked; `.env.example` present; no hard-coded developer paths in tracked
`.py`/`.ts`/`.bat`; prod compose publishes only nginx 80/443 (CI-verified);
image runs non-root (CI-verified); the TradingView secret is unset by default
and unset means **every alert is refused**.

One finding: the demo MT5 login appears in 14 tracked documents. Demo account,
no password, no money — LOW, but it is an account identifier in a repo.

## 9. Known limitations

- **No Windows packaging exists.** No PyInstaller, Inno, NSIS, electron-builder
  or MSIX. FastAPI + Postgres + Redis + Next.js does not become one `.exe`;
  the realistic target is an installer that provisions the runtime and
  registers a Windows Service.
- **Path B has never traded.** `accounts_broker: 0` → refuses with `no_venue`.
- **No soak test has been run.** No session has survived long enough to start one.
- **Docker daemon was down** during this work, so Postgres/Redis-backed
  integration was not exercised; the demo runs the pipeline directly, which
  needs neither.
- **Research base rates, expectation gaps, channel and guidance intelligence
  have no data source** and return `insufficient_data` naming the gap.
- No claim is made about profitability. Six search families across five
  universes have failed to clear their own permutation nulls.

## 9b. P1b, done after the report was first written

Three parts, all committed and gated:

- **A crash journal.** `tools/crash_report.py` rewrites one atomic JSON record
  per pass with the session's last known state. A kill cannot prevent a write
  that already happened, so even `taskkill /F` now leaves a record saying when
  the session was last alive and what it was holding. The next session reads
  those and refuses to start quietly — it prints what the last one abandoned
  and how to close it.
- **A console guard.** `tools/console_guard.py` installs a Windows console
  control handler, turning the most likely cause of the eight deaths from
  "killed mid-pass" into "asked to stop, ran the flush". Its limits are
  documented rather than implied: a few seconds of grace that belong to
  Windows, nothing at all for `taskkill /F` or a suspend, and a closed lid is
  not a console event.
- **A detached launch.** `start-trading.bat --detach` runs under `pythonw`, so
  there is no console to close and the event never arrives. This is the
  stronger fix; the guard covers the launches that did not use it.

Held by test, not by intention: a stop request must still run the flush (so it
does not set `halted`, which would suppress it); a heartbeat that raises must
not end a session; and a blind pass records `positions_open` as **UNKNOWN**,
never 0, because a record claiming nothing was open would send a recovery run
away empty.

Verified end to end: an unfinished record was written, and a new session
printed `PREVIOUS SESSION DID NOT FINISH: ... positions_open=7` with the
`--harvest-only` remedy beside it.

**Amended 2026-09-11 — the field evidence is now in.** Session
`20260910-224639` ran from 22:46 to 05:59, 433 minutes and 1,298 passes, and
the `--flat-by 06:00` flush closed all 6 open positions at 05:59:24 before the
process exited 0. `CRASH_REPORTS/session-20260910-224639.json` records
`"status": "completed"` with `flushed: 6` and `positions_open: 0`, and the
watchdog logged `the session finished normally` without spending a restart.
Realised —14.95 over the night, 21 opened and 12 harvested.

One session is evidence that the mechanism works, not a reliability rate. The
same three artefacts — the journal record, the log tail and the watchdog line
— are what the next long run gets read against.

## 10. Next action

*Amended 2026-09-11.* P1b is done and its evidence is in (§9). P4 added the
weekly-loss, consecutive-loss and correlation vetoes, closing the last HIGH
finding from the audit.

**P2 is built.** The decision was to route through the Risk Engine rather than
to mark the harness unattended-only, and it turned out to be cheap:
`app/risk/engine.py` is pure stdlib and synchronous, so `tools/risk_gate.py`
reaches it with `backend/` on `sys.path` and no database, event loop or running
platform. `place()` now refuses a live order that carries no decision.

**Next is evidence, not code.** One overnight session behind the fence, read
afterwards for the `risk` block every order now carries. Then P5 — foreign
keys and a Postgres integration job — which is the last CRITICAL that has had
nothing done to it.

Three levels of research intelligence still sit above a harness that has traded
for three weeks without an edge. The ordering is better than it was; it is not
yet right.
