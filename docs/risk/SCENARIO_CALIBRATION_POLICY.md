# SCENARIO_CALIBRATION_POLICY.md

L66 sections 34, 35 and 36. Prediction quality.

---

## What calibration requires

Predictions that were made, and outcomes that arrived.

**This platform has neither.** No scenario has been run, so there is no
prediction to score and no error to measure. MAE, RMSE, calibration error,
directional accuracy, false positive rate and false negative rate are all
computed over an empty sample.

A calibration report produced anyway would be a table of metrics whose every
cell was undefined, presented with the authority of a measurement. `CLAUDE.md`
documents at length what this project's own searches produced when run against
insufficient data; a calibration over zero observations is that failure with
the sample size taken to its limit.

## The rule that will apply when there is data

**Do not automatically alter production risk rules.** Section 34's last line.
Calibration feeds model monitoring, the research orchestrator and policy
research -- all of which propose, and none of which applies.

## Model drift falls back to the baseline

Section 36. When a predictive model becomes unreliable, fall back to the
deterministic baseline.

**This needs no fallback branch.** `conservative_of` already treats a
low-confidence or absent model as not displacing the baseline, so an unreliable
model degrades to the baseline by the ordinary rule rather than by a special
case that has to fire correctly.

`ModelMonitoring` exists and is not duplicated.
