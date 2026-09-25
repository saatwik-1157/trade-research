# MULTI_HORIZON_RISK_POLICY.md

L65 sections 20 and 21. Risk across horizons.

---

## The rule

**No horizon can turn an approved budget into a larger one.**

Section 20's example - approved $10,000, long-term optimizer wants $12,000,
expected REJECT - and section 44's TEST 9 with $15,000.

## Where it is enforced

Not here. This module produces a recommendation; it cannot size anything.

The ceiling is enforced twice over, at layers that already existed:

- **`app.safety.envelope`** (L62) - `max_capital_movement` is a hard limit, and
  an action aimed at one is FORBIDDEN with no approval path.
- **`app.safety.policy_research`** (L64) - a capital ceiling is Category A, so
  a candidate proposing to change it is refused at static validation before any
  simulation runs.

`test_no_horizon_can_turn_10000_of_approved_risk_into_15000` asserts both
explicitly rather than relying on inheritance, so the multi-horizon path is
covered by name.

## Long-term optimization is the weakest input in the system

It enters `decide()` as `OPTIMIZATION`, the second-weakest of seven layers. It
cannot outrank a portfolio constraint, a risk constraint or a hard safety
finding. **Being strategic confers no authority.**

## Constraints are applied before optimization

Section 21. The ordering is not a preference: `decide()` takes the most
restrictive verdict, so a constraint that says REDUCE cannot be outvoted by an
optimizer that says ALLOW, whatever the optimizer's confidence.
