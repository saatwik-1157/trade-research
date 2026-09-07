# DECISION_REPLAY_POLICY.md

Replay and reproducibility. L62 §9, 2026-09-06.

---

## What exists, and it is not new

**`app.datasets.leakage` is the strongest reproducibility control in this
repository, and it predates L62 by many levels.** It refuses a feature computed
from information that did not exist at its timestamp, and it is bound as
INV-22's evidence rather than reimplemented here.

Its tests are worth naming because they are what a replay policy actually rests
on:

- `test_a_feature_at_T_does_not_change_when_the_future_arrives`
- `test_the_leakage_check_reports_a_feature_that_reads_forward`
- `test_a_label_reads_the_future_and_a_feature_does_not`
- `test_a_chronological_split_never_mixes_past_and_future`
- `test_the_leakage_check_catches_a_scaler_fitted_on_the_whole_series`

The last one is the subtle case: a scaler fitted on the whole series leaks the
future into every row without any single feature reading forward.

`app/replay/` provides a clock and an engine for market replay, and
`tools/rule_search.py` carries the era blocks, walk-forward and date clustering
that `CLAUDE.md` documents at length.

## What L62 did NOT build, and why

Section 9 asks for deterministic replay of **autonomous decisions**, recording
portfolio state, policy version, model versions, action proposal, verification
result, approval state and execution result at each historical timestamp.

**No autonomous decision has ever been made on this platform.** There is no
decision history to replay. Building a replay engine now would mean building it
against imagined records, and validating it would mean generating those records
— which is fabricating the evidence the replay exists to check.

## What is real about the version stamp

A `PolicyVerificationResult` carries a `policy_version`, and it is **a hash of
the rules actually applied** — the envelope's values plus the source of both
classifier modules — not the id of a stored policy, because no stored policy
exists.

That is deliberately narrower than section 19 asks for, and it is honest. It
delivers the property a version is *for*: a decision can be replayed against
the rules that made it, and a change to any rule produces a different version.

`test_a_decision_is_verified_against_exactly_one_policy_version` asserts both
directions — different rules version differently, identical rules version
identically.

**INV-25 is NOT_APPLICABLE and GATE-10 is WARNING because of this.** Reporting
model versioning (`app.ai.registry_service`, which is real and does support
rollback) as policy versioning would be the substitution the invariant registry
exists to prevent.
