# STRATEGIC_CONTROL_POLICY.md

L65 sections 6, 19 and 24. The long horizon.

---

## Long-term context never executes

Section 6's last line. It enters `decide()` as `OPTIMIZATION` and nothing in
the module reaches the RiskEngine, the OMS or an adapter.

## A strategy may hold different states at different horizons

Section 19, and it is worth stating because the instinct is to collapse them.

A strategy may be **ACTIVE** short-term, **WATCH** medium-term and **REVIEW**
long-term, all at once and all correctly: it can execute safely today while
being reviewed for retirement next quarter.

Collapsing those into one health field would force a choice between stopping a
strategy that is working and hiding a review that is warranted. This module
does not collapse them - each horizon carries its own recommendation, and the
conflict between them is reported rather than resolved away.

## Research stays non-executing

Section 24. `app/safety/policy_research.py` (L64) has no importer anywhere
under `app/`, asserted by a syntax-tree test. Research cannot affect current
trading because nothing reads it.

## Regime vocabulary is reused

Section 18 says to use the existing regime vocabulary if one is defined. One
is: `app/ai/regime.py` defines `Regime` with `TRENDING_UP`, `TRENDING_DOWN`,
`RANGING`, `HIGH_VOLATILITY`, `LOW_VOLATILITY` and `UNKNOWN`.

So this module defines **no regime enum of its own**. The brief's proposed
TREND / MEAN_REVERSION / RISK_ON / RISK_OFF would have been a second vocabulary
for one concept, and `test_the_existing_regime_vocabulary_is_reused_not_replaced`
asserts none was added.

`UNKNOWN` regime means baseline behaviour, not aggressive adaptation - which
follows from UNKNOWN confidence never being read as high.
