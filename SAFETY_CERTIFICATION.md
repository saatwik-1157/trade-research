# SAFETY_CERTIFICATION.md

The twelve gates and what the platform is certified for. L62, 2026-09-06.

Generated from `backend/app/safety/certification.py`.

---

## Result

# CONDITIONALLY_CERTIFIED

**Not CERTIFIED, and the difference is the point.** Ten gates pass outright.
Two are `WARNING` because part of what they would check does not exist in this
repository, and a gate that has verified nothing is not reported as passing.

| Gate | Name | Question | Status | Basis |
|---|---|---|---|---|
| GATE-01 | Architecture Safety | Is there any unauthorized execution path? | **WARNING** | INV-03, INV-04 verified; INV-24 not applicable here |
| GATE-02 | Risk Safety | Is the RiskEngine still the final veto? | **PASS** | INV-01, INV-02 verified |
| GATE-03 | Policy Safety | Can a hard constraint be overridden? | **PASS** | INV-05, INV-06, INV-15, INV-16 verified |
| GATE-04 | State Safety | Are invalid transitions rejected? | **PASS** | INV-13, INV-14 verified |
| GATE-05 | Data Safety | Can stale or future data drive an unsafe decision? | **PASS** | INV-11, INV-22 verified |
| GATE-06 | Execution Safety | Are unknown order states reconciled? | **PASS** | INV-10 verified |
| GATE-07 | Recovery Safety | Do failures enter a safe state? | **PASS** | INV-19, INV-20 verified |
| GATE-08 | Control Loop Safety | Can adaptation run away? | **PASS** | INV-17, INV-18, INV-21 verified |
| GATE-09 | Account Isolation | Can one account contaminate another? | **PASS** | INV-12 verified |
| GATE-10 | Reproducibility | Can a historical decision be replayed? | **WARNING** | INV-22 verified; INV-25 not applicable here |
| GATE-11 | Security | Can AI or a caller cross a privilege boundary? | **PASS** | INV-02, INV-23 verified |
| GATE-12 | Environment Safety | Is paper still the default and live still off? | **PASS** | INV-07, INV-08, INV-09 verified |

## The two rules that make the claim falsifiable

A certification is only worth something if it can come out negative.

**A gate whose invariants are all NOT_APPLICABLE returns `NOT_TESTED`, never
PASS.** A gate around a component that does not exist has verified nothing.

**One failing CRITICAL invariant revokes certification outright**, with no
weighing against the others. Twenty-four passes do not average out a breached
veto. `test_a_failed_critical_invariant_revokes_certification` and
`test_one_critical_failure_is_not_outweighed_by_everything_else` prove both
directions.

## What holds it at CONDITIONAL

**GATE-01 Architecture Safety** — INV-03 and INV-04 are enforced and tested;
INV-24 constrains an autonomous decision engine that does not exist.

**GATE-10 Reproducibility** — INV-22 (no future data in replay) is enforced by
`app.datasets.leakage`, which predates this level and is the strongest
reproducibility control in the repository. INV-25 (immutable policy versions,
deterministic rollback) has no policy object to apply to.

Both clear when the components exist. Neither is a defect.

## States

`NOT_CERTIFIED` · `CONDITIONALLY_CERTIFIED` · `CERTIFIED` ·
`CERTIFICATION_EXPIRED` · `CERTIFICATION_REVOKED`

A CRITICAL invariant failure moves straight to `CERTIFICATION_REVOKED`.
