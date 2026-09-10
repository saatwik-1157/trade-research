# PROJECT_STATUS.md

Generated 2026-09-10 from the repository, the venue and the Windows power log.

**Current Status:** `AUDIT_COMPLETE` → `STABILITY_FIX_REQUIRED`

The audit is done and the highest-risk defect is fixed. The system is not
`PAPER_READY` because no session has yet survived to its own deadline.

| | |
|---|---|
| **Trading Mode** | `paper` — `live_trading: false`, 12 live blockers, `assert_demo` in code |
| **Account** | `5055473926 @ MetaQuotes-Demo` **[DEMO]** · flat: 0 positions, 0 pending · 99,989.49 USD |
| **Running processes** | none. MT5 terminal open and idle |
| **TradingView Status** | gateway built (hmac, age check, idempotency, body cap). **Never exercised end to end** — no broker account registered |
| **MT5 Status** | one adapter, one `order_send` on the platform path; 4 on the harness path |
| **Risk Status** | 30+ veto codes, RiskEngine is the final veto on the platform path. **Bypassed entirely by the harness path** |
| **Windows Build Status** | **none.** No PyInstaller/Inno/NSIS/electron-builder anywhere |
| **Stability** | 8 of 8 recent sessions terminated early; 0 ran the deadline flush |
| **Tests** | 7 toolkit gates pass (py3.14) · backend `ruff` + `mypy` clean, 411 files · 179 research/portfolio tests pass |

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

## In Progress

Nothing. Stopped for instruction, per L83 §37.

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
2. **No session reaches its deadline**, so the `--flat-by` flush — the one
   mechanism bounding the losing tail — has not run in 8 attempts.
3. **Foreign keys are unenforced across the test suite.** SQLite runs with the
   pragma off; only `orders.signal_id` is covered. 2,700 tests are weaker
   evidence than the count suggests.

## High Issues

- `webhooks/gateway.py` docstring claims risk/sizing/OMS "are not built" — all
  three exist. Misleads anyone auditing the gate.
- `SIGNAL_CREATED` has no consumer; `ExecutionWorker` polls instead.
- RiskEngine lacks weekly-loss, consecutive-loss and correlation vetoes.
- No watchdog across workers; no crash journal; no restart-loop limiting.

## What is NOT wrong

Stated because three were assumed: no live trading and none reachable without
ten deliberate code changes; **no AI in the path that traded** (`--rule random`,
no model consulted); no martingale or size escalation; **loss control held** —
max drawdown −65.50 on 100,000 (−0.065%).

## Next Recommended Action

**P1b — recoverable flush + console-independent session + `CRASH_REPORTS/`.**
Small, and it is what stops the tail surviving a dead session. Then P2, fence
the harness path.

Do not train a model. `ModelTrainingNeedAssessment` = **DO_NOT_TRAIN**: six
search families across five universes have already failed to clear their own
permutation nulls, and a model would search the same space with more parameters.

## Working tree

Uncommitted: the disconnect fix + 2 tests, the `tr_toolkit` packaging change,
the L83 module + 27 tests, the L86 module + 24 tests, and 7 documents.
Nothing committed. All gates green.
