# POLICY_ROLLBACK_POLICY.md

L64 section 30. Automatic rollback.

---

## When a rollback may be automatic

All four must hold: a predefined rollback condition triggered, the policy
explicitly permits rollback, the rollback target is known, and the rollback
itself passes safety checks.

The fourth is the one that is easy to drop. **A rollback is a change, and it is
verified like any other change** - restoring a previous policy is not
self-evidently safe just because it used to run.

## Rollback must not increase risk

The direction rule from L61 applies unchanged: a rollback that loosens a hard
constraint is an action that loosens a hard constraint, and is FORBIDDEN
whatever it is called. A rollback is not an exemption from the classifier; it
is an ordinary action with a sympathetic name.

## The states

`CANARY -> ROLLED_BACK` and `ACTIVE -> ROLLED_BACK` exist.
`ROLLED_BACK -> CANARY` and `ROLLED_BACK -> ACTIVE` do not: a rolled-back
policy is retired, not re-promoted. Re-proposing it means a new candidate that
goes through the pipeline again, and the duplicate check will notice it is the
same policy.

## When the rollback target is unavailable

Section 40's TEST L. Every state can reach RETIRED and REJECTED, so no
candidate is ever stranded in a state that only promotes forward. Whatever goes
wrong, there is a move that deploys nothing.

This is the same fail-closed shape as `ceiling_for` returning OBSERVE for an
unrecognised certification state (L63).

## Not built

Automatic rollback execution, rollback history, and effectiveness measurement
after a rollback. Nothing has ever been deployed, so nothing has ever been
rolled back.
