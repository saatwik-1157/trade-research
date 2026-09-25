# CASCADE_FAILURE_POLICY.md

L67 section 11. Causal claims and what supports them.

---

## Do not assume causality from correlation

Section 11 says it and this platform is in an unusually good position to obey
it, because it has **no correlation data at all**.
`app/portfolio/exposure.py` has reported correlation as unavailable since L53
and still does: correlation needs a common window across two or more
instruments, and this deployment has one.

Every edge is therefore labelled, and the labels are honest:

| From | To | Evidence | Note |
|---|---|---|---|
| `market_volatility` | `spread` | **HYPOTHETICAL** | widely held; not measured on this broker's recorded spreads |
| `spread` | `slippage` | **HYPOTHETICAL** | mechanism, not measurement |
| `slippage` | `execution_quality` | **HYPOTHETICAL** | - |
| `execution_quality` | `strategy_performance` | **HYPOTHETICAL** | - |
| `market_volatility` | `margin` | **HYPOTHETICAL** | - |
| `margin` | `portfolio_drawdown` | **HYPOTHETICAL** | - |
| `strategy_performance` | `portfolio_drawdown` | **HYPOTHETICAL** | - |
| `portfolio_drawdown` | `risk_restriction` | **OBSERVED** | this one IS observed: the daily-loss limit halts new entries, and tests/test_risk.py exercises it |

**Exactly one edge is OBSERVED.** Drawdown to risk restriction -- the daily
loss limit really does halt new entries, and `tests/test_risk.py` exercises it.

Everything else is `HYPOTHETICAL`: a mechanism from a textbook. `INFERRED`
would require the correlation analysis that does not exist, and **not one edge
claims it**.

## Why the labels matter more than the graph

A cascade graph is the most quotable artefact a stress system produces. Drawn
from plausible mechanisms and labelled `OBSERVED`, it would be the most
convincing false claim in the repository -- convincing precisely because every
edge is individually reasonable.

`test_almost_every_cascade_edge_is_hypothetical` asserts the one observed edge
and that no edge claims INFERRED.

## What is not built

Bottleneck detection, feedback-loop identification and critical-dependency
ranking over a real graph. Those need edges with weights, and a weight is a
measurement.
