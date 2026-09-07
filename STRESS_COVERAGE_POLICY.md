# STRESS_COVERAGE_POLICY.md

L67 sections 7 and 8. What counts as covered.

---

## The sixteen classes

| Stress class |
|---|
| `MARKET_STRESS` |
| `VOLATILITY_STRESS` |
| `CORRELATION_STRESS` |
| `LIQUIDITY_STRESS` |
| `SPREAD_STRESS` |
| `SLIPPAGE_STRESS` |
| `MARGIN_STRESS` |
| `DRAWDOWN_STRESS` |
| `REGIME_STRESS` |
| `STRATEGY_STRESS` |
| `MODEL_STRESS` |
| `EXECUTION_STRESS` |
| `BROKER_STRESS` |
| `INFRASTRUCTURE_STRESS` |
| `COMBINED_STRESS` |
| `CASCADING_STRESS` |

`matrix()` reports **every one of them, always**. A matrix listing only what
was tested makes the untested invisible, which is the opposite of its job.

## Coverage is per account

Section 26. A stress result for account A says nothing about account B, and
`coverage_of` filters by account before anything else.

## Results go stale

Default TTL 7 days. A stress result is a statement about the portfolio that was
tested, and portfolios change.

A configuration decision, not a measurement -- no stress has ever been run here
-- and it **requires approval**.

## Gaps are named

`Gap` carries the class, the account, the severity, the evidence, a recommended
scenario and a priority. A gap without a recommended scenario is an observation
nobody can act on.

## What is not built

Automatic gap *detection* across combined dimensions -- "no scenario covers
high volatility + low liquidity". The combination machinery exists and is
bounded; what is missing is the record of which combinations were run, because
none have been.
