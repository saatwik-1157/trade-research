# PROJECT_STATUS.md

Generated 2026-09-11 from the repository, the venue and the Windows power log.

**Current Status:** `STABILITY_PROVEN` → `HARNESS_FENCE_REQUIRED (P2)`

The audit is done, the stability defects are fixed, and one full-length session
has now run to its own deadline and flushed. What blocks `PAPER_READY` is no
longer stability: it is the one remaining critical finding, that the path which
has traded this account is not the path the safety machinery sits on.

| | |
|---|---|
| **Trading Mode** | `paper` — `live_trading: false`, 12 live blockers, `assert_demo` in code |
| **Account** | `5055473926 @ MetaQuotes-Demo` **[DEMO]** · flat: 0 positions, 0 pending · 99,970.28 USD |
| **Running processes** | none. MT5 terminal open and idle |
| **TradingView Status** | gateway built (hmac, age check, idempotency, body cap). **Never exercised end to end** — no broker account registered |
| **MT5 Status** | one adapter, one `order_send` on the platform path; 4 on the harness path |
| **Risk Status** | 33+ veto codes — weekly loss, consecutive losses and correlation added at P4. RiskEngine is the final veto on the platform path. **Bypassed entirely by the harness path** |
| **Windows Build Status** | **none.** No PyInstaller/Inno/NSIS/electron-builder anywhere |
| **Stability** | **one full-length session has reached its deadline.** `20260910-224639`: 433 minutes, 1,298 passes, `flat-by 06:00` closed 6 at 05:59:24, exit 0, watchdog correctly declined to restart. One session is evidence, not a rate |
| **Tests** | 8 toolkit gates pass (py3.14) · **full backend suite 2,976 passed / 0 failed** (21m33s) · `ruff` + `mypy` clean, 411 files |

## Completed

- Full architecture audit → `docs/ARCHITECTURE_AUDIT.md` (A–Q)
- Crash root-cause analysis → `EMERGENCY_STABILITY_AUDIT.md`
- Loss classification over 476 venue-read trades → `LOSS_ROOT_CAUSE_REPORT.md`
- **Disconnect defect fixed**: `account_info()` returning `None` was caught as
  a formatting error; ran 23 blind passes with `--max-daily-loss` unable to
  evaluate. Now fails closed, with its own failure budget. 3 sites fixed,
  2 regression tests.
- **L83 opportunity research core** → `app/research/opportunity.py`, 27 tests
- **L86 allocation validation** → `app/portfolio/allocation.py`, 24 tests. Reuses
  `portfolio.state`/`decision`/`scenario`/`stress`/`control`/`horizon`; no engine duplicated
- **P4 risk vetoes**: weekly loss (halts, and holds its own lock), consecutive
  losses (vetoes, and deliberately does not latch), correlation (FAILS when a
  limit is configured without the data to evaluate it). A MISSING input fails
  each check rather than passing it
- **Deadline evidence banked** — see Stability above. The crash journal now
  holds 5 sessions: one 11ms death from the missing detached stdout, since
  fixed; two short completions; one full-length overnight run that flushed
- **Ledger merged to 751 trades** (`data/track_record.jsonl`), with the 9
  platform trades that carry the harness tag excluded by id again. Account
  `5055473926` now stands at 499 trades, net −30.39, 09-04 to 09-11; the older
  252 stay `unrecorded`. R-multiple n=741, mean −0.014R, t=−0.92 — still no
  edge, which is what every earlier read said too

## In Progress

Nothing. P1b is complete and its evidence now exists. P2 is next, and not
started.

## Blocked

- **Base rates, expectation gaps, channel and guidance intelligence** — no data
  source exists. Not stubbed; they return `insufficient_data` naming the gap.
- **Path B end-to-end demo validation** — `accounts_broker: 0`, so the platform
  path refuses with `no_venue`.

## Critical Issues

1. **Two independent paths to a broker order.** The harness
   (`take_profit.py` → `mt5_paper.py`, 4 × `order_send`) imports nothing from
   `app` — no RiskEngine, no OMS, no kill switch, no journal. It is the only
   path that has ever traded this account. Every §46 invariant holds on the
   platform path and is violated on this one.
2. ~~**No session reaches its deadline.**~~ **Closed 2026-09-11.** Session
   `20260910-224639` ran 433 minutes, made 1,298 passes, and the
   `--flat-by 06:00` flush closed all 6 open positions at 05:59:24 before
   exiting 0. The watchdog logged `the session finished normally` and did not
   spend a restart. That is the field evidence P1b lacked. One run is not a
   reliability figure, so the next long session gets checked the same way
   rather than assumed.
3. **Foreign keys are unenforced across the test suite.** SQLite runs with the
   pragma off; only `orders.signal_id` is covered. 2,700 tests are weaker
   evidence than the count suggests.

## High Issues

- `SIGNAL_CREATED` has no consumer; `ExecutionWorker` polls instead.
- ~~RiskEngine lacks weekly-loss, consecutive-loss and correlation vetoes.~~
  **Closed at P4** — all three exist, and each fails closed on a MISSING input.
- ~~No watchdog across workers; no crash journal; no restart-loop limiting.~~
  **Closed at P1b for the harness** — it has all three, and the watchdog's
  restart budget does not refill. Still absent across the `app/` workers, which
  is where the finding was originally aimed.

## What is NOT wrong

Stated because three were assumed: no live trading and none reachable without
ten deliberate code changes; **no AI in the path that traded** (`--rule random`,
no model consulted); no martingale or size escalation; **loss control held** —
max drawdown −65.50 on 100,000 (−0.065%).

## Next Recommended Action

**P2 — fence the harness path.** Nothing stands in front of it now: P1b is
done and its overnight evidence is banked, and P4 closed the last HIGH finding
from the audit. What remains is the critical one. `tools/take_profit.py` →
`tools/mt5_paper.py` reaches a broker through four `order_send` sites
(`mt5_paper.py:485, 526, 540, 644`) while importing nothing from `app/` — no
RiskEngine, no OMS, no kill switch, no journal. It is the only path that has
ever traded this account, so the three vetoes added at P4 could not see one of
the 21 trades opened last night.

Do not train a model. `ModelTrainingNeedAssessment` = **DO_NOT_TRAIN**: six
search families across five universes have already failed to clear their own
permutation nulls, and a model would search the same space with more parameters.

## Working tree

Clean. Everything above is committed on `audit-fix-and-demo`, through `b3e0313`
(the P4 vetoes). The ledger and the reports are gitignored, so the merge of
2026-09-11 shows up in `data/track_record.jsonl` and `reports/track_record.json`
rather than in the diff.

`PROJECT_STATE.json` is still stamped 2026-09-07 and was deliberately not
regenerated: it measures the running deployment, and neither `tr-postgres` nor
the API is up. Run `python tools/project_state.py --write` with the stack
running rather than editing it by hand.
