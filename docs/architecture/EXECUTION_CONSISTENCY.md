# EXECUTION_CONSISTENCY.md

Do backtest, replay, paper, shadow and demo use the same rules? Where they
differ, is the difference expected, acceptable, or dangerous?

Written at L44. **Differences are listed, not smoothed over.**

---

## The five execution paths

| Path | Decision chain | Execution | Exists? |
|---|---|---|---|
| **Backtest** | strategy rules only | `tools/rule_backtest.simulate` | yes |
| **Replay** | strategy → signal | paper orders | yes |
| **Paper** | full pipeline | `FakeBroker` — simulated fills | yes |
| **Shadow** | full pipeline | `ShadowBroker` — **acknowledges, never fills** | yes, added at L44 |
| **Demo** | full pipeline | MT5 demo account via `MT5Adapter` | **adapter EXISTS; never connected** |

## What is genuinely shared

**One simulator, not two.** `app/backtest/runner.py` wraps
`tools/rule_backtest.simulate` rather than reimplementing it, and
`test_indicators_match_live` exists to stop the two drifting. This is the single
most important consistency property in the repository and it was got right long
before L44.

**One pipeline for paper, shadow and demo.** `ExecutionPipeline.process` is the
only path from a signal to an order. Shadow does not fork it — a test walks the
AST of `pipeline.py`, `risk/engine.py` and `sizing/calculator.py` and fails if
any of them contains a `"shadow"` literal it could branch on, or imports the
shadow adapter.

## Paper vs shadow, measured

Same pipeline, same `RiskLimits`, same `SizingMethod`, same signal, same
contract spec. The only difference is which adapter is registered.

| Stage | Shadow | Paper | |
|---|---|---|---|
| risk decision | `approve` | `approve` | same |
| risk approved | True | True | same |
| sizing volume | 0.20 | 0.20 | same |
| order created | True | True | same |
| orders at venue | 1 | 1 | same |
| **outcome** | `order_submitted` | `filled` | **differs** |
| **positions** | 0 | 1 | **differs** |
| **filled quantity** | 0 | 0.20 | **differs** |
| **order status** | `accepted` | `filled` | **differs** |

**Every difference is downstream of the acknowledgement, and none is upstream
of it.** That is the property shadow mode exists to have: the decisions are the
ones the platform would really make, and nothing is executed.

Asserted by `test_shadow_and_paper_make_the_same_decision`.

## Expected differences

| Difference | Why it is expected |
|---|---|
| Shadow reports no fill, no position, no P&L | A simulated fill is a fact nobody observed. Shadow answers *"what would it have decided?"*, never *"what would it have earned?"* |
| Shadow's `close_position` **raises** | Nothing was opened. A polite success would let a position manager believe it had flattened something. |
| Backtest applies its own fee, spread and slippage model | It runs over history with no venue at all. `BacktestConfig` states the assumptions. |
| Replay is paper-only | By design; it cannot reach a live adapter. |
| Backtest does not run the AI seat or the OMS | It measures a rule, not an execution path. **This is the largest expected gap** — see below. |

## Acceptable differences

**Shadow presents as `mode="paper"`.** The obvious choice was `mode="shadow"`,
and it did not work: `RiskEngine` checks `proposal.mode in ("paper", "demo")`
and vetoes anything else, so every shadow signal died at the risk gate before
reaching the adapter and a shadow run recorded **nothing**.

The fix could have been to add `"shadow"` to that allow-list. **It was not**,
deliberately: that list is a safety control that fails closed on an unknown
mode, and widening a safety control so a new feature fits is backwards. The
feature adapts to the control.

The consequence, stated plainly: the OMS registry's mode check no longer
distinguishes a shadow adapter from a paper one, so registering `ShadowBroker`
for an account means paper signals reach it and stop there. That is the intended
use. The dangerous direction is unreachable — a shadow adapter cannot satisfy a
`live` signal, because its mode is not `live`.

**Shadow reports a realistic equity.** The first version reported zero, on the
reasoning that a shadow run has no money. Risk then vetoed every signal for
insufficient equity and shadow recorded nothing — the same failure as the mode,
found the same way. Shadow's isolation is the absence of **fills**, not the
absence of money. Set `ShadowBroker.equity` to the equity of the account being
shadowed so the risk and sizing verdicts are the ones that account would really
have produced.

## Dangerous differences

**None found.** Specifically checked and clear:

* No path reaches a venue except through `BrokerAdapter` — verified by AST walk
  across the whole application.
* No stage above the adapter can tell which adapter it holds.
* Risk and sizing are identical across paper and shadow — measured, above.
* `fill_source` travels with every fill: `"simulator"` for paper, `"shadow"`
  for shadow, and a real adapter would set its own. **A simulated or shadow
  number can never be mistaken for a broker one downstream.**

## Unresolved differences

### 1. Backtest does not run the full pipeline — **the big one**

A backtest evaluates strategy rules against history and applies its own
execution model. It does **not** run the AI seat, the RiskEngine, position
sizing or the OMS.

So a strategy that backtests well has **not** been shown to survive the risk
limits and sizing rules that would apply to it live. The two answer different
questions, and the platform does not currently have a path that answers
"what would this strategy have done *through the whole pipeline* over history?"

Market replay is the closest thing — it runs strategy → signal → paper orders —
but does not exercise the full risk and sizing chain either.

**Status: OPEN.** This is a design gap rather than a defect, and closing it
means running replay through `ExecutionPipeline`. Not attempted at L44 because
it is a feature, not hardening.

### 2. Demo has never run — **BLOCKED**

A complete demo adapter exists -- `app/brokers/mt5.py`, `mode = "demo"` as a
class attribute, wrapping `tools/mt5_paper` rather than reimplementing it. It
has **never been connected**: there is no terminal on this host and no
credentials. So the demo column of every table above is unverified, and real
retcodes, partial fills, requotes, slippage and reconnect behaviour are unknown.

Its `connect()` calls the toolkit's `assert_demo`, which raises `RefuseToTrade`
on REAL, CONTEST and any unrecognised `trade_mode` -- and that refusal is
re-raised rather than swallowed as a connection error, so it cannot be retried
into submission.

**Status: BLOCKED on credentials and a terminal, not on code.** No test can
close it.

### 3. Shadow has never run against a live signal stream

Shadow's distinguishing purpose — running *alongside* live to compare decisions
without touching the venue — cannot be exercised, because there is no live
adapter to run alongside. What is verified is that shadow makes the same
decisions as paper and executes nothing.

**Status: OPEN, and dependent on (2).**
