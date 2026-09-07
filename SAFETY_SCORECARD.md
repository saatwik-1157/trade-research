# SAFETY_SCORECARD.md

L62 §16. Generated 2026-09-06 from `app/safety/`.

**Certification: CONDITIONALLY_CERTIFIED**

Statuses follow the same rule as the gates: a category whose invariants are all
NOT_APPLICABLE reads `NOT_TESTED`, never PASS. A category is only as strong as
its weakest invariant, and the severity column is the worst in the category
rather than an average - averaging severities is how a CRITICAL gets hidden
behind two MEDIUMs.

| | Category | Status | Worst severity | Invariants | Note |
|---|---|---|---|---|---|
| A | Risk Safety | **PASS** | CRITICAL | INV-01, INV-02, INV-05 | - |
| B | Capital Safety | **PASS** | CRITICAL | INV-06, INV-17 | - |
| C | Execution Safety | **PASS** | CRITICAL | INV-10, INV-03, INV-04 | - |
| D | Data Safety | **PASS** | CRITICAL | INV-11, INV-22 | - |
| E | Model Safety | **PASS** | CRITICAL | INV-02, INV-23 | - |
| F | Policy Safety | **PASS** | CRITICAL | INV-15, INV-16, INV-14 | - |
| G | Account Isolation | **PASS** | CRITICAL | INV-12, INV-21 | - |
| H | Recovery Safety | **PASS** | CRITICAL | INV-19, INV-20 | - |
| I | Control Loop Safety | **PASS** | HIGH | INV-18, INV-17 | - |
| J | Auditability | **PASS** | CRITICAL | INV-14, INV-21 | - |
| K | Security | **PASS** | CRITICAL | INV-07, INV-08, INV-09, INV-13 | - |
| L | Reproducibility | **WARNING** | CRITICAL | INV-22, INV-25, INV-24 | INV-25, INV-24 not applicable |

Last tested: 2026-09-06, `tests/test_safety_invariants.py` (60 tests) and
`tests/test_policy_verification.py` (38 tests).

Remediation for the two WARNING rows is the same in both cases and is not a
fix: the components those invariants constrain do not exist. See
`AUTONOMOUS_CONTROL_VERIFICATION.md`.
