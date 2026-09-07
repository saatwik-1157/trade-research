# COUNTERFACTUAL_TESTING.md

Counterfactual and fault-injection testing. L62 §10 and §11, 2026-09-06.

---

## What L62 tests counterfactually

Every condition in `PolicyVerificationEngine.verify()` is a parameter, not a
global read. That is what makes the what-ifs of section 10 expressible as
ordinary tests with no simulation harness and **no way to touch real trading
state**:

| What if… | Expressed as | Test |
|---|---|---|
| the risk limit were lower | `SafetyEnvelope(max_capital_movement=…)` | `test_a_proposal_over_the_approved_budget_is_refused` |
| the RiskEngine rejected | `risk_veto=…` | `test_a_risk_veto_is_not_retried_around` |
| market data went stale | `data_age=`, `DecisionContext` | `test_stale_evidence_defers_rather_than_permits` |
| AI became unavailable | `confidence=None` | `test_an_absent_advisory_input_cannot_make_a_decision_more_permissive` |
| a strategy were quarantined | `quarantined=True` | `test_a_quarantined_strategy_gets_no_new_exposure` |
| the broker state were unknown | `unreconciled_order=True` | `test_an_unknown_venue_state_is_reconciled_before_anything_moves` |
| the same action arrived twice | resubmission | `test_the_same_proposal_twice_is_one_action` |
| the policy version changed | two envelopes | `test_a_decision_is_verified_against_exactly_one_policy_version` |
| live trading were on | `environment="live"` | `test_no_autonomous_action_touches_a_live_environment` |

The engine is pure with respect to trading state: it reads no database, opens
no connection and calls nothing that trades — asserted by
`test_the_verification_module_never_reaches_a_venue` and by the package-level
import guard.

## The property that found a defect

TEST J is written as a **property over the whole confidence range** rather than
as a single scenario:

> for every confidence an advisory input could carry, the outcome with it
> absent is never more permissive than the outcome with it present.

It failed on first run at `confidence=0.0`, and the failure was real: an absent
confidence passed a bar that a stated zero failed. A single-scenario test would
have picked one value and passed. See `AUTONOMOUS_CONTROL_VERIFICATION.md`.

**This is the argument for property framing over example framing**, in a case
where it paid immediately.

## Fault injection: what already exists

Section 11 lists twenty fault conditions. Most were already covered before L62
and are not duplicated here — the platform has existing tests for DB failure,
Redis failure, worker crash, broker unreachable, MT5 disconnect, unknown order
state, notification failure, monitoring failure, duplicate webhook, and
malformed signals. The recovery suite alone carries the safe-mode latching for
several of them:

- `test_a_missing_table_latches_safe_mode_rather_than_migrating`
- `test_an_unknown_order_latches_safe_mode_and_is_never_retried`
- `test_an_unsettled_position_latches_safe_mode`
- `test_a_failing_notification_does_not_fail_a_trade`
- `test_a_failing_bus_never_fails_the_request`

The last two are the shape worth noting: a **non-safety** subsystem failing
must not fail a trade, while a **safety** subsystem failing must. Both
directions are tested, and getting either backwards is a defect.

## What is not simulated

Concurrent autonomous decisions, action storms under load, and deadlock between
control loops. **There is no control loop running**, so there is nothing to run
concurrently. Section 29's load tests would be measuring a harness rather than
the platform.
