# SCENARIO_SECURITY.md

L66 section 44. Isolation.

---

## The scenario engine has no credentials because it has no imports

Section 44 requires that it not access broker credentials, MT5 credentials or
production execution secrets.

`app/portfolio/scenario.py` imports **nothing from `app.`**. Not a session, not
a settings object, not an adapter, not a repository. There is no reference
through which a credential could be reached, and
`test_a_scenario_cannot_mutate_production_state` asserts the application-import
list is empty.

That is stronger than sandboxing: a sandbox implies a door that could otherwise
be opened.

## Input validation

Every scenario input is bounded, and an unrecognised input is refused rather
than passed through -- so a malicious or malformed definition is rejected as
data before anything runs.

Values are `Decimal`. There is no expression, no callable and no interpreter,
so section 45's "arbitrary code execution" test has nothing to execute. Same
structural answer as L64's policy candidates.

## Account isolation

A `ScenarioDefinition` carries its `account_id`, and the fingerprint includes
it -- so the same scenario against a different account is a different scenario
and can never be deduplicated into one.

## RBAC and API authorisation

No API was added, so there is no new surface to authorise. The existing route
guards in `SECURITY.md` are not duplicated by a weaker copy here.
