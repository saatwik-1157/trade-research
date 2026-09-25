# CERTIFICATION_IMPACT_ANALYSIS.md

What a change costs the certification. L63 §10 and §11, 2026-09-06.
Source: `backend/app/safety/impact.py`.

---

## This is the part of L63 with something real to work on

Every other section of the brief describes watching a control loop that is not
running. This reads **a set of changed files** — which the repository produces
on every commit — and answers whether the certification still stands.

It works because L62 left a map behind: each invariant names the module that
enforces it, and each gate names its invariants. A changed file resolves to
invariants, to gates, to a verdict. **No new bookkeeping was needed.**

## Verdicts

`NONE` · `TARGETED_REVALIDATION` · `FULL_REVALIDATION` · `SUSPEND_AUTONOMY`

## Rules, worst first

1. **A critical path suspends autonomy.** `app/risk/engine.py`,
   `app/oms/service.py`, `app/oms/state.py`, `app/brokers/base.py`,
   `app/execution/pipeline.py`, and `app/safety/` itself. A diff here means the
   platform is running code that has not been certified, and the response is to
   stop acting rather than to reason about whether the change looked safe.
2. **An unrecognised path is FULL_REVALIDATION, never NONE.** A path nobody
   classified is a path nobody thought about. This is the branch it would be
   most tempting to soften the first time it flags something dull.
3. **A CRITICAL invariant affected** → full rather than targeted.
4. Otherwise → targeted.

Exempt: documentation, `frontend/`, `reports/`, `tools/`, and tests. Tests are
the evidence; a changed test is caught by running it, which revalidation does.

**The exempt list is deliberately short.** A generous one is how a change to a
"harmless" module stops being reviewed.

## Triggers

| Trigger | Impact |
|---|---|
| `POLICY_CHANGED` | FULL_REVALIDATION |
| `RISK_RULE_CHANGED` | SUSPEND_AUTONOMY |
| `OMS_CHANGED` | SUSPEND_AUTONOMY |
| `BROKER_ADAPTER_CHANGED` | SUSPEND_AUTONOMY |
| `MT5_CONFIGURATION_CHANGED` | FULL_REVALIDATION |
| `MODEL_CHANGED` | TARGETED_REVALIDATION |
| `STRATEGY_CHANGED` | TARGETED_REVALIDATION |
| `PORTFOLIO_POLICY_CHANGED` | FULL_REVALIDATION |
| `SECURITY_CHANGE` | FULL_REVALIDATION |
| `DATABASE_SCHEMA_CHANGE` | FULL_REVALIDATION |
| `EXECUTION_BEHAVIOR_DEGRADATION` | SUSPEND_AUTONOMY |
| `CONTROL_LOOP_ANOMALY` | SUSPEND_AUTONOMY |
| `CRITICAL_INVARIANT_FAILURE` | SUSPEND_AUTONOMY |
| `CERTIFICATION_EXPIRY` | FULL_REVALIDATION |
| `DOCUMENTATION_CHANGED` | NONE |
| `FRONTEND_CHANGED` | NONE |

`trigger_impact` returns `FULL_REVALIDATION` for anything unmapped, so the
fail-closed default is not something each caller has to remember.

## It judges itself

`app/safety/` is a critical path, so **changing the analyser suspends
autonomy**. Run over this session's own L62/L63 changes it returns
`SUSPEND_AUTONOMY`, which is the correct answer. A governance tool exempt from
its own rule is the first place a hole appears.
