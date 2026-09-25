# POLICY_HYPOTHESIS_POLICY.md

L64 section 7. What a research proposal must contain.

---

A `Hypothesis` carries: id, title, motivation, target, baseline policy version,
expected effect, validation plan, rollback plan. `as_dict()` adds the target's
**category**, computed rather than declared, so a hypothesis cannot describe
itself as governance research when it is aimed at a hard limit.

## The two examples from section 7

**Acceptable:** *"Reducing adaptive allocation frequency may decrease
unnecessary portfolio churn without materially reducing strategy
responsiveness."* Target `oscillation_limit` — Category B, on the allow-list
with bounds 2–6.

**Automatically rejected:** *"Increase maximum portfolio risk to improve
returns."* Target `max_portfolio_risk` — Category A. Not rejected after review:
**not reviewable.**

Both are asserted in `test_a_hypothesis_carries_its_category`.

## Why the rollback plan is required at hypothesis time

Not at deployment time, which is when it would be convenient to write one. A
hypothesis whose author cannot say how to undo it has not finished thinking
about it, and the moment a rollback plan is genuinely needed is the worst
moment to be drafting it.

## What is not built

Hypothesis *generation*. Section 5 asks the system to identify improvement
opportunities from assurance data, certification history, policy violations and
autonomy downgrades. **None of those records exist** — no autonomous action has
ever been applied here, so there are no violations, no downgrades and no
effectiveness history to mine. Generating hypotheses from an empty record would
produce plausible sentences with nothing behind them.
