# RESILIENCE_OPTIMIZATION_POLICY.md

L67 sections 17, 18 and 19. Recommending improvement.

---

## It cannot expand approved risk

Sections 17 and 43's TESTS 2, 3 and 13.

Enforced twice, at layers that already existed:

- **`app.safety.envelope`** (L62) -- `max_capital_movement` and `max_leverage`
  are hard limits; an action aimed at one is FORBIDDEN with no approval path.
- **`app.safety.policy_research`** (L64) -- both are Category A, refused at
  static validation before any simulation runs.

`test_a_resilience_optimizer_cannot_raise_the_risk_budget` asserts both by name
so the resilience path is covered explicitly rather than by inheritance.

## The frontier is not selected from

Section 19: produce a conceptual frontier, and **do not automatically select a
point on it.** Selection is policy and governance.

That is the whole reason a frontier is safer than an optimum: an optimum
implies a choice has been made.

## Do not optimize solely for returns

Section 18. The comparison metrics are risk, drawdown, stress loss, recovery,
concentration, correlation and capital efficiency -- with expected return one
of eight, not the objective.

## Not built

The optimizer itself, and the baseline-versus-resilient comparison. Both need a
portfolio to rearrange. What exists is the boundary it would have to operate
inside, which is the half that must be right first.
