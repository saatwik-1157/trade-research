# CONTINUOUS_DRIFT_ASSURANCE.md

Drift. L63 section 12, 2026-09-06.

---

## The rule that matters most

**Drift is not failure.** Section 12 says so explicitly, and it is the sentence
most likely to be lost when somebody wires a drift detector to an alert. Drift
is evaluated against a policy threshold; a distribution moving is information,
not a verdict.

## What already exists, and is not duplicated

`ModelMonitoring` and `StrategyMonitoring` are real, running and tested
(`tests/test_model_monitoring.py`, `tests/test_monitoring.py`). Section 12 says
to reuse their results rather than build a second detector, and nothing here
re-derives what they measure.

`app.datasets.leakage` covers the data-side property that matters most: a
feature computed from information that did not exist at its timestamp is
refused. That predates L62 and is bound as INV-22's evidence.

## What is not built, and why

`POLICY_DRIFT`, `CONFIGURATION_DRIFT`, `EXECUTION_DRIFT` and `BEHAVIOR_DRIFT`
as continuously-evaluated signals.

The first two have a real substitute that IS built: **change impact analysis**
(`CERTIFICATION_IMPACT_ANALYSIS.md`) answers the question drift detection would
answer for policy and configuration, and answers it from the actual diff rather
than from a statistic about it. A changed file is a fact; a drift score over a
changed file is an inference from one.

`EXECUTION_DRIFT` and `BEHAVIOR_DRIFT` compare actual autonomous behaviour
against expected. **There is no autonomous behaviour.** A detector built now
would be calibrated against a distribution with no samples in it.

## The one drift control that is live

Certification expiry. A certificate that is thirty days old has drifted from
the evidence that produced it by definition, whether or not any metric moved,
and `lifecycle.assess` acts on that without needing a model.
