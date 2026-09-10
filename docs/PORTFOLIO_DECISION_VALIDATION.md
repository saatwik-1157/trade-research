# PORTFOLIO_DECISION_VALIDATION.md — L86

`backend/app/portfolio/allocation.py` · 24 tests in `backend/tests/test_allocation.py`

## The question

    GOOD COMPANY            -- the business is sound
    GOOD INVESTMENT         -- the price is wrong
    GOOD PORTFOLIO DECISION -- adding it beats holding what we have, or cash

`app.research.opportunity` (L83) answers the first two. This answers the third,
and is deliberately not allowed to see the first two as a shortcut to it.

## The audit: what already existed

Of L86 §2's twenty named engines, **two exist by name** (`RiskEngine`,
`PositionSizer`). The rest do not. But most of the *capability* does, under
different module names, and this module reuses it rather than rebuilding:

| §2 asks for | Already exists as | Lines |
|---|---|---|
| `PortfolioScenarioEngine` | `app/portfolio/scenario.py` — L66, a forecast may tighten and never loosen | 594 |
| `PortfolioFragilityEngine`, `CorrelationStressEngine` | `app/portfolio/stress.py` — L67, coverage is earned never assumed | 595 |
| `InvestmentDecisionIntelligenceEngine` | `app/portfolio/decision.py` — L60, missing is never safe | 458 |
| multi-horizon (§19) | `app/portfolio/horizon.py` — L65, orthogonal to authority | 447 |
| `CapitalPreservationEngine` | `app/portfolio/control.py` — L61, self-loosening forbidden by classification | 257 |
| concentration, gross/net (§4) | `app/portfolio/exposure.py` | 432 |
| final veto | `app/risk/` — 30+ codes | 2,995 |
| capital reservations | migration `0027_capital_reservations` | — |

**Nothing was duplicated.** `allocation.py` is a bridge of ~470 lines that
consumes `AccountState` and `Freshness` from `portfolio.state` and defers to
the platform's existing `allows_new_risk()` rather than deciding risk itself.

## The three rules

**1. Zero allocation is always valid** (§6, §34.10). `NO_ALLOCATION` is the
default and needs no justification; every other outcome does. An engine that
must always allocate something is a forced buyer.

**2. Hard constraints are never overridden** (§7, §34.15). Ten codes, checked
by `_hard_constraints()`, which **deliberately takes no score argument** — a
constraint that can see the conviction behind a request is one that can be
argued with. `test_a_perfect_opportunity_cannot_override_capital_preservation`
pins it: score 1.0, `strong_edge`, and the answer is still `BLOCKED`.

**3. Nothing is ever sold automatically** (§9). Sell-to-fund emits
`REDUCE_REVIEW` for a human. Observing that capital is short and a position is
weak is not authority to sell it.

## Capital preservation tightens; it does not merely veto

`DEFENSIVE` does not block — it caps the best available outcome at
`REVIEW_REQUIRED`, so an allocation cannot be reached without a human.
`CAPITAL_PRESERVATION` and `EMERGENCY` are hard blocks. This mirrors
`portfolio/scenario.py`'s monotone rule: each later stage may make the answer
more conservative and never less.

## What refuses to compute, and why that is correct

`MIN_HOLDINGS_FOR_PORTFOLIO_MATH = 2`. Below it, marginal contribution and
portfolio fit return `INDETERMINATE` with the reason named.

The portfolio here has **one open position at its historical peak and none
today**. A correlation over one position is not a small correlation — it is
not a correlation. `CLAUDE.md` already states the requirement in the form that
does not go stale: *correlation, fragility and cross-instrument stress need a
common window across two or more instruments.*

Even when concentration *is* computable (a Herfindahl index over position
values — the one portfolio-relative quantity derivable from what is stored),
`d_risk`, `d_drawdown` and `d_correlation` stay `None` and `reason_absent`
says why: they need a per-holding return series that is not stored for these
instruments. `test_risk_and_correlation_deltas_stay_absent_even_when_computed`
holds that line.

## No edge means wait, not act

`waiting_value()` returns `WAIT_FOR_INFORMATION` whenever
`opportunity_edge == "no_edge"` — which is L83's default. Given six search
families across five universes have failed to clear their own permutation
nulls, this is the ordinary path, not the exceptional one. The repository's
standing finding, encoded as a portfolio decision.

## Safety

`test_the_module_cannot_reach_a_venue` parses the import graph with `ast` and
fails on `app.oms`, `app.brokers`, `app.execution` or `MetaTrader5`. §34.1–3
enforced, not asserted. Every proposal carries `valid_until` (§28) and states
in `uncertainty` that the RiskEngine remains the final veto.

## Not built

Counterfactual portfolios (§22), historical replay (§23), allocation
attribution (§24), post-mortem (§25), improvement proposals (§26),
champion/challenger (§27), persistence (§30 — 11 tables), API (§31), UI (§29).

All of them need either a portfolio with more than one position or an
allocation history that does not exist. `docs/PORTFOLIO_FIT.md`,
`CAPITAL_COMPETITION.md`, `COUNTERFACTUAL_PORTFOLIOS.md`,
`PORTFOLIO_FRAGILITY.md`, `CAPITAL_PRESERVATION.md` and
`ALLOCATION_ATTRIBUTION.md` are deferred with them — this repository already
carries 177 root documents, several describing mechanisms that drifted from
the code, and adding six more for unbuilt machinery would repeat that.
