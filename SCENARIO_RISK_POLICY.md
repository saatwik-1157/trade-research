# SCENARIO_RISK_POLICY.md

L66 sections 11, 20, 21 and 31. Risk under a scenario.

---

## Scenario risk is not RiskEngine state

Section 11's last line: scenario calculations must not modify actual RiskEngine
state.

They cannot. The module imports nothing from `app.`, so there is no reference
to the RiskEngine to modify. Isolation here is structural rather than
disciplined.

## Ranking never uses likelihood alone

Section 20. See the matrix in `PORTFOLIO_SCENARIO_ARCHITECTURE.md`: an unlikely
EXTREME scenario classifies HIGH, and an unlikely EXTREME outranks a likely
EXPECTED.

The matrix is enumerated rather than computed because likelihood x severity is
exactly the arithmetic that buries a rare catastrophe under a common
inconvenience.

## A hard constraint is not a scenario input

Section 31: if a severe scenario violates a hard constraint, the action is
rejected. `breaches_hard_constraint=True` returns REJECT unconditionally, and
no confidence, severity or likelihood softens it.

## Budgets are enforced elsewhere and asserted here

Section 46's TEST 12 -- approved $10,000, scenario optimizer recommends
$15,000, expect REJECT.

This module cannot size anything. The ceiling is enforced by
`app.safety.envelope` (L62, hard limit, no approval path) and
`app.safety.policy_research` (L64, Category A, refused at static validation).
`test_no_scenario_can_turn_10000_of_approved_risk_into_15000` asserts both by
name so the scenario path is covered explicitly rather than by inheritance.
