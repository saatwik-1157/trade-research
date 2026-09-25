# POLICY_INVARIANTS.md

The twenty-five safety invariants. L62, 2026-09-06.

Source of truth is `backend/app/safety/invariants.py`. **This table is
generated from it**, and `tests/test_safety_invariants.py` fails if any module
or test named here does not exist.

---

## The finding that shapes this list

Nearly every one of these was **already enforced and already tested before
L62**, scattered across thirty-six test files under names nobody could
enumerate. What did not exist was any way to ask *"are they all still true?"*
and get an answer. So this is a registry, not a new enforcement layer: delete
`invariants.py` and not one refusal in the platform changes.

Writing it caught its own first mistake. Thirteen of the twenty-three evidence
pointers were wrong on the first run -- eleven named real tests in the wrong
file, two named nothing at all. They were written from memory and greps, which
is exactly how a registry becomes a list of confident sentences. The resolver
test is the reason they are now right.

## The two that are NOT_APPLICABLE, and why that matters

It would have been easy to mark INV-24 and INV-25 ENFORCED. The components they
constrain do not exist, so nothing can violate them and every test would pass.

**A test that cannot fail is not evidence.** An invariant that is vacuously
true is not a safety property, and a green row for one is worse than no row,
because it spends credibility the platform has not earned.

| # | Invariant | Severity | Status | Enforced by | Evidence |
|---|---|---|---|---|---|
| INV-01 | The RiskEngine is the final risk veto. | CRITICAL | ENFORCED | `app.execution.pipeline` | `test_a_signal_cannot_reach_a_venue_without_passing_risk` |
| INV-02 | No AI component can bypass the RiskEngine. | CRITICAL | ENFORCED | `app.ai.decision` | `test_the_ai_cannot_overturn_a_risk_veto` |
| INV-03 | The portfolio decision layer cannot submit broker orders. | CRITICAL | ENFORCED | `app.portfolio.decision` | `test_nothing_here_reaches_risk_the_oms_or_a_venue` |
| INV-04 | The portfolio control layer cannot bypass the RiskEngine. | CRITICAL | ENFORCED | `app.portfolio.control` | `test_nothing_here_reaches_risk_the_oms_or_a_venue` |
| INV-05 | Optimization cannot increase an approved risk ceiling. | CRITICAL | ENFORCED | `app.portfolio.control` | `test_increasing_the_maximum_risk_is_forbidden` |
| INV-06 | No component may silently increase leverage. | CRITICAL | ENFORCED | `app.portfolio.control` | `test_increasing_leverage_is_forbidden` |
| INV-07 | No autonomous action may enable live trading. | CRITICAL | ENFORCED | `app.portfolio.control` | `test_any_loosening_of_a_hard_limit_is_forbidden_whatever_it_is_called` |
| INV-08 | LIVE_TRADING stays false unless enabled through production governance. | CRITICAL | ENFORCED | `app.core.settings` | `test_defaults_fail_closed` |
| INV-09 | TRADING_MODE defaults to paper. | CRITICAL | ENFORCED | `app.core.settings` | `test_the_risk_service_defaults_to_paper_and_not_live` |
| INV-10 | Unknown broker or order state is reconciled before any retry. | CRITICAL | ENFORCED | `app.oms.state` | `test_an_unknown_order_latches_safe_mode_and_is_never_retried` |
| INV-11 | Stale or invalid critical data prevents unsafe new entries. | CRITICAL | ENFORCED | `app.portfolio.decision` | `test_a_missing_required_input_defers_the_decision` |
| INV-12 | One account's state cannot contaminate another's. | CRITICAL | ENFORCED | `app.portfolio.service` | `test_paper_and_live_positions_are_never_pooled` |
| INV-13 | Quarantine restricts new entries without blocking management of open ones. | HIGH | ENFORCED | `app.main` | `test_a_breached_limit_cannot_trap_an_open_position` |
| INV-14 | An approval-required action cannot be auto-applied. | CRITICAL | ENFORCED | `app.safety.verification` | `test_an_approval_required_action_is_never_auto_applied` |
| INV-15 | A forbidden action cannot execute even if the AI recommends it. | CRITICAL | ENFORCED | `app.portfolio.control` | `test_ai_recommending_that_risk_protection_be_disabled_is_rejected` |
| INV-16 | Policy conflicts resolve by the authority hierarchy, not by confidence. | CRITICAL | ENFORCED | `app.portfolio.decision` | `test_no_layer_can_be_relaxed_by_the_one_below_it` |
| INV-17 | Autonomous actions respect cooldowns and frequency limits. | HIGH | ENFORCED | `app.bots.budget` | `test_a_bot_that_crashes_on_start_is_not_restarted_forever` |
| INV-18 | Repeated identical actions cannot become an action storm. | HIGH | ENFORCED | `app.portfolio.control` | `test_a_thrashing_target_stops_being_adjusted_automatically` |
| INV-19 | A control-loop failure fails safe. | CRITICAL | ENFORCED | `app.recovery.safe_mode` | `test_a_missing_table_latches_safe_mode_rather_than_migrating` |
| INV-20 | Recovery from uncertain state enters a safe state before resuming. | CRITICAL | ENFORCED | `app.recovery.manager` | `test_an_unresolved_order_latches_safe_mode_at_startup` |
| INV-21 | An autonomous action is idempotent under resubmission. | HIGH | ENFORCED | `app.safety.verification` | `test_the_same_proposal_twice_is_one_action` |
| INV-22 | Historical replay never uses information from after the decision. | CRITICAL | ENFORCED | `app.datasets.leakage` | `test_the_leakage_check_reports_a_feature_that_reads_forward` |
| INV-23 | Safety controls stay active when AI or model services are unavailable. | CRITICAL | ENFORCED | `app.execution.pipeline` | `test_an_absent_ai_block_says_which_kind_of_absent` |
| INV-24 | The autonomous decision engine cannot expand its own safety envelope. | CRITICAL | **NOT_APPLICABLE** | — | `—` |
| INV-25 | Policy versions are immutable, and rollback is deterministic. | HIGH | **NOT_APPLICABLE** | — | `—` |

## Status meanings

- **ENFORCED** — a real component enforces it and a real test proves it.
- **NOT_APPLICABLE** — the thing it constrains does not exist here. Never
  reported as ENFORCED: "we checked and it holds" and "there was nothing to
  check" are different facts.
- **UNENFORCED** — it applies here and nothing enforces it. **Currently empty**,
  and empty by measurement rather than by nobody having looked:
  `test_nothing_applicable_is_left_unenforced` asserts it.
