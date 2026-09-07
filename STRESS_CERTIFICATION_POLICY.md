# STRESS_CERTIFICATION_POLICY.md

L67 section 33. Stress coverage and certification.

---

## No second certification system

Section 33's last line. L63's certification is the one, and this integrates
with it rather than adding another.

## How stress reaches certification

A critical unresolved resilience failure produces
`ResilienceStatus.CRITICAL`, whose `blocks_autonomy` is `True`. `UNKNOWN` does
the same.

The route from there is L63's existing machinery: `downgrade_for` takes
`critical_invariant_failed` and `safety_state_unknown`, and both drop autonomy
to `OBSERVE`. Nothing new was needed.

## Coverage as a certification input

Section 33 suggests certification may require minimum stress coverage, critical
scenario coverage and recent stress validation.

**Not wired**, and deliberately: on this deployment coverage is 0 of 16, so
wiring it would immediately and permanently suspend autonomy for a reason that
is true but not actionable -- no stress can be run against a portfolio with one
position and one instrument.

Recording it as unwired is more useful than wiring it into a permanent red
light nobody can clear. The day stresses can run, this becomes a real gate.

## What certification is today

`CONDITIONALLY_CERTIFIED`, unchanged. Autonomy ceiling `BOUNDED`.
