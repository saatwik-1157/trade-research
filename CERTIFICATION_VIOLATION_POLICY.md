# CERTIFICATION_VIOLATION_POLICY.md

Violations and what they cost. L63 §14 and §16, 2026-09-06.

---

## Severities

`INFO` · `LOW` · `MEDIUM` · `HIGH` · `CRITICAL`

## Downgrade rules

Deterministic, written worst-first, first match wins. From
`autonomy.downgrade_for`:

| Condition | Autonomy drops to | Severity |
|---|---|---|
| A critical invariant failed | **OBSERVE** | CRITICAL |
| Safety state could not be established | **OBSERVE** | CRITICAL |
| Any HIGH-severity policy violation | APPROVAL | HIGH |
| Evidence stale | APPROVAL | MEDIUM |
| 3+ MEDIUM violations | BOUNDED | MEDIUM |
| Otherwise | unchanged | — |

Two of these are worth reading closely.

**Unknown is as severe as failure.** "The safety state could not be
established" drops to OBSERVE, the same as an outright critical failure,
because from where the platform is standing the two are indistinguishable. Any
softer treatment would make not-measuring the cheapest way to stay certified.

**Three MEDIUMs, not one.** One is noise; a run of them is a pattern, and the
pattern is precisely what a single-event rule cannot see. This is the L51
lesson again — five good gates and none asking *how many times*.

## Every branch only lowers

`test_autonomy_is_a_ceiling_and_nothing_here_raises_it` checks the whole
cross-product of current levels against every failure condition and asserts no
path returns a higher level. Restoration is a separate, staged process a person
starts; see `CONTROLLED_RESTORATION_POLICY.md`.

## Not built

A stored violation table, violation history and repeat-offender tracking. Every
one of those needs a running control loop to produce violations. What exists is
the classification and the response, which are the parts that must be right
before the first violation.
