# STRESS_CALIBRATION_POLICY.md

L67 sections 31 and 32. Was the stress right?

---

## Not built, and the reason is arithmetic

Calibration compares predicted stress against actual events. It requires
predictions that were made and events that happened.

**Zero stresses have been run on this platform.** Severity error, missed
scenarios, false warnings and recovery-estimate error are all statistics over
an empty sample.

`CLAUDE.md` documents at length what this project's own searches produced when
run against insufficient data -- five universes, four search families, and in
every case the winner did not survive out-of-sample. A calibration report over
zero observations is that failure with the sample size taken to its limit, and
it would arrive wearing a methodology.

## The rules that will apply

**Do not automatically alter hard safety.** Section 31's last line. Calibration
feeds model monitoring, the research orchestrator and policy research -- all of
which propose, and none of which applies.

**Avoid data leakage.** Section 32. `app.datasets.leakage` is the control and
predates L62; it is bound as INV-22's evidence and is not duplicated.

**Calibrate by regime, strategy, portfolio, account, symbol and timeframe.**
Recorded because pooling across them is how a calibration figure becomes an
average of incomparable things -- the same error `CLAUDE.md` documents for
points across metals and indices, where a 59x unit spread made the pooled
figure meaningless.
