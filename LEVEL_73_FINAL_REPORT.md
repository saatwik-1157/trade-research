# Level 73 — final report

Controlled production scaling. Measured 2026-09-07 against the running
deployment.

## Final status

    SCALING_NOT_READY

## Why, in one line

Level 73 scales a live production system. This platform has never activated one,
so there is nothing to scale — and its own §2 precondition and §37 change freeze
both stop the level before any change is made.

## The required sections

| Section | Value | Source |
|---|---|---|
| **Current live state** | none. `trading_mode=paper`, `live_trading=false`, `live_execution_allowed=false`, 12 blockers | `/health` |
| **Active accounts** | 0 broker accounts (7 paper) | `broker_accounts` |
| **Active brokers** | 0 registered. `_ADAPTERS` is now `{"simulator": "paper", "mt5_demo": "demo"}` — registrable, not registered | `app/api/v1/brokers.py` |
| **Active MT5 terminals** | 0 registered. The terminal is reachable and now registrable as `mt5_demo`; 1 in use by `tools/`, on a **demo** account | `MT5Adapter` registrable, not registered |
| **Active strategies** | 0 live (2 strategies, 3 versions, all paper) | `strategies` |
| **Active bots** | 0 live (7 paper) | `bots` |
| **Capital utilisation** | not applicable — no live capital allocated | — |
| **Risk utilisation** | not applicable. All eight capital limits are **unset**, which the live gate reports as a blocker | preflight `capital_limits_configured: FAIL` |
| **Margin utilisation** | not applicable to the platform. The demo account outside it: free margin 99,695.27 of 100,012.00 equity | `tools/mt5_account.py` |
| **Portfolio exposure** | 0 live. 1 open paper position | `positions` |
| **Correlation** | not computable for a live portfolio that does not exist. Market data covers 2 symbols / 705 bars, which is below what correlation work needs | `market_bars` |
| **Execution capacity** | 4 workers running, database 79ms, Redis 7ms, API healthy | `/health/ready` |
| **Operational capacity** | stack up: tr-api, tr-postgres, tr-redis, tr-frontend, tr-nginx | `docker ps` |
| **Strategy health** | no live strategy to score | — |
| **Broker health** | no broker registered | `BrokerRegistry` empty |
| **MT5 health** | terminal answers; account 5055473926 @ MetaQuotes-Demo, **DEMO**, trading permitted | preflight `terminal_reachable: PASS` |
| **Market data health** | 705 bars across 2 symbols; no live feed. `MARKET_DATA_STALE_SECONDS=900` | `market_bars` |
| **Monitoring health** | `MONITORING_ENABLED=true`, worker running | `/health/ready` |
| **Recovery health** | `RECOVERY_STARTUP_CHECKS=true`, safe mode clear | settings, `/health` |
| **Certification** | `CONDITIONALLY_CERTIFIED` — GATE-01 and GATE-10 not passing. 31 invariants: 29 ENFORCED, 2 NOT_APPLICABLE, 0 UNENFORCED | `project_state.py` |
| **Scaling changes** | none made | — |
| **Rollbacks** | none needed | — |
| **Incidents** | none | — |

## Warnings

1. **`CONDITIONALLY_CERTIFIED` with two gates not passing** (GATE-01, GATE-10).
   §55 requires the L62–L69 controls to remain active through scaling; two of
   them are not currently passing, which is a scaling blocker in its own right.
2. **Market data is thin.** 705 bars across 2 symbols. Correlation, fragility
   and cross-instrument stress all need a common window across two or more
   instruments — this is at the very edge of that, and every §13 correlation
   control would be computing on almost nothing.
3. **The demo harness runs outside every platform control.** `tools/` is trading
   the demo account right now with no platform kill switch, no audit trail and
   no visibility to `PositionManager`. Bounded today because it is demo money
   behind a code fence, but it is the one execution path that scaling governance
   would not reach.

## Blockers

| # | Blocker | Why it stops this level |
|---|---|---|
| 1 | No live account | nothing to scale |
| 2 | No live broker | `_ADAPTERS` has a demo MT5 entry and no live one; live credentials are a separate seat |
| 3 | No live strategy or bot | §7's inventory is empty; §9's "one change → observe" has no first change |
| 4 | **No approved live capital envelope** | §5 requires the scaling policy to be bounded by the approved risk policy. All eight capital limits are unset |
| 5 | 12 live execution blockers | §2 precondition |
| 6 | Certification `CONDITIONALLY_CERTIFIED` | §30 requires certification to pass; §55 requires it to stay passing |
| 7 | No prior live session to attribute against | §47's attribution and §48's experiment log have no baseline |

## What was not built, and why

No `ProductionScalingPolicy`, no `ScalingProposal`, no scaling dry-run, no
shadow-scaling harness, no `PRODUCTION_SCALING_POLICY.md`,
`PRODUCTION_SCALING_LOG.md` or `PRODUCTION_CAPACITY_REPORT.md`.

§62 says implement only *verified missing* controls, and §61 forbids weakening
anything. A scaling layer written now would govern zero strategies inside a
budget nobody has approved, and its tests would assert shapes rather than
behaviour. A scaling log with no experiments and a capacity report for a live
system that has never run are documents that would go stale the moment either
became real — and worse, they would read to the next level as evidence.

The machinery §12 tells me to reuse — `PortfolioRiskOrchestrator`,
`DynamicRiskBudgetAllocator`, `PortfolioCapitalOptimizer`,
`StrategyHealthEvaluator`, `PortfolioDecisionEngine`, `PortfolioControlEngine` —
**already exists** (L53–L61, `app/portfolio/`, 11 modules) and is already
bounded by the RiskEngine. What is missing is not code. It is a live system.

## What §53 and §54 asked for, and what the audit found

Both are already satisfied, and this was verified rather than assumed:

- **One authoritative risk engine.** `app/risk/engine.py`. `Approval` has no
  other constructor; `OrderManager.submit` takes one positionally. Every other
  risk-shaped calculation in the repository produces recommendations, analytics
  or stress results and cannot mint an approval.
- **Three code paths reach MT5**, two of them read-only. The third is the
  standalone `tools/` harness, documented in `ORDER_EXECUTION_FLOW.md` rather
  than removed — it is the only code here that has ever traded, and it is fenced
  to demo accounts in code.

## Recommended next step

Not Level 74, and not a scaling layer. The step with the largest real gain and
no live risk:

**Done as of 2026-09-07:** `_ADAPTERS["mt5_demo"]` now maps to the existing
`MT5Adapter`, behind step-up, the mode fence and `assert_demo`, verified against
the live terminal. What remains is the half that produces evidence: **run the
harness's strategy through the platform against that demo venue and compare the
two ledgers.**

That is what turns 318 modules of tested-in-isolation execution machinery into
a pipeline with evidence behind it — and it is the prerequisite for every level
above it.

    SCALING_NOT_READY
