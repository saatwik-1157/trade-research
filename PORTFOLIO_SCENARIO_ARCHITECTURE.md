# PORTFOLIO_SCENARIO_ARCHITECTURE.md

Predictive scenario intelligence. L66, 2026-09-06.
Source: `backend/app/portfolio/scenario.py`.

---

## One rule carries the module

**A prediction may tighten and may never loosen.**

The asymmetry is not caution for its own sake, it is the cost function:

- A scenario that wrongly predicts **danger** costs a missed opportunity.
- A scenario that wrongly predicts **safety** costs the portfolio.

Those are not the same mistake, so they must not carry the same authority. The
way that gets lost is a gate returning a verdict computed symmetrically from a
forecast.

So `gate()` takes the verdict the platform **already holds** and returns
`max(current, everything the scenario adds)`. A scenario predicting calm --
with 95% confidence on every component -- cannot upgrade a RESTRICT to a PASS.
It can only decline to add anything.

This makes section 47's rule (prediction must never become direct execution) a
property of the return value rather than a policy somebody enforces. It is
L61's "autonomy may take permissions away and never grant them", applied to
forecasting.

`test_no_scenario_input_can_ever_lower_the_verdict` checks every severity x
likelihood x confidence combination against every verdict the platform might
hold. There is no cell where the gate is more permissive than what it was
given.

## It is not a RiskEngine

Section 32 says so and it bears repeating: the gate decides whether a proposed
adaptation is **worth putting in front of the safety chain**, not whether it is
safe. `may_proceed` means "to verification", never "to a venue".

After it: certification, policy verification, safety validation, the portfolio
risk orchestrator, the RiskEngine, the position sizer, the OMS. The gate
replaces none of them.

## The risk matrix

| | EXPECTED | ADVERSE | SEVERE | EXTREME |
|---|---|---|---|---|
| **UNLIKELY** | LOW | LOW | MEDIUM | HIGH |
| **POSSIBLE** | LOW | MEDIUM | HIGH | CRITICAL |
| **LIKELY** | MEDIUM | HIGH | CRITICAL | CRITICAL |

**Read the UNLIKELY row.** An unlikely EXTREME scenario is HIGH, not LOW.
Section 20 says low-likelihood/high-severity events must stay visible, and
multiplying likelihood by severity is precisely what makes them vanish. The
matrix is written out rather than computed for that reason -- a product would
have been shorter and would have buried the row this exists to protect.

`EXTREME` is a stress test, not a forecast. Section 19 says so, and it matters
because an extreme scenario ranked beside a likely one invites being read as a
prediction.

## On conflict, the conservative side wins

Section 16. `conservative_of(baseline, model)` returns the more restrictive of
a deterministic baseline and a model output.

**A model is never consulted to relax what a baseline already said.** It is
consulted only to make the answer more conservative -- so an absent or
low-confidence model degrades to the baseline rather than to nothing, and
section 46's TEST 10 needs no fallback branch because the baseline was always
the floor.

## Recommendations are classified by effect, not by name

Every `Recommendation` carries a declared effect, and all of them are TIGHTENS
or NEUTRAL. There is no INCREASE, no RAISE, no ENABLE -- section 33's list
contains none either, and that is not an oversight in the brief. A forecast
with an upside is one somebody will eventually act on for its upside.

The first version of the test for this matched substrings and flagged
`DEFER_INCREASE`, which *withholds* an increase and is a restriction. Matching
characters produces false positives and, worse, false negatives -- a member
called `OPTIMISE_HEADROOM` would have passed. Classified by effect now, the
same way L61 classifies actions.

## The data constraint

This deployment has **700 H1 bars for one instrument** (EURUSD, one month) and
one open position. That is the whole market history in the database.

Correlation needs two or more instruments over a common window, so correlation
scenarios cannot run. Volatility and liquidity scenarios estimated from that
sample would be estimates from one month of one symbol. Calibration needs
predictions that were made and outcomes that arrived; there are none.

`app/portfolio/exposure.py` has reported correlation as unavailable since L53
and still does -- **its verdict was right and its stated reason had gone
stale**, claiming `market_bars` was empty when it now holds 700 rows. Corrected
at L66, because the next reader checks the reason to decide whether the
situation has changed.

## What was built

Scenario definition with bounded inputs, validation, fingerprinting and
duplicate detection, the risk matrix, component confidence, the gate, the
baseline/model conservative rule, and predictive warnings that say they are
predictions.

## What was not built

Scenario **simulation** -- portfolio impact, risk impact, capital impact,
execution impact, fragility, marginal contribution, strategy interaction,
cascading failure, calibration, feedback, drift integration, migrations, APIs
and the frontend dashboard.

All of it needs either market data across several instruments or a history of
predictions and outcomes. Neither exists. A fragility analysis over one
instrument, or a calibration report over zero predictions, would be a number
with a methodology attached and nothing underneath.

What exists is the half that must be right **before** the first scenario runs:
what a scenario may claim, and what a claim is allowed to change.
