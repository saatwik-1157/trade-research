# RESILIENCE_BOTTLENECK_POLICY.md

L67 sections 15 and 16. Components that disproportionately reduce resilience.

---

## The type exists and carries its evidence label

`Bottleneck` records the component, the impact, an `Evidence` label
(`OBSERVED` / `INFERRED` / `HYPOTHETICAL`), a severity and a recommendation.

The evidence label is the field that matters. A bottleneck claim -- *this one
strategy dominates portfolio risk* -- is exactly the kind of statement that
gets acted on, and it should not be possible to make it without saying how it
is known.

## Nothing is removed automatically

Section 15's last line. Every `Recommendation` is `TIGHTENS` or `NEUTRAL`, and
none of them executes -- reused from L66 rather than redefined.

## Marginal contribution

Section 16 asks what happens to resilience if a component is removed **in
simulation**.

Not built. It requires a portfolio with several strategies and a simulation
that can run them; this deployment has one open position and seven bots that
are test artefacts with no strategy version.

The rule that will apply when it is built is recorded now: **simulation only,
never mutate production state.** The structural guarantee already exists -- the
module imports nothing that could mutate anything.
