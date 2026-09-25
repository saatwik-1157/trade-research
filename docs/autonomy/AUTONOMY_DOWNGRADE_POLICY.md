# AUTONOMY_DOWNGRADE_POLICY.md

L63 section 16. See `CERTIFICATION_VIOLATION_POLICY.md` for the rule table and
`CONTROLLED_RESTORATION_POLICY.md` for the way back.

---

**This is a pointer, not a third copy.** The downgrade rules are one table and
they live in `CERTIFICATION_VIOLATION_POLICY.md`; restating them here is how
two documents describing one table come to disagree.

The three properties that belong to downgrade specifically:

**1. Falling is automatic and one step.** No approval is needed to reduce
autonomy, on the same reasoning the safe-mode route already gives for requiring
no step-up: a control that is expensive to engage is one nobody engages in an
emergency.

**2. Climbing is staged and needs evidence.** A condition clearing is not
evidence that the thing it broke now works. Restoration goes through
REVALIDATING and returns one level at a time, capped by the certification
ceiling.

**3. Unknown is treated as failure, not as absence.** "The safety state could
not be established" drops to OBSERVE, the same as an outright critical failure,
because from where the platform is standing the two are indistinguishable.
Anything softer would make not-measuring the cheapest way to stay certified.

`test_autonomy_is_a_ceiling_and_nothing_here_raises_it` asserts across the
whole cross-product of levels and conditions that no branch returns a higher
level than it was given.
