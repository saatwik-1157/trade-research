"""The twenty-five safety invariants, named and bound to evidence. **L62.**

L62 asks for "a formal set of non-negotiable system invariants". Nearly all of
them were already enforced before this level and already tested -- scattered
across thirty-six test files, under names nobody could enumerate. What did not
exist was any way to ask *"are they all still true?"* and get an answer.

So this is a REGISTRY, not a second enforcement layer. Each invariant names the
module that actually enforces it and the test that actually proves it. Nothing
here does any enforcing; if this file were deleted, not one refusal in the
platform would change.

**The status field is the honest part.** Three of the twenty-five are
`NOT_APPLICABLE`, and it would have been easy to mark them ENFORCED: the
components they constrain -- a decision engine, a control policy, a policy
version -- do not exist in this repository, so nothing can violate them and
every test of them would pass. A test that cannot fail is not evidence, and an
invariant that is vacuously true is not a safety property. `tests/
test_safety_invariants.py` checks that every module and test named here exists,
so a claim of enforcement cannot outlive the thing it names.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InvariantStatus(StrEnum):
    """Whether this invariant is actually holding anything up."""

    #: A real component enforces it and a real test proves it.
    ENFORCED = "ENFORCED"
    #: The thing it constrains does not exist here, so it is vacuously true.
    #: Never to be reported as ENFORCED: the distinction between "we checked
    #: and it holds" and "there was nothing to check" is the whole point.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: It matters, the thing it constrains exists, and nothing enforces it.
    UNENFORCED = "UNENFORCED"


class Severity(StrEnum):
    """What a violation would cost. CRITICAL revokes certification outright."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


@dataclass(frozen=True)
class Invariant:
    """One non-negotiable property, and where to go to check it."""

    id: str
    statement: str
    severity: Severity
    status: InvariantStatus
    #: Dotted module path that enforces it. Empty only when NOT_APPLICABLE.
    enforced_by: str
    #: `tests/<file>::<test>` that proves it. Empty only when NOT_APPLICABLE.
    evidence: str
    #: Why the status is what it is. Required for anything not ENFORCED,
    #: because "we did not do this" needs a reason far more than "we did".
    note: str = ""


C = Severity.CRITICAL
H = Severity.HIGH
M = Severity.MEDIUM
E = InvariantStatus.ENFORCED
NA = InvariantStatus.NOT_APPLICABLE


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        "INV-01",
        "The RiskEngine is the final risk veto.",
        C,
        E,
        "app.execution.pipeline",
        "tests/test_execution.py::test_a_signal_cannot_reach_a_venue_without_passing_risk",
        "Stage 8 of the nine-gate pipeline. Every path to a venue runs it.",
    ),
    Invariant(
        "INV-02",
        "No AI component can bypass the RiskEngine.",
        C,
        E,
        "app.ai.decision",
        "tests/test_paper.py::test_the_ai_cannot_overturn_a_risk_veto",
        "Enforced by TYPE, which is stronger than by check: `AiVerdict` has no "
        "field by which a model could raise a limit, approve an order, size a "
        "position or disengage a kill switch. A richer model later still "
        "returns this type, and this type cannot express an approval.",
    ),
    Invariant(
        "INV-03",
        "The portfolio decision layer cannot submit broker orders.",
        C,
        E,
        "app.portfolio.decision",
        "tests/test_portfolio_decision.py::test_nothing_here_reaches_risk_the_oms_or_a_venue",
        "A syntax-tree test: the module imports nothing from `app.` at all. "
        "Note this is the L60 decision HELPER, not the `PortfolioDecisionEngine` "
        "of the L62 brief, which does not exist -- see INV-24.",
    ),
    Invariant(
        "INV-04",
        "The portfolio control layer cannot bypass the RiskEngine.",
        C,
        E,
        "app.portfolio.control",
        "tests/test_portfolio_control.py::test_nothing_here_reaches_risk_the_oms_or_a_venue",
        "Same syntax-tree guard. It classifies a proposal and returns the "
        "classification; it applies nothing.",
    ),
    Invariant(
        "INV-05",
        "Optimization cannot increase an approved risk ceiling.",
        C,
        E,
        "app.portfolio.control",
        "tests/test_portfolio_control.py::test_increasing_the_maximum_risk_is_forbidden",
        "Classified by effect rather than by name, so a rename does not open a "
        "hole. FORBIDDEN has no approval path.",
    ),
    Invariant(
        "INV-06",
        "No component may silently increase leverage.",
        C,
        E,
        "app.portfolio.control",
        "tests/test_portfolio_control.py::test_increasing_leverage_is_forbidden",
        "",
    ),
    Invariant(
        "INV-07",
        "No autonomous action may enable live trading.",
        C,
        E,
        "app.portfolio.control",
        "tests/test_portfolio_control.py::"
        "test_any_loosening_of_a_hard_limit_is_forbidden_whatever_it_is_called",
        "`live_trading` is a hard limit, and loosening one is FORBIDDEN "
        "whatever the action is called.",
    ),
    Invariant(
        "INV-08",
        "LIVE_TRADING stays false unless enabled through production governance.",
        C,
        E,
        "app.core.settings",
        "tests/test_settings.py::test_defaults_fail_closed",
        "",
    ),
    Invariant(
        "INV-09",
        "TRADING_MODE defaults to paper.",
        C,
        E,
        "app.core.settings",
        "tests/test_autonomy.py::test_the_risk_service_defaults_to_paper_and_not_live",
        "",
    ),
    Invariant(
        "INV-10",
        "Unknown broker or order state is reconciled before any retry.",
        C,
        E,
        "app.oms.state",
        "tests/test_recovery.py::test_an_unknown_order_latches_safe_mode_and_is_never_retried",
        "`SAFE_TO_RESEND` is `{failed}` and nothing else. An IPC timeout after "
        "order_send looks exactly like a rejection from this side, so unknown "
        "is settled by asking the venue, never by sending again.",
    ),
    Invariant(
        "INV-11",
        "Stale or invalid critical data prevents unsafe new entries.",
        C,
        E,
        "app.portfolio.decision",
        "tests/test_portfolio_decision.py::test_a_missing_required_input_defers_the_decision",
        "`require_fresh` turns absent evidence into a HARD_SAFETY finding "
        "rather than a warning. A warning that does not change the verdict is "
        "not a safety control.",
    ),
    Invariant(
        "INV-12",
        "One account's state cannot contaminate another's.",
        C,
        E,
        "app.portfolio.service",
        "tests/test_portfolio.py::test_paper_and_live_positions_are_never_pooled",
        "Read one account at a time; paper and broker are never pooled into a figure.",
    ),
    Invariant(
        "INV-13",
        "Quarantine restricts new entries without blocking management of open ones.",
        H,
        E,
        "app.main",
        "tests/test_positions.py::test_a_breached_limit_cannot_trap_an_open_position",
        "The distinction that matters: a restriction that also blocked exits "
        "would turn a safety control into a way of being unable to get out.",
    ),
    Invariant(
        "INV-14",
        "An approval-required action cannot be auto-applied.",
        C,
        E,
        "app.safety.verification",
        "tests/test_policy_verification.py::test_an_approval_required_action_is_never_auto_applied",
        "",
    ),
    Invariant(
        "INV-15",
        "A forbidden action cannot execute even if the AI recommends it.",
        C,
        E,
        "app.portfolio.control",
        "tests/test_portfolio_control.py::"
        "test_ai_recommending_that_risk_protection_be_disabled_is_rejected",
        "It does not matter who proposed it.",
    ),
    Invariant(
        "INV-16",
        "Policy conflicts resolve by the authority hierarchy, not by confidence.",
        C,
        E,
        "app.portfolio.decision",
        "tests/test_portfolio_decision.py::test_no_layer_can_be_relaxed_by_the_one_below_it",
        "`decide()` takes the most severe verdict present, so an ALLOW from "
        "OPTIMIZATION cannot displace a RESTRICT from RISK. Enforced by "
        "construction: the function cannot express the other outcome.",
    ),
    Invariant(
        "INV-17",
        "Autonomous actions respect cooldowns and frequency limits.",
        H,
        E,
        "app.bots.budget",
        "tests/test_bots.py::test_a_bot_that_crashes_on_start_is_not_restarted_forever",
        "",
    ),
    Invariant(
        "INV-18",
        "Repeated identical actions cannot become an action storm.",
        H,
        E,
        "app.portfolio.control",
        "tests/test_portfolio_control.py::test_a_thrashing_target_stops_being_adjusted_automatically",
        "Three direction changes on one target in an hour drops it to "
        "REQUIRES_APPROVAL -- and never blocks an emergency stop, or "
        "instability would suppress the response to instability.",
    ),
    Invariant(
        "INV-19",
        "A control-loop failure fails safe.",
        C,
        E,
        "app.recovery.safe_mode",
        "tests/test_recovery.py::test_a_missing_table_latches_safe_mode_rather_than_migrating",
        "Safe mode is a latched SECOND refusal, not a replacement for risk.",
    ),
    Invariant(
        "INV-20",
        "Recovery from uncertain state enters a safe state before resuming.",
        C,
        E,
        "app.recovery.manager",
        "tests/test_integration.py::test_an_unresolved_order_latches_safe_mode_at_startup",
        "Sixteen-step startup sequence; release re-runs it and refuses while "
        "the condition still holds.",
    ),
    Invariant(
        "INV-21",
        "An autonomous action is idempotent under resubmission.",
        H,
        E,
        "app.safety.verification",
        "tests/test_policy_verification.py::test_the_same_proposal_twice_is_one_action",
        "Fingerprinted over the fields that make an action the same action. "
        "The database's UNIQUE on `orders.intent_id` remains the backstop for "
        "orders themselves.",
    ),
    Invariant(
        "INV-22",
        "Historical replay never uses information from after the decision.",
        C,
        E,
        "app.datasets.leakage",
        "tests/test_datasets.py::test_the_leakage_check_reports_a_feature_that_reads_forward",
        "The leakage checker predates this level and is the strongest "
        "reproducibility control in the repository.",
    ),
    Invariant(
        "INV-23",
        "Safety controls stay active when AI or model services are unavailable.",
        C,
        E,
        "app.execution.pipeline",
        "tests/test_trade_journal.py::test_an_absent_ai_block_says_which_kind_of_absent",
        "The AI gate can only subtract. An absent model removes a veto, never "
        "adds an approval, so unavailability is strictly more conservative.",
    ),
    Invariant(
        "INV-24",
        "The autonomous decision engine cannot expand its own safety envelope.",
        C,
        NA,
        "",
        "",
        "**There is no autonomous decision engine.** The L62 brief opens by "
        "describing an OBSERVATION -> STATE ESTIMATION -> "
        "PortfolioDecisionEngine -> ... -> Outcome Measurement loop as having "
        "been built at L61. It was not. L61 built the safety half only, and "
        "MIGRATION_STATUS records that the measurement half was declined "
        "because no autonomous action has ever been applied here, so there "
        "were no outcomes to measure. `PortfolioDecisionEngine`, "
        "`PortfolioControlPolicy`, `PortfolioControlState`, "
        "`PortfolioStateEstimator`, `PortfolioRiskOrchestrator` and "
        "`PortfolioDecisionContext` do not exist. Marking this ENFORCED would "
        "be certifying a boundary around an empty room.",
    ),
    Invariant(
        "INV-25",
        "Policy versions are immutable, and rollback is deterministic.",
        H,
        NA,
        "",
        "",
        "**There is no policy object to version.** `app.portfolio.control` and "
        "`app.portfolio.decision` are pure functions over module constants; "
        "there is no stored, mutable policy that a version could pin or a "
        "rollback restore. Model versioning and rollback DO exist "
        "(`app.ai.registry_service`) and are real -- but they version models, "
        "not policy, and reporting one as the other would be the substitution "
        "this registry exists to prevent.",
    ),
    Invariant(
        "INV-26",
        "Autonomy never exceeds what the current certification permits.",
        C,
        E,
        "app.safety.autonomy",
        "tests/test_autonomy_governance.py::test_nothing_below_bounded_is_ever_auto_applied",
        "Enforced where the ACTION is rather than where the certificate is: "
        "`PolicyVerificationEngine` carries an autonomy level and refuses to "
        "mark anything auto-appliable below BOUNDED. `ceiling_for` maps an "
        "unrecognised certification state to OBSERVE, so a state nobody "
        "mapped fails closed rather than inheriting the last good value.",
    ),
    Invariant(
        "INV-27",
        "No autonomous action may modify the governance machinery.",
        C,
        E,
        "app.safety.autonomy",
        "tests/test_autonomy_governance.py::test_the_system_cannot_raise_its_own_autonomy",
        "**L63 found this open.** Before `GOVERNANCE_TARGETS`, an action "
        "targeting `autonomy_level`, `certification_state`, `audit_logging` or "
        "`monitoring` classified as REQUIRES_APPROVAL rather than FORBIDDEN -- "
        "an approval path by which the system could raise its own autonomy or "
        "switch off its own audit log. Section 15 requires a wall and "
        "REQUIRES_APPROVAL is a door. Fixed by putting the governance targets "
        "inside the set L61 already refuses outright, exactly as L62 did for "
        "the safety envelope.",
    ),
    Invariant(
        "INV-28",
        "Hard safety is not researchable, not only unmodifiable.",
        C,
        E,
        "app.safety.policy_research",
        "tests/test_policy_research.py::test_anything_unrecognised_is_hard_safety",
        "**The polarity is the property.** L62 and L63 protect actions with "
        "blocklists, which is right there because an action's targets are known "
        "in advance. Research inverts it: a parameter is Category A unless it "
        "appears in `RESEARCHABLE` with a category and explicit bounds, so a "
        "safety parameter added later is immutable by default rather than "
        "researchable by nobody's decision. Category A has no approval path -- "
        "it is not reviewable, not proposable and not optimizable, which is "
        "stronger than requiring sign-off.",
    ),
    Invariant(
        "INV-29",
        "A longer decision horizon never displaces a shorter one on safety.",
        C,
        E,
        "app.portfolio.horizon",
        "tests/test_multi_horizon.py::test_no_non_safety_horizon_carries_a_safety_layer",
        "**Horizon is not a second ordering.** `Horizon` answers *when* and "
        "`Layer` answers *who says so*; they are orthogonal, so they compose "
        "rather than compete. `synthesise()` maps each horizon onto a layer and "
        "delegates the whole resolution to L60's `decide()`, which already "
        "cannot express a lower layer relaxing a higher one. Two independent "
        "orderings resolving one conflict would be the failure this repository "
        "keeps naming -- the one that disagreed silently would be the one "
        "nobody read. EMERGENCY and IMMEDIATE_RISK own HARD_SAFETY and RISK, "
        "and no other horizon may borrow either.",
    ),
    Invariant(
        "INV-30",
        "A prediction may tighten a decision and may never loosen one.",
        C,
        E,
        "app.portfolio.scenario",
        "tests/test_scenario_intelligence.py::test_no_scenario_input_can_ever_lower_the_verdict",
        "**The asymmetry is the cost function, not caution.** A scenario that "
        "wrongly predicts danger costs a missed opportunity; one that wrongly "
        "predicts safety costs the portfolio. Those are not the same mistake, "
        "so they do not get the same authority. `gate()` returns "
        "`max(current, ...)`, so there is no input -- however confident, "
        "however favourable -- that makes the answer more permissive than what "
        "the platform already held. L61's rule applied to forecasting, and it "
        "makes section 47 a property of the return value rather than a policy.",
    ),
    Invariant(
        "INV-31",
        "Stress coverage is earned by a completed run and is never assumed.",
        C,
        E,
        "app.portfolio.stress",
        "tests/test_stress_orchestration.py::test_nothing_makes_an_unrun_stress_class_covered",
        "**The only POSITIVE claim in the safety stack.** Every other artefact "
        "here refuses something; a coverage matrix asserts that the portfolio "
        "has been tested against a condition, and a positive claim is the kind "
        "that gets quoted. So there is no path from absence to COVERED: "
        "`is_covered` is true for one value of five, a failed run covers "
        "nothing, another account's run covers nothing, and a stale result is "
        "STALE rather than COVERED. Read against this deployment the matrix "
        "returns 0 of 16 and the resilience verdict is UNKNOWN, which blocks "
        "autonomy -- the correct answer, and the useful one.",
    ),
)


def by_id(invariant_id: str) -> Invariant:
    for inv in INVARIANTS:
        if inv.id == invariant_id:
            return inv
    raise KeyError(f"no invariant {invariant_id!r}")


def enforced() -> tuple[Invariant, ...]:
    return tuple(i for i in INVARIANTS if i.status is InvariantStatus.ENFORCED)


def unenforced() -> tuple[Invariant, ...]:
    """Invariants that matter, apply here, and nothing enforces.

    Empty today. It is a function rather than a constant so that it stays
    empty by measurement rather than by nobody having looked.
    """
    return tuple(i for i in INVARIANTS if i.status is InvariantStatus.UNENFORCED)


def not_applicable() -> tuple[Invariant, ...]:
    return tuple(i for i in INVARIANTS if i.status is InvariantStatus.NOT_APPLICABLE)


__all__ = [
    "INVARIANTS",
    "Invariant",
    "InvariantStatus",
    "Severity",
    "by_id",
    "enforced",
    "not_applicable",
    "unenforced",
]
