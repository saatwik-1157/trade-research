# Documentation

Design, policy, runbook and audit documents for trade-research, grouped by topic.
Levels (L18, L62, ...) refer to the build level that produced the document.

Some documents stay at the repository root because code, configuration or
`CLAUDE.md` names them by path; they are listed at the end.

## Topics

- [Architecture](#architecture) (`architecture/`, 15) - Target platform design, execution flow, environments, and the TradingView strategy pipeline.
- [AI and models](#ai) (`ai/`, 5) - Training, validation, registry, monitoring and strategy integration of AI models.
- [Autonomy](#autonomy) (`autonomy/`, 10) - What the platform may do without a person, and how that is bounded, downgraded and restored.
- [Assurance and certification](#assurance) (`assurance/`, 12) - Safety invariants, certification, evidence, replay and continuous assurance.
- [Policy research and governance](#policy) (`policy/`, 15) - Researching and changing governance policy under controls (L63-L64).
- [Portfolio, risk and stress](#risk) (`risk/`, 36) - Portfolio and risk budgets, multi-horizon decisions, scenarios and stress orchestration (L30, L53-L67).
- [Broker and live trading](#broker) (`broker/`, 14) - MT5 venue integration, demo venue runs, and the live-trading path and its gates.
- [Operations](#operations) (`operations/`, 7) - Running, demoing, releasing, rolling back and going live.
- [Testing](#testing) (`testing/`, 7) - Test plans, environments, scenarios and level test reports.
- [History, audits and reports](#history) (`history/`, 26) - Per-level build records, audits, final reports and the project progress log.

<a id="architecture"></a>

## Architecture

Target platform design, execution flow, environments, and the TradingView strategy pipeline.

| Document | Summary |
|---|---|
| [`ARCHITECTURE.md`](architecture/ARCHITECTURE.md) | Master context: the target platform, invariants at every level, and how existing code maps onto it. |
| [`DECISIONS.md`](architecture/DECISIONS.md) | Engineering decision log, one entry per decision that changes what code exists; newest first. |
| [`DECISION_HIERARCHY.md`](architecture/DECISION_HIERARCHY.md) | Which decision layer wins and why it cannot be overridden (L60). |
| [`ORDER_EXECUTION_FLOW.md`](architecture/ORDER_EXECUTION_FLOW.md) | Every path from an intention to a venue, and the refusals along each one. |
| [`EXECUTION_CONSISTENCY.md`](architecture/EXECUTION_CONSISTENCY.md) | Whether backtest, replay, paper, shadow and demo apply the same rules, and where they differ. |
| [`ENVIRONMENT_CONFIGURATION.md`](architecture/ENVIRONMENT_CONFIGURATION.md) | The five environments, what separates them, and which settings decide whether money can move. |
| [`RECOVERY_ARCHITECTURE.md`](architecture/RECOVERY_ARCHITECTURE.md) | Recovery, resilience and reconciliation design (L38). |
| [`TRADING_FAILURE_RECOVERY.md`](architecture/TRADING_FAILURE_RECOVERY.md) | What breaks, what the platform does about it, and what it deliberately does not do. |
| [`TRADE_JOURNAL_ARCHITECTURE.md`](architecture/TRADE_JOURNAL_ARCHITECTURE.md) | Trade journal and trade lifecycle history (L31). |
| [`STRATEGY_SPECIFICATION.md`](architecture/STRATEGY_SPECIFICATION.md) | Canonical internal form of a TradingView strategy and the Pine subset that can reach it. |
| [`STRATEGY_COMPILER.md`](architecture/STRATEGY_COMPILER.md) | TradingViewSpec to StrategyDefinition compilation, and what the compiler refuses. |
| [`STRATEGY_VALIDATION.md`](architecture/STRATEGY_VALIDATION.md) | Gates a compiled strategy passes before anything runs it, and what VALIDATED does not mean. |
| [`STRATEGY_STATE_MACHINE.md`](architecture/STRATEGY_STATE_MACHINE.md) | Allowed transitions between strategy-version statuses (L57). |
| [`STRATEGY_QUARANTINE_POLICY.md`](architecture/STRATEGY_QUARANTINE_POLICY.md) | How a strategy is switched off (L56). |
| [`TRADINGVIEW_DISCOVERY.md`](architecture/TRADINGVIEW_DISCOVERY.md) | What automatic strategy discovery can legitimately mean and what each source yields. |

<a id="ai"></a>

## AI and models

Training, validation, registry, monitoring and strategy integration of AI models.

| Document | Summary |
|---|---|
| [`AI_TRAINING_ARCHITECTURE.md`](ai/AI_TRAINING_ARCHITECTURE.md) | Training architecture: job lifecycle, data flow, versioning, artifacts and where promotion stops (L25). |
| [`AI_VALIDATION_ARCHITECTURE.md`](ai/AI_VALIDATION_ARCHITECTURE.md) | The deliberately narrow question the AI validation engine answers (L26). |
| [`AI_STRATEGY_INTEGRATION.md`](ai/AI_STRATEGY_INTEGRATION.md) | How the AI layer occupies the execution seat reserved for it (L27). |
| [`MODEL_REGISTRY_ARCHITECTURE.md`](ai/MODEL_REGISTRY_ARCHITECTURE.md) | Model registry and model lifecycle (L28). |
| [`AI_MODEL_MONITORING.md`](ai/AI_MODEL_MONITORING.md) | Model monitoring and drift detection (L29). |

<a id="autonomy"></a>

## Autonomy

What the platform may do without a person, and how that is bounded, downgraded and restored.

| Document | Summary |
|---|---|
| [`AUTONOMY_POLICY.md`](autonomy/AUTONOMY_POLICY.md) | What the platform may do without a person (L51). |
| [`AUTONOMY_LEVEL_POLICY.md`](autonomy/AUTONOMY_LEVEL_POLICY.md) | Autonomy levels: what the platform may do unattended (L63 section 15). |
| [`AUTONOMY_DOWNGRADE_POLICY.md`](autonomy/AUTONOMY_DOWNGRADE_POLICY.md) | When and how autonomy is downgraded (L63 section 16). |
| [`CONTROLLED_RESTORATION_POLICY.md`](autonomy/CONTROLLED_RESTORATION_POLICY.md) | Getting autonomy back after a downgrade (L63 sections 17-18). |
| [`AUTONOMOUS_ACTION_POLICY.md`](autonomy/AUTONOMOUS_ACTION_POLICY.md) | What the platform may do to itself without asking (L61). |
| [`AUTONOMOUS_INCIDENT_RESPONSE.md`](autonomy/AUTONOMOUS_INCIDENT_RESPONSE.md) | What happens automatically when something breaks, per incident (L51). |
| [`AUTONOMOUS_CONTROL_SECURITY.md`](autonomy/AUTONOMOUS_CONTROL_SECURITY.md) | Security of the autonomous control surface (L62 sections 21-22). |
| [`AUTONOMOUS_CONTROL_VERIFICATION.md`](autonomy/AUTONOMOUS_CONTROL_VERIFICATION.md) | What was audited and verified at L62, and what could not be. |
| [`CONTROL_LOOP_SAFETY.md`](autonomy/CONTROL_LOOP_SAFETY.md) | Runaway control-loop protection (L62 section 12). |
| [`AUTONOMY_TEST_MATRIX.md`](autonomy/AUTONOMY_TEST_MATRIX.md) | Autonomy test matrix; every PASS names the test or observation behind it (L51). |

<a id="assurance"></a>

## Assurance and certification

Safety invariants, certification, evidence, replay and continuous assurance.

| Document | Summary |
|---|---|
| [`SYSTEM_CERTIFICATION.md`](assurance/SYSTEM_CERTIFICATION.md) | System certification at L42; nothing marked PASS without named evidence. |
| [`POLICY_INVARIANTS.md`](assurance/POLICY_INVARIANTS.md) | The twenty-five safety invariants (L62). |
| [`SAFETY_SCORECARD.md`](assurance/SAFETY_SCORECARD.md) | Safety scorecard generated from app/safety/ (L62 section 16). |
| [`ASSURANCE_EVIDENCE_POLICY.md`](assurance/ASSURANCE_EVIDENCE_POLICY.md) | What counts as assurance evidence (L63). |
| [`ASSURANCE_REPLAY_POLICY.md`](assurance/ASSURANCE_REPLAY_POLICY.md) | Reconstructing a past certification (L63 sections 25-26). |
| [`CERTIFICATION_IMPACT_ANALYSIS.md`](assurance/CERTIFICATION_IMPACT_ANALYSIS.md) | What a change costs the certification (L63 sections 10-11). |
| [`CERTIFICATION_VIOLATION_POLICY.md`](assurance/CERTIFICATION_VIOLATION_POLICY.md) | Certification violations and what they cost (L63 sections 14, 16). |
| [`CONTINUOUS_DRIFT_ASSURANCE.md`](assurance/CONTINUOUS_DRIFT_ASSURANCE.md) | Drift under continuous assurance (L63 section 12). |
| [`DECISION_REPLAY_POLICY.md`](assurance/DECISION_REPLAY_POLICY.md) | Decision replay and reproducibility (L62 section 9). |
| [`COUNTERFACTUAL_TESTING.md`](assurance/COUNTERFACTUAL_TESTING.md) | Counterfactual and fault-injection testing (L62 sections 10-11). |
| [`POLICY_CONFLICT_RESOLUTION.md`](assurance/POLICY_CONFLICT_RESOLUTION.md) | How conflicting safety policies resolve (L62 section 7). |
| [`POLICY_REGRESSION_POLICY.md`](assurance/POLICY_REGRESSION_POLICY.md) | Policy regression and shadow comparison (L62 sections 14-15). |

<a id="policy"></a>

## Policy research and governance

Researching and changing governance policy under controls (L63-L64).

| Document | Summary |
|---|---|
| [`POLICY_RESEARCH_ARCHITECTURE.md`](policy/POLICY_RESEARCH_ARCHITECTURE.md) | Researching governance policy without being able to change it (L64). |
| [`POLICY_CHANGE_GOVERNANCE.md`](policy/POLICY_CHANGE_GOVERNANCE.md) | How a policy changes (L63 sections 22-24). |
| [`POLICY_GOVERNANCE_WORKFLOW.md`](policy/POLICY_GOVERNANCE_WORKFLOW.md) | Review workflow and the AI boundary (L64 sections 28, 32). |
| [`POLICY_HYPOTHESIS_POLICY.md`](policy/POLICY_HYPOTHESIS_POLICY.md) | What a policy research proposal must contain (L64 section 7). |
| [`POLICY_VALIDATION_POLICY.md`](policy/POLICY_VALIDATION_POLICY.md) | The policy validation pipeline (L64 sections 10-16). |
| [`POLICY_SELECTION_POLICY.md`](policy/POLICY_SELECTION_POLICY.md) | Choosing between baseline and challenger policies (L64 sections 18, 20). |
| [`POLICY_SHADOW_POLICY.md`](policy/POLICY_SHADOW_POLICY.md) | Shadow evaluation of policies (L64 section 17). |
| [`POLICY_COMPLEXITY_POLICY.md`](policy/POLICY_COMPLEXITY_POLICY.md) | Preferring simple policies (L64 section 19). |
| [`POLICY_RESEARCH_MEMORY.md`](policy/POLICY_RESEARCH_MEMORY.md) | Learning from research history (L64 section 24). |
| [`POLICY_MULTIPLE_TESTING_POLICY.md`](policy/POLICY_MULTIPLE_TESTING_POLICY.md) | Preventing manufactured confidence from multiple testing (L64 section 25). |
| [`POLICY_OVERFITTING_POLICY.md`](policy/POLICY_OVERFITTING_POLICY.md) | Multiple testing and overfitting controls (L64 sections 25-26). |
| [`POLICY_ROBUSTNESS_POLICY.md`](policy/POLICY_ROBUSTNESS_POLICY.md) | Perturbation testing of policies (L64 section 27). |
| [`POLICY_DEPLOYMENT_POLICY.md`](policy/POLICY_DEPLOYMENT_POLICY.md) | Controlled policy deployment (L64 section 29). |
| [`POLICY_ROLLBACK_POLICY.md`](policy/POLICY_ROLLBACK_POLICY.md) | Automatic policy rollback (L64 section 30). |
| [`POLICY_SANDBOX_SECURITY.md`](policy/POLICY_SANDBOX_SECURITY.md) | Why there is no policy sandbox (L64 sections 9, 33). |

<a id="risk"></a>

## Portfolio, risk and stress

Portfolio and risk budgets, multi-horizon decisions, scenarios and stress orchestration (L30, L53-L67).

| Document | Summary |
|---|---|
| [`PORTFOLIO_ARCHITECTURE.md`](risk/PORTFOLIO_ARCHITECTURE.md) | Portfolio management and exposure (L30). |
| [`PORTFOLIO_INTELLIGENCE_ARCHITECTURE.md`](risk/PORTFOLIO_INTELLIGENCE_ARCHITECTURE.md) | Portfolio intelligence audit: what existed and was never wired to enforcement (L53). |
| [`PORTFOLIO_RISK_REPORT.md`](risk/PORTFOLIO_RISK_REPORT.md) | Portfolio risk measured against the running deployment (L53). |
| [`RISK_CONTROL_MATRIX.md`](risk/RISK_CONTROL_MATRIX.md) | Every risk control, where it lives, what it refuses, and what proves it. |
| [`RISK_BUDGET_HIERARCHY.md`](risk/RISK_BUDGET_HIERARCHY.md) | How a limit at one level restricts the level below (L54/L55). |
| [`DYNAMIC_RISK_BUDGETING.md`](risk/DYNAMIC_RISK_BUDGETING.md) | Dynamic risk budgeting: the constraint layer built, and why the optimizer was not (L55). |
| [`MULTI_HORIZON_ARCHITECTURE.md`](risk/MULTI_HORIZON_ARCHITECTURE.md) | Decisions that reason over different time scales (L65). |
| [`MULTI_HORIZON_DECISION_POLICY.md`](risk/MULTI_HORIZON_DECISION_POLICY.md) | The multi-horizon decision object (L65 sections 9-12). |
| [`MULTI_HORIZON_RISK_POLICY.md`](risk/MULTI_HORIZON_RISK_POLICY.md) | Risk across horizons (L65 sections 20-21). |
| [`MULTI_HORIZON_REPLAY_POLICY.md`](risk/MULTI_HORIZON_REPLAY_POLICY.md) | Time consistency and replay across horizons (L65 sections 14, 17). |
| [`MULTI_HORIZON_AI_POLICY.md`](risk/MULTI_HORIZON_AI_POLICY.md) | What AI contributes to a horizon (L65 section 33). |
| [`HORIZON_AUTHORITY_POLICY.md`](risk/HORIZON_AUTHORITY_POLICY.md) | Which horizon outranks which (L65 sections 7, 48). |
| [`HORIZON_CONFLICT_POLICY.md`](risk/HORIZON_CONFLICT_POLICY.md) | When horizons disagree (L65 section 8). |
| [`STRATEGIC_CONTROL_POLICY.md`](risk/STRATEGIC_CONTROL_POLICY.md) | The long horizon and strategic control (L65 sections 6, 19, 24). |
| [`DEFERRED_DECISION_POLICY.md`](risk/DEFERRED_DECISION_POLICY.md) | Recommendations that wait (L65 sections 28, 30). |
| [`DECISION_FRESHNESS_POLICY.md`](risk/DECISION_FRESHNESS_POLICY.md) | Decision freshness policy (L81 section 6). |
| [`PREDICTIVE_RISK_POLICY.md`](risk/PREDICTIVE_RISK_POLICY.md) | What a risk prediction is allowed to do (L66). |
| [`SCENARIO_VALIDATION_POLICY.md`](risk/SCENARIO_VALIDATION_POLICY.md) | What is checked before a scenario runs (L66 section 8). |
| [`SCENARIO_RISK_POLICY.md`](risk/SCENARIO_RISK_POLICY.md) | Risk under a scenario (L66). |
| [`SCENARIO_CONFIDENCE_POLICY.md`](risk/SCENARIO_CONFIDENCE_POLICY.md) | Scenario confidence and what unknown means (L66 section 13). |
| [`SCENARIO_CALIBRATION_POLICY.md`](risk/SCENARIO_CALIBRATION_POLICY.md) | Scenario prediction quality (L66 sections 34-36). |
| [`SCENARIO_REPLAY_POLICY.md`](risk/SCENARIO_REPLAY_POLICY.md) | Historical scenarios and leakage (L66). |
| [`SCENARIO_SECURITY.md`](risk/SCENARIO_SECURITY.md) | Scenario isolation (L66 section 44). |
| [`LIQUIDITY_STRESS_POLICY.md`](risk/LIQUIDITY_STRESS_POLICY.md) | Spread, slippage and depth stress (L66 section 25). |
| [`EXECUTION_STRESS_POLICY.md`](risk/EXECUTION_STRESS_POLICY.md) | Latency, rejection and disconnect stress (L66 section 26). |
| [`PORTFOLIO_FRAGILITY_POLICY.md`](risk/PORTFOLIO_FRAGILITY_POLICY.md) | Where the portfolio is brittle (L66 sections 22-24). |
| [`STRESS_ORCHESTRATION_ARCHITECTURE.md`](risk/STRESS_ORCHESTRATION_ARCHITECTURE.md) | Which stresses to run and what running them established (L67). |
| [`STRESS_PRIORITY_POLICY.md`](risk/STRESS_PRIORITY_POLICY.md) | What stress to run next (L67 sections 5-6). |
| [`COMBINED_SCENARIO_POLICY.md`](risk/COMBINED_SCENARIO_POLICY.md) | Combined scenarios, and when to stop (L67 sections 9, 10, 40). |
| [`CASCADE_FAILURE_POLICY.md`](risk/CASCADE_FAILURE_POLICY.md) | Cascade failures: causal claims and what supports them (L67 section 11). |
| [`PORTFOLIO_RESILIENCE_POLICY.md`](risk/PORTFOLIO_RESILIENCE_POLICY.md) | What stress established about robustness (L67 sections 12-14). |
| [`RESILIENCE_BOTTLENECK_POLICY.md`](risk/RESILIENCE_BOTTLENECK_POLICY.md) | Components that disproportionately reduce resilience (L67 sections 15-16). |
| [`RESILIENCE_OPTIMIZATION_POLICY.md`](risk/RESILIENCE_OPTIMIZATION_POLICY.md) | Recommending resilience improvements (L67 sections 17-19). |
| [`STRESS_CALIBRATION_POLICY.md`](risk/STRESS_CALIBRATION_POLICY.md) | Whether the stress was right (L67 sections 31-32). |
| [`STRESS_CERTIFICATION_POLICY.md`](risk/STRESS_CERTIFICATION_POLICY.md) | Stress coverage and certification (L67 section 33). |
| [`STRESS_RESEARCH_POLICY.md`](risk/STRESS_RESEARCH_POLICY.md) | Stress gaps become research (L67 sections 30, 34). |

<a id="broker"></a>

## Broker and live trading

MT5 venue integration, demo venue runs, and the live-trading path and its gates.

| Document | Summary |
|---|---|
| [`BROKER_MT5_INTEGRATION.md`](broker/BROKER_MT5_INTEGRATION.md) | Broker / MT5 integration and which connection paths exist. |
| [`BROKER_REGISTRATION.md`](broker/BROKER_REGISTRATION.md) | How an operator points the platform at a venue (L51). |
| [`EXECUTION_WORKER_ACTIVATION.md`](broker/EXECUTION_WORKER_ACTIVATION.md) | Starting the execution worker in paper: what changed and what it did. |
| [`FIRST_DEMO_VENUE_RUN.md`](broker/FIRST_DEMO_VENUE_RUN.md) | The first time the execution path reached a venue it did not also write. |
| [`DEMO_VENUE_LIFECYCLE.md`](broker/DEMO_VENUE_LIFECYCLE.md) | Opening, recording, reconciling and closing a position at a real demo venue. |
| [`SIGNAL_PATH_FIRST_RUN.md`](broker/SIGNAL_PATH_FIRST_RUN.md) | A TradingView alert carried through the gateway to a real venue, first run. |
| [`LEDGER_COMPARISON.md`](broker/LEDGER_COMPARISON.md) | Does the platform's record agree with the venue's? |
| [`PROJECT_TRADING_ACTIVATION_AUDIT.md`](broker/PROJECT_TRADING_ACTIVATION_AUDIT.md) | Trading activation audit, Phase 1 of L70. |
| [`LIVE_TRADING_ARCHITECTURE.md`](broker/LIVE_TRADING_ARCHITECTURE.md) | What was added for live trading at L70, and what it is not allowed to do. |
| [`LIVE_TRADING_SAFETY.md`](broker/LIVE_TRADING_SAFETY.md) | Live-trading invariants, where each is enforced, and which test proves it. |
| [`LIVE_TRADING_PREFLIGHT.md`](broker/LIVE_TRADING_PREFLIGHT.md) | The live-trading preflight command and what it checks. |
| [`LIVE_TRADING_READINESS_REPORT.md`](broker/LIVE_TRADING_READINESS_REPORT.md) | Live-trading readiness generated by running the code (L70). |
| [`LIVE_ACTIVATION_FINAL_REPORT.md`](broker/LIVE_ACTIVATION_FINAL_REPORT.md) | Live activation final report (L70-L71). |
| [`FIRST_LIVE_TRADE_RUNBOOK.md`](broker/FIRST_LIVE_TRADE_RUNBOOK.md) | The reasoning and procedure for a first live trade, and why it cannot start yet. |

<a id="operations"></a>

## Operations

Running, demoing, releasing, rolling back and going live.

| Document | Summary |
|---|---|
| [`COMMANDS.md`](operations/COMMANDS.md) | Standing commands for this project and the loop they belong to. |
| [`DEMO_GUIDE.md`](operations/DEMO_GUIDE.md) | See the whole execution flow work in seconds with no broker, database or credentials. |
| [`RELEASE_PROCESS.md`](operations/RELEASE_PROCESS.md) | How a commit becomes a running deployment. |
| [`ROLLBACK_RUNBOOK.md`](operations/ROLLBACK_RUNBOOK.md) | How to go back, and the cases where you cannot. |
| [`GO_LIVE_READINESS.md`](operations/GO_LIVE_READINESS.md) | Subsystem-by-subsystem readiness for controlled production operation (L44). |
| [`GO_LIVE_CHECKLIST.md`](operations/GO_LIVE_CHECKLIST.md) | Go-live checklist (L44). |
| [`PRODUCTION_PILOT_READINESS.md`](operations/PRODUCTION_PILOT_READINESS.md) | Production pilot eligibility gate (L45). |

<a id="testing"></a>

## Testing

Test plans, environments, scenarios and level test reports.

| Document | Summary |
|---|---|
| [`TEST_ENVIRONMENT.md`](testing/TEST_ENVIRONMENT.md) | What the tests run against, and what they cannot run against on this machine (L40). |
| [`INTEGRATION_TEST_PLAN.md`](testing/INTEGRATION_TEST_PLAN.md) | The L40 integration test plan: what existed, what was missing, what was added. |
| [`INTEGRATION_TEST_REPORT.md`](testing/INTEGRATION_TEST_REPORT.md) | Integration test report (L40). |
| [`E2E_TEST_SCENARIOS.md`](testing/E2E_TEST_SCENARIOS.md) | End-to-end scenarios exercised at L40 and the test that carries each. |
| [`LEVEL_62_TEST_REPORT.md`](testing/LEVEL_62_TEST_REPORT.md) | Test report: autonomous control verification, policy assurance, safety certification (L62). |
| [`LEVEL_63_TEST_REPORT.md`](testing/LEVEL_63_TEST_REPORT.md) | Test report: continuous assurance, certification lifecycle, autonomy governance (L63). |
| [`LEVEL_64_TEST_REPORT.md`](testing/LEVEL_64_TEST_REPORT.md) | Test report: adaptive policy research and controlled policy evolution (L64). |

<a id="history"></a>

## History, audits and reports

Per-level build records, audits, final reports and the project progress log.

| Document | Summary |
|---|---|
| [`PROJECT_PROGRESS.md`](history/PROJECT_PROGRESS.md) | Running narrative log of level work, newest entry last. |
| [`IMPLEMENTATION_PRIORITY.md`](history/IMPLEMENTATION_PRIORITY.md) | Work sorted by priority as of 2026-09-02, marked up as items landed. |
| [`LEVEL_18_POSITION_SIZING.md`](history/LEVEL_18_POSITION_SIZING.md) | Level 18 build record: position sizing engine. |
| [`LEVEL_19_OMS.md`](history/LEVEL_19_OMS.md) | Level 19 build record: order management system. |
| [`LEVEL_20_AUTOMATED_EXECUTION.md`](history/LEVEL_20_AUTOMATED_EXECUTION.md) | Level 20 build record: automated execution engine. |
| [`LEVEL_21_POSITION_MANAGEMENT.md`](history/LEVEL_21_POSITION_MANAGEMENT.md) | Level 21 build record: position management. |
| [`LEVEL_22_BOT_MANAGER.md`](history/LEVEL_22_BOT_MANAGER.md) | Level 22 build record: autonomous bot manager. |
| [`LEVEL_23_AI_DATA_PIPELINE.md`](history/LEVEL_23_AI_DATA_PIPELINE.md) | Level 23 build record: AI data pipeline. |
| [`LEVEL_24_AI_MODELS.md`](history/LEVEL_24_AI_MODELS.md) | Level 24 build record: AI models. |
| [`LEVEL_32_ANALYTICS.md`](history/LEVEL_32_ANALYTICS.md) | Level 32: analytics and performance analytics. |
| [`LEVEL_33_AI_TRADE_REVIEW.md`](history/LEVEL_33_AI_TRADE_REVIEW.md) | Level 33: AI trade review and post-trade intelligence. |
| [`LEVEL_34_NOTIFICATIONS.md`](history/LEVEL_34_NOTIFICATIONS.md) | Level 34: notifications and alerting. |
| [`LEVEL_35_DISCORD.md`](history/LEVEL_35_DISCORD.md) | Level 35: Discord webhook integration. |
| [`LEVEL_36_ADMIN.md`](history/LEVEL_36_ADMIN.md) | Level 36: admin panel and system control centre. |
| [`LEVEL_37_MONITORING.md`](history/LEVEL_37_MONITORING.md) | Level 37: monitoring, observability and system health. |
| [`FINAL_AUDIT_REPORT.md`](history/FINAL_AUDIT_REPORT.md) | Final audit of the automated trading platform (L42). |
| [`FINAL_FEATURE_MATRIX.md`](history/FINAL_FEATURE_MATRIX.md) | Every planned level, its status, evidence, and what is missing (L42). |
| [`FINAL_PRODUCTION_READINESS.md`](history/FINAL_PRODUCTION_READINESS.md) | Final production readiness assessment (L42). |
| [`FINAL_RISK_REGISTER.md`](history/FINAL_RISK_REGISTER.md) | Every unresolved issue at L42, unsoftened. |
| [`FINAL_SYSTEM_REPORT.md`](history/FINAL_SYSTEM_REPORT.md) | Measured system report, 2026-09-10 (amended 2026-09-11). |
| [`PRODUCTION_SCALING_AUDIT.md`](history/PRODUCTION_SCALING_AUDIT.md) | Production scaling audit, L73 Phase 1. |
| [`LEVEL_73_FINAL_REPORT.md`](history/LEVEL_73_FINAL_REPORT.md) | Level 73 final report: controlled production scaling. |
| [`LEVEL_74_RESEARCH_AUDIT.md`](history/LEVEL_74_RESEARCH_AUDIT.md) | Level 74 research audit: classify what exists before building. |
| [`LEVEL_75_RESEARCH_VERIFICATION_AUDIT.md`](history/LEVEL_75_RESEARCH_VERIFICATION_AUDIT.md) | Level 75 research verification audit. |
| [`LEVEL_76_DECISION_INTELLIGENCE_AUDIT.md`](history/LEVEL_76_DECISION_INTELLIGENCE_AUDIT.md) | Level 76 decision intelligence audit. |
| [`LEVEL_81_READINESS_AUDIT.md`](history/LEVEL_81_READINESS_AUDIT.md) | Level 81 execution readiness audit. |

## Other documents in docs/

| Document | Summary |
|---|---|
| [`ARCHITECTURE_AUDIT.md`](ARCHITECTURE_AUDIT.md) | Read-only architecture audit, 2026-09-10: the repository and venue as the source of truth. |
| [`HOW_THE_SYSTEM_WORKS.md`](HOW_THE_SYSTEM_WORKS.md) | Plain-language walkthrough of every flow, verified against the code. |
| [`NINE_PROMPTS.md`](NINE_PROMPTS.md) | Nine viral stock-research prompts, and what this repository does instead. |
| [`OPPORTUNITY_DISCOVERY.md`](OPPORTUNITY_DISCOVERY.md) | Opportunity discovery (L83), backend/app/research/opportunity.py. |
| [`PORTFOLIO_DECISION_VALIDATION.md`](PORTFOLIO_DECISION_VALIDATION.md) | Portfolio decision validation (L86), backend/app/portfolio/allocation.py. |

## Kept at the repository root

| Document | Summary |
|---|---|
| [`README.md`](../README.md) | Project overview and quick start. |
| [`CLAUDE.md`](../CLAUDE.md) | Working rules for this repository. |
| [`CHANGELOG.md`](../CHANGELOG.md) | Notable changes, newest first. |
| [`SECURITY.md`](../SECURITY.md) | Security posture and the controls the platform adds. |
| [`ARCHITECTURE_MIGRATION.md`](../ARCHITECTURE_MIGRATION.md) | Current vs target architecture and the decision for every component. |
| [`AUTONOMOUS_ACTION_SAFETY_ENVELOPE.md`](../AUTONOMOUS_ACTION_SAFETY_ENVELOPE.md) | The bounds an autonomous action must sit inside. |
| [`BACKUP_RESTORE.md`](../BACKUP_RESTORE.md) | Backup, restore and disaster recovery. |
| [`CERTIFICATION_LIFECYCLE.md`](../CERTIFICATION_LIFECYCLE.md) | Certification as something that can go stale. |
| [`CONTINUOUS_ASSURANCE_ARCHITECTURE.md`](../CONTINUOUS_ASSURANCE_ARCHITECTURE.md) | Continuous assurance architecture. |
| [`DECISION_EXPIRATION_POLICY.md`](../DECISION_EXPIRATION_POLICY.md) | Decision validity windows. |
| [`DEPLOYMENT.md`](../DEPLOYMENT.md) | How the system runs today and how the platform is deployed. |
| [`DEPLOYMENT_ARCHITECTURE.md`](../DEPLOYMENT_ARCHITECTURE.md) | How the platform is deployed, and the decisions behind it. |
| [`DEPLOYMENT_GUIDE.md`](../DEPLOYMENT_GUIDE.md) | How to deploy, with commands run against the real stack. |
| [`EMERGENCY_STABILITY_AUDIT.md`](../EMERGENCY_STABILITY_AUDIT.md) | Emergency stability audit, 2026-09-10. |
| [`FIRST_LIVE_TRADE_CHECKLIST.md`](../FIRST_LIVE_TRADE_CHECKLIST.md) | The one-page first-live-trade form. |
| [`INTERFACE.md`](../INTERFACE.md) | The look of the app, and which parts of it are measured. |
| [`KNOWN_TEST_LIMITATIONS.md`](../KNOWN_TEST_LIMITATIONS.md) | What the test suite does not prove. |
| [`LIVE_TRADING_RUNBOOK.md`](../LIVE_TRADING_RUNBOOK.md) | The live-trading operational sequence. |
| [`LOSS_ROOT_CAUSE_REPORT.md`](../LOSS_ROOT_CAUSE_REPORT.md) | Root cause of losses across 476 closed demo trades. |
| [`MIGRATION_STATUS.md`](../MIGRATION_STATUS.md) | Per-level status snapshot for levels 00-42. |
| [`NIGHTLY.md`](../NIGHTLY.md) | Operational checklist for the overnight harvest session. |
| [`POLICY_CANDIDATE_POLICY.md`](../POLICY_CANDIDATE_POLICY.md) | What a policy candidate is, and what makes one valid. |
| [`PORTFOLIO_SCENARIO_ARCHITECTURE.md`](../PORTFOLIO_SCENARIO_ARCHITECTURE.md) | Predictive scenario intelligence (L66). |
| [`PRODUCTION_OPERATIONS_RUNBOOK.md`](../PRODUCTION_OPERATIONS_RUNBOOK.md) | Day-to-day production operation. |
| [`PRODUCTION_READINESS.md`](../PRODUCTION_READINESS.md) | Production readiness assessed at L41. |
| [`PRODUCTION_RUNBOOK.md`](../PRODUCTION_RUNBOOK.md) | Operating the platform once it is running: alerts and responses. |
| [`PROJECT_AUDIT.md`](../PROJECT_AUDIT.md) | Full repository audit as of 2026-09-02. |
| [`PROJECT_STATUS.md`](../PROJECT_STATUS.md) | Current project status, generated 2026-09-18. |
| [`SAFETY_CERTIFICATION.md`](../SAFETY_CERTIFICATION.md) | The twelve gates and what the platform is certified for. |
| [`SCENARIO_DEFINITION_POLICY.md`](../SCENARIO_DEFINITION_POLICY.md) | What a scenario is. |
| [`SECOND_MACHINE.md`](../SECOND_MACHINE.md) | Running this on another laptop. |
| [`SIGNAL_ROUTING.md`](../SIGNAL_ROUTING.md) | How a TradingView alert becomes an order, and where it stops. |
| [`STRESS_COVERAGE_POLICY.md`](../STRESS_COVERAGE_POLICY.md) | What counts as stress coverage. |
| [`TESTING.md`](../TESTING.md) | What is tested, how to run it, and what each level must add. |
| [`TRADINGVIEW_ARCHITECTURE.md`](../TRADINGVIEW_ARCHITECTURE.md) | The TradingView to execution chain and its build ladder. |
