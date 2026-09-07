# POLICY_REGRESSION_POLICY.md

Policy regression and shadow comparison. L62 §14 and §15, 2026-09-06.

---

## The rule

**A policy change must not silently remove a safety rule.**

At L62 the mechanism for that is the invariant registry, not a stored suite of
policy versions. `tests/test_safety_invariants.py` asserts that every invariant
marked ENFORCED names a module that exists and a test that exists, so deleting
the enforcement without deleting the claim fails the build.

The check earns its place: on first run it found **thirteen** evidence pointers
that did not resolve — eleven naming real tests in the wrong file, two naming
nothing at all.

## What a policy change must do

1. Move the value or rule in `app/safety/envelope.py`, `app/portfolio/control.py`
   or `app/portfolio/decision.py`.
2. Re-run `tests/test_safety_invariants.py` and `tests/test_policy_verification.py`.
3. Re-run `certify()`. A CRITICAL invariant failure moves the platform to
   `CERTIFICATION_REVOKED`, not to a lower score.
4. Record the change in `DECISIONS.md` with the reasoning.

Every rule change alters `policy_version`, which is a hash of the envelope plus
the source of both classifier modules. Two verifications produced under
different rules cannot be confused for one another, and an action verified
under the old rules is not treated as already-verified under the new ones — the
fingerprint includes the version.

## Shadow comparison: not built, and why

Section 14 asks for a current policy run against a challenger policy, with the
challenger non-executing, comparing decisions, rejected actions, risk impact,
false positives and false negatives.

**There is one policy and it is a pure function.** A challenger comparison
needs two policies and a stream of real decisions to run both over; this
platform has produced no autonomous decisions at all, so the comparison would
have an empty input and its "false positive rate" would be a number computed
from nothing.

The machinery it would need already half-exists and is worth naming so it is
not rebuilt later: `app/brokers/shadow.py` and `tests/test_shadow.py` implement
a non-executing shadow adapter, and that is the right pattern to extend when
there is something to shadow.

## Regression suite

`tests/test_policy_verification.py` is the regression suite for the current
rules: 38 tests, including all fifteen of section 28's mandatory cases and the
regression for the defect L62 found.

Its own no-duplication rule is asserted rather than trusted.
`test_the_verifier_delegates_rather_than_re_deciding` parses the verification
module and fails if it grows its own `classify`, its own `decide` or its own
risk engine — because a second policy engine never arrives named as one, it
arrives as a wrapper that started re-deriving what it was supposed to call.
