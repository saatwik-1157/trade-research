# SCENARIO_VALIDATION_POLICY.md

L66 section 8. What is checked before anything runs.

---

Every check runs; a scenario is refused for **every** reason it is refusable.
An author reading a rejection wants the whole list, not the first item.

| Check | Refusal |
|---|---|
| environment is `live` | a simulation naming live is read as being about live |
| unrecognised input | nobody bounded it, so nobody thought about it |
| input outside bounds | stated with both numbers |
| hypothetical changing nothing | not a scenario |
| historical without a data version | cannot be reproduced or checked for leakage |
| duplicate fingerprint | the same question under the same versions |

## What isolation actually rests on

Section 8 requires no production mutation, no broker access, no MT5 access.

**None of those is enforced by a check.** They hold because the module imports
nothing from `app.` at all -- no session, no engine, no repository, no adapter.
`test_a_scenario_cannot_mutate_production_state` parses the module and asserts
the import list is empty of application modules, and that it calls no `commit`,
`flush`, `execute`, `add`, `delete` or `merge`.

A guard would imply there is a door. There is not one.
