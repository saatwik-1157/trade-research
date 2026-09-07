# SCENARIO_REPLAY_POLICY.md

L66 sections 5, 14 and 37. Historical scenarios and leakage.

---

## The leakage control already exists

`app.datasets.leakage` refuses a feature computed from information that did not
exist at its timestamp -- including the subtle case where a scaler fitted on the
whole series leaks the future into every row without any single feature reading
forward. It predates L62 and is bound as INV-22's evidence.

It is **not duplicated here**. Section 46's TEST 8 (scenario uses future
information during replay) is that control's job.

## What this module adds

A historical scenario must name its `data_version`, or it is refused at
validation.

That is narrower than a leakage check and it is the part that belongs at
definition time: a replay that does not say what it replayed cannot be
reproduced *or* checked. The leakage control can only run on a scenario whose
data is identified.

## Reproducibility

The fingerprint covers data version, model version, policy version and seed. A
scenario re-run under different versions is a different scenario, not a
duplicate -- so a result cannot be silently attributed to the wrong inputs.

## No wall clock

`gate()` and `Outlook.expired()` take `now` as a parameter. There is no call to
a system clock anywhere in the module, so replaying at a historical timestamp
uses that timestamp throughout. It is not a mode; it is the only way the
functions work.

## Not built

Reconstruction of a historical scenario run. None have been run. Building the
replay against imagined records, and validating it by generating those records,
would fabricate the evidence the replay exists to check.
