# ASSURANCE_REPLAY_POLICY.md

Reconstructing a past certification. L63 sections 25 and 26, 2026-09-06.

---

**See `DECISION_REPLAY_POLICY.md` (L62) first.** It records what replay
machinery exists, and this adds only what L63 asks on top.

## What exists

`app.datasets.leakage` — the strongest reproducibility control in the
repository, predating L62 by many levels. It refuses a feature computed from
information that did not exist at its timestamp, including the subtle case: a
scaler fitted on the whole series leaks the future into every row without any
single feature reading forward.

`policy_version` — a hash of the rules actually applied (the envelope's values
plus the source of both classifier modules), so a verification can be replayed
against the rules that made it. Not the id of a stored policy, because there is
no stored policy.

## What is not built

**Reconstructing a historical certification state, autonomy level and evidence
set at a given timestamp.**

Section 25 asks for exactly that. It requires a history of certification events,
autonomy changes and evidence snapshots. **None has ever been recorded**, because
certification has been evaluated exactly as many times as somebody has run
`certify()` and no autonomous action has ever been applied.

Building the replay engine now would mean building it against imagined records,
and validating it would mean generating those records — which is fabricating
the evidence the replay exists to check.

## Assurance simulation

Section 26 asks for simulations of prolonged stale data, repeated broker
disconnects, rising drawdown, model drift, certification expiry and recovery
after critical failure, with deterministic expected transitions.

**Those exist, as tests rather than as a simulation harness.** Every condition
in `assess()` and `downgrade_for()` is a parameter, so each scenario is an
ordinary test with a deterministic expected transition, and none of them can
touch real state:

| Scenario | Test |
|---|---|
| certification expiry | `test_an_expired_certification_downgrades_autonomy_by_itself` |
| stale evidence | `test_evidence_that_cannot_be_reconstructed_degrades_certification` |
| unknown safety state | `test_an_unknown_safety_state_fails_closed` |
| critical invariant failure | `test_a_critical_invariant_failure_revokes_and_suspends_at_once` |
| recovery after suspension | `test_recovery_restores_one_step_at_a_time_and_never_jumps` |
| repeated action anomaly | `test_a_repeated_action_anomaly_restricts_the_loop` |

A separate simulation harness would run the same functions with the same inputs
and record the results in a table nobody reads.
