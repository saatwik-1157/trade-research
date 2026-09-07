# Production scaling audit

Level 73, Phase 1. Measured 2026-09-07 from the running deployment via
`tools/project_state.py --write` and `/health`.

## Precondition: not met

Level 73 §2 requires the live system to be `STABLE` or `STABLE_WITH_WARNINGS`
before scaling. §37 adds a change freeze while it is degraded or in safe mode.

**There is no live system.** Not a degraded one, not one in safe mode — none has
ever been activated. So the precondition is not merely unmet; the thing it asks
about does not exist.

The documents §1 asks me to read do not exist either, and cannot honestly be
written: `LEVEL_72_FINAL_REPORT.md`, `POST_LIVE_SYSTEM_AUDIT.md`,
`LIVE_PRODUCTION_STABILITY_REPORT.md`, `LIVE_PRODUCTION_SAFETY_SCORECARD.md`,
`LIVE_OPERATIONAL_RUNBOOK.md`, `LIVE_CHANGE_CONTROL.md`,
`FIRST_LIVE_SESSION_REPORT.md`. Each describes a live production system that has
not run. Writing them would be fabricating the evidence the next level is meant
to check.

**Status: SCALING_NOT_READY.**

## The measured inventory

Everything below is a query result, not an estimate.

| Dimension | Live | Paper / other | Source |
|---|---|---|---|
| Accounts | **0** | 7 paper | `select count(*) from broker_accounts` / `paper_accounts` |
| Brokers | **0** registered | — | `BrokerRegistry` empty at startup by design |
| MT5 terminals | 0 registered; **now registrable** as `mt5_demo` on a Windows host in demo mode | 1 reachable from `tools/` | `_ADAPTERS` gained an MT5 demo entry |
| Bots | **0** live | 7 paper | `select count(*) from bots` |
| Strategies | **0** live | 2, with 3 versions | `strategies`, `strategy_versions` |
| AI models | **0** live | 1 version, 3 training runs | `model_versions`, `training_runs` |
| Symbols | **0** live | 2 with bars (705 bars) | `market_bars` |
| Orders | **0** live | 271 paper | `orders` |
| Trades | **0** live | 252 paper | `trades` |
| Open positions | **0** live | 1 paper | `positions where status='open'` |
| Signals | — | 121 | `signals` |
| Capital reservations | 0 | 0 | `capital_reservations` |
| Workers | 4 running | — | `/health/ready` |
| Trading mode | `paper` | | `/health` |
| Live blockers | **12** | | `/health` |
| Safety invariants | 31 total: 29 ENFORCED, 2 NOT_APPLICABLE, 0 UNENFORCED | | `project_state.py` |
| Certification | `CONDITIONALLY_CERTIFIED`, GATE-01 and GATE-10 not passing | | `project_state.py` |

Separately, outside the platform: one MT5 **demo** account (5055473926 @
MetaQuotes-Demo) with 7 open positions, being traded right now by
`tools/run_overnight.py`. That process is not a live production deployment and
is not governed by anything in this level.

## Classification

| Component | Class | Reason |
|---|---|---|
| `PortfolioRiskOrchestrator`, `DynamicRiskBudgetAllocator`, `PortfolioCapitalOptimizer` (`app/portfolio/`, 11 modules) | **KEEP** | built L53–L56, untouched |
| `StrategyHealthEvaluator`, strategy lifecycle (`app/strategies/lifecycle.py`) | **KEEP** | L57 |
| `PortfolioDecisionEngine`, `PortfolioControlEngine` | **KEEP** | L60–L61 |
| Policy verification, certification, assurance (`app/safety/`, 9 modules) | **KEEP** | L62–L64 |
| Multi-horizon coordination, scenario intelligence, stress orchestration | **KEEP** | L65–L67 |
| Resilience, recovery, continuity (`app/recovery/`) | **KEEP** | L68–L69 |
| `ShadowBroker` (`app/brokers/shadow.py`) | **KEEP** | the canary/shadow mechanism §10 and §33 ask for already exists |
| Capital reservations (migration 0027) | **KEEP** | §15's requirement, already built and tested |
| `RiskEngine` | **KEEP, DO NOT TOUCH** | the single authoritative veto §53 requires |
| Live activation layer (`app/live/`) | **KEEP** | added at L70 this session |
| `ProductionScalingPolicy` | **NOT ADDED** | see below |
| Scaling proposal / dry run / champion-challenger wiring | **NOT ADDED** | see below |

**Nothing is classified REMOVE.** No duplicate risk engine and no unauthorised
execution path was found — §53 and §54 are already satisfied, and the full path
audit is in `ORDER_EXECUTION_FLOW.md`.

## Why the scaling controls were not built

§62 says "implement only verified missing scaling controls" and §61 forbids
weakening anything. A scaling layer built now would be:

- **Untestable against its own purpose.** Every test would exercise it against
  zero live strategies and zero live capital. "Scaling beyond approved capital
  is rejected" is a real test; "scaling from 0 strategies to 1 within a $0
  approved budget" is a shape, not a check.
- **Governing an envelope nobody has set.** §5 requires the policy to be bounded
  by the existing approved risk policy. There is no approved live risk policy —
  the eight capital limits the live gate demands are all unset, which the
  preflight reports as `capital_limits_configured: FAIL`.
- **Premature by its own §9.** "One change → observe → validate → stabilise →
  next change" cannot start from zero changes observed.

The honest position: the machinery §12 tells me to reuse (portfolio
orchestration, budget allocation, health evaluation, decision and control
engines) **already exists and is already bounded by the RiskEngine**. What is
missing is not code. It is a live system to govern.

## §53 — duplicate risk engines

Audited. There is one authoritative veto: `app/risk/engine.py`. `Approval` is
constructible only by `RiskEngine.approve`, and `OrderManager.submit` takes one
positionally, so an unapproved order is not expressible.

The other modules that compute risk-shaped numbers — `app/portfolio/`
(allocation), `app/ai/` (scores), `app/backtest/` and `app/research/`
(forecasts), stress and scenario packages — produce recommendations, analytics
and stress results. None can mint an approval, and a test asserts it.

## §54 — duplicate execution paths

Three code paths reach MetaTrader 5. Two are read-only. The third is
`tools/mt5_paper.place()`, reached from the standalone harness — a genuine
second execution path, documented rather than removed, because it is the only
code in this repository that has ever traded and it is fenced to demo in code.
Full table in `ORDER_EXECUTION_FLOW.md`.

The UI, TradingView gateway, AI package, research package, portfolio optimiser
and recovery manager reach none of them. Each is asserted by a test that parses
the package for forbidden imports.

## Recommended sequence

Level 73 becomes meaningful after, not before, these:

1. ~~Give the platform a **demo venue**~~ (`_ADAPTERS["mt5_demo"]`).
   **DONE 2026-09-07**, verified against the live terminal. Windows host only.
2. Run the existing strategy through the platform against that demo venue.
   Compare the two ledgers. This is what proves the pipeline end to end.
3. Set the eight capital limits, even for demo. The gate already demands them.
4. Flip the nine built `LIVE_GATES` in one reviewed pass, evidence per gate.
5. Then: credentials, live adapter, MFA, a real account — each its own level.
6. **Then** L73, with something real to scale and observations to attribute.

## Status

    SCALING_NOT_READY

    Blockers: no live account, no live broker, no live strategy, no live bot,
              no approved live capital envelope, 12 live execution blockers,
              certification CONDITIONALLY_CERTIFIED with GATE-01 and GATE-10
              not passing.
