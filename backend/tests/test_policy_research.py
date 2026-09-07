"""Researching policy without being able to change it. **L64 §40 and §41.**

Section 40 lists fifteen mandatory safety tests and section 41 one critical
risk test. They are the first sections here, in order.

**Most of them pass for one reason**, and it is worth stating before the tests
rather than after: research operates on an allow-list. A parameter is Category
A -- immutable, not even researchable -- unless it appears in `RESEARCHABLE`
with a category and explicit bounds. So `max_portfolio_risk`,
`risk_engine_enabled` and `live_trading` are refused not because somebody
remembered to forbid them, but because nobody authorised them. A test proves
that with names invented on the spot.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from decimal import Decimal

import pytest
from app.safety.policy_research import (
    RESEARCHABLE,
    Candidate,
    Category,
    Hypothesis,
    PromotionState,
    Score,
    Verdict,
    better,
    categorise,
    check_promotion,
    diff,
    validate,
)


def candidate(**changes: Decimal) -> Candidate:
    return Candidate("cand-1", "pv-baseline", dict(changes))


# ============================================ §41: the critical risk test


def test_no_candidate_can_turn_10000_of_approved_risk_into_15000() -> None:
    """**Section 41, mandatory.**

    "No optimizer, research engine, AI model, policy engine, or portfolio
    controller may convert $10,000 approved risk into $15,000 approved risk."

    It is refused at the earliest possible point -- static validation, before
    any simulation runs -- because a capital ceiling is Category A and Category
    A has no approval path. Not "rejected after review": not reviewable.
    """
    v = validate(candidate(max_capital_movement=Decimal("15000")))
    assert v.verdict is Verdict.REJECTED_HARD_SAFETY
    assert not v.accepted
    assert "no approval path" in " ".join(v.violations)


# ================================================ §40: the mandatory fifteen


@pytest.mark.parametrize(
    "target",
    [
        "max_portfolio_risk",
        "max_capital_movement",
        "max_exposure_change",
        "risk_budget",
    ],
)
def test_a_candidate_that_raises_maximum_risk_is_rejected(target: str) -> None:
    """**TEST A.**"""
    assert validate(candidate(**{target: Decimal("999")})).verdict is (Verdict.REJECTED_HARD_SAFETY)


def test_a_candidate_that_raises_leverage_is_rejected() -> None:
    """**TEST B.**"""
    assert validate(candidate(max_leverage=Decimal("50"))).verdict is (Verdict.REJECTED_HARD_SAFETY)


def test_a_candidate_that_disables_the_risk_engine_is_rejected() -> None:
    """**TEST C.**"""
    for target in ("risk_engine_enabled", "risk_engine", "risk_veto"):
        assert validate(candidate(**{target: Decimal("0")})).verdict is (
            Verdict.REJECTED_HARD_SAFETY
        ), target


def test_a_candidate_that_bypasses_certification_is_rejected() -> None:
    """**TEST D.**"""
    for target in ("certification_state", "certification_expiry", "autonomy_level"):
        assert validate(candidate(**{target: Decimal("1")})).verdict is (
            Verdict.REJECTED_HARD_SAFETY
        ), target


def test_an_unsafe_generated_policy_is_refused_without_being_run() -> None:
    """**TEST E.** AI generates an unsafe policy.

    There is no sandbox because there is nothing to sandbox. A candidate is a
    mapping of name to number; the module has no interpreter, so an unsafe
    candidate is refused as *data* and never becomes behaviour. Section 9's
    list of things a sandbox must prevent -- broker access, filesystem abuse,
    secret access, arbitrary network access -- are all unreachable from a
    dictionary of decimals.
    """
    hostile = candidate(
        live_trading=Decimal("1"),
        audit_logging=Decimal("0"),
        monitoring=Decimal("0"),
    )
    v = validate(hostile)
    assert v.verdict is Verdict.REJECTED_HARD_SAFETY
    assert set(v.touched) == {"audit_logging", "live_trading", "monitoring"}
    for name in v.touched:
        assert v.categories[name] == Category.HARD_SAFETY.value


def test_a_candidate_strong_in_sample_and_weak_out_of_sample_loses() -> None:
    """**TEST F.** Overfitting.

    Delegated to `app.research.selection`, the L58/L59 multiple-testing gate,
    which was validated against this repository's OWN recorded searches --
    `CLAUDE.md` reports 41 candidates clearing ~1 by chance, 128 clearing 3.2,
    200 clearing 4.9, and the gate reproduces all three. Rebuilding it here
    would have been a second implementation of the one piece of research
    machinery this project has already proven against real data.
    """
    from app.research.selection import assess

    # A search over 200 candidates that produced 5 clearing the conventional
    # bar: below what chance alone predicts.
    result = assess(tested=200, cleared=5)
    assert result.verdict == "NOTHING_ESTABLISHED"
    assert result.expected > 4, "200 candidates clear ~5 by chance alone"

    # And the ordering refuses a challenger that wins on anything but safety.
    unsafe_but_fast = Score(safety=1, robustness=9, stability=9, complexity=1)
    safe_baseline = Score(safety=3, robustness=2, stability=2, complexity=1)
    ok, why = better(unsafe_but_fast, safe_baseline)
    assert not ok
    assert "compared first and alone" in why


def test_a_candidate_identical_to_one_already_assessed_is_a_duplicate() -> None:
    """**TEST G.** And the fingerprint ignores what does not matter."""
    first = Candidate("a", "pv-1", {"oscillation_limit": Decimal("4")})
    seen = {first.fingerprint(): "a"}

    same_policy_different_id = Candidate(
        "b", "pv-1", {"oscillation_limit": Decimal("4")}, rationale="worded differently"
    )
    v = validate(same_policy_different_id, seen=seen)
    assert v.verdict is Verdict.REJECTED_DUPLICATE
    assert "another draw from the same urn" in " ".join(v.violations)

    # A different parent policy is a different candidate: the same numbers
    # under different rules have not actually been assessed.
    other_parent = Candidate("c", "pv-2", {"oscillation_limit": Decimal("4")})
    assert validate(other_parent, seen=seen).verdict is Verdict.ACCEPTED


def test_a_candidate_that_would_destabilise_the_control_loop_is_out_of_bounds() -> None:
    """**TEST H and TEST I.** Instability and excessive action frequency.

    Both are the same shape: a value outside the range somebody approved. The
    bounds are what make "excessive" a measured word rather than a judgement
    made after the fact.
    """
    assert validate(candidate(oscillation_limit=Decimal("100"))).verdict is (
        Verdict.REJECTED_OUT_OF_BOUNDS
    )
    assert validate(candidate(oscillation_window_hours=Decimal("0.01"))).verdict is (
        Verdict.REJECTED_OUT_OF_BOUNDS
    )
    # And the envelope's own action-frequency bound is not researchable at all.
    assert validate(candidate(max_actions_per_window=Decimal("1000"))).verdict is (
        Verdict.REJECTED_HARD_SAFETY
    )


def test_passing_research_does_not_make_a_candidate_certified() -> None:
    """**TEST J.** Research is one step of many, and cannot skip the rest."""
    assert check_promotion(PromotionState.SHADOW, PromotionState.CERTIFIED) is not None
    assert check_promotion(PromotionState.PROMISING, PromotionState.ACTIVE) is not None
    assert check_promotion(PromotionState.BACKTESTED, PromotionState.APPROVED) is not None


def test_nothing_becomes_active_without_a_canary() -> None:
    """**TEST K's precondition**, asserted over the whole table.

    Section 21 says no candidate may jump to ACTIVE, and the way that rule gets
    broken is a transition table where the edge quietly exists.
    """
    from app.safety.policy_research import TRANSITIONS

    for frm, tos in TRANSITIONS.items():
        if PromotionState.ACTIVE in tos:
            assert frm is PromotionState.CANARY, f"{frm} can reach ACTIVE without a canary"


def test_a_failing_canary_rolls_back_and_a_rollback_is_terminal() -> None:
    """**TEST K.** And rollback does not loop back into deployment."""
    assert check_promotion(PromotionState.CANARY, PromotionState.ROLLED_BACK) is None
    assert check_promotion(PromotionState.ACTIVE, PromotionState.ROLLED_BACK) is None
    # A rolled-back policy is retired, not re-promoted.
    assert check_promotion(PromotionState.ROLLED_BACK, PromotionState.CANARY) is not None
    assert check_promotion(PromotionState.ROLLED_BACK, PromotionState.ACTIVE) is not None


def test_any_state_can_be_abandoned() -> None:
    """**TEST L and TEST M.** Rollback target unavailable; database lost.

    Both reduce to the same requirement: whatever goes wrong, there is always a
    move to a state that deploys nothing. REJECTED and RETIRED are reachable
    from everywhere, so no candidate can be stranded in a state that only
    promotes forward.
    """
    for state in PromotionState:
        if state is PromotionState.RETIRED:
            continue
        assert check_promotion(state, PromotionState.RETIRED) is None, state
        assert check_promotion(state, PromotionState.REJECTED) is None, state


def test_research_being_unavailable_leaves_the_certified_policy_authoritative() -> None:
    """**TEST N.** AI unavailable.

    Nothing in the platform reads this module, so research stopping changes
    nothing about what the platform does -- which is the strongest possible
    form of "existing certified policy remains authoritative". Asserted rather
    than asserted-about: no `app.` module imports `policy_research`.

    Walks the syntax tree rather than grepping the text, and the first version
    of this test grepped. It failed the moment the invariant registry NAMED
    this module in an `enforced_by` string -- a reference, not a dependency.
    Same lesson as C-4 and the L57 lifecycle guard: match the structure, never
    the characters.
    """
    root = pathlib.Path(inspect.getfile(validate)).resolve().parents[2]
    importers = []
    for f in sorted((root / "app").rglob("*.py")):
        if f.name in ("policy_research.py", "__init__.py") or "__pycache__" in f.parts:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = next((a.name for a in node.names), "")
            else:
                continue
            if "policy_research" in mod:
                importers.append(f"{f.name}:{node.lineno}")
    assert importers == [], f"policy research reaches production code via {importers}"


def test_a_candidate_cannot_reach_a_venue_because_it_is_not_code() -> None:
    """**TEST O.** Candidate attempts direct MT5 access.

    It cannot express the attempt. `Candidate.changes` is `dict[str, Decimal]`;
    there is no field that could hold a call, and no interpreter that would run
    one. This asserts the module itself has no execution machinery, which is
    what makes the claim structural rather than a promise.
    """
    from app.safety import policy_research

    tree = ast.parse(inspect.getsource(policy_research))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for dangerous in ("eval", "exec", "compile", "__import__", "open", "globals", "locals"):
        assert dangerous not in called, f"policy research calls {dangerous}()"

    attr_called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for dangerous in (
        "place_order",
        "close_position",
        "cancel_order",
        "modify_order",
        "system",
        "popen",
        "run",
        "loads",
    ):
        assert dangerous not in attr_called, f"policy research calls {dangerous}()"


# ================================= the allow-list, which is why most of the above works


@pytest.mark.parametrize(
    "invented",
    [
        "max_portfolio_risk",
        "a_parameter_nobody_thought_of",
        "innocuous_sounding_knob",
        "risk_ceiling_v2",
        "",
    ],
)
def test_anything_unrecognised_is_hard_safety(invented: str) -> None:
    """**The design decision the whole level rests on.**

    L62 and L63 used blocklists, which is right for actions: an action's
    targets are known in advance. It is exactly wrong for research, because a
    blocklist means *everything is researchable unless forbidden* -- so a
    safety parameter added next month would be researchable by default, by
    nobody's decision.

    Two of these names were invented for this test and match no list anywhere.
    They are refused because nobody authorised them.
    """
    assert categorise(invented) is Category.HARD_SAFETY
    if invented:
        assert validate(candidate(**{invented: Decimal("1")})).verdict is (
            Verdict.REJECTED_HARD_SAFETY
        )


def test_the_allow_list_is_small_and_every_entry_explains_itself() -> None:
    """A generous allow-list is how a hard limit becomes researchable."""
    assert len(RESEARCHABLE) <= 12, "the allow-list is growing; each entry needs a reason"
    for entry in RESEARCHABLE:
        assert entry.low < entry.high, entry.name
        assert len(entry.why) > 40, f"{entry.name} does not explain itself"
        assert entry.category is not Category.HARD_SAFETY


def test_no_researchable_parameter_is_also_a_hard_limit() -> None:
    """The two lists must not overlap, or the polarity would be ambiguous."""
    from app.safety.autonomy import GOVERNANCE_TARGETS
    from app.safety.envelope import ENVELOPE_TARGETS

    for entry in RESEARCHABLE:
        assert entry.name not in ENVELOPE_TARGETS, entry.name
        assert entry.name not in GOVERNANCE_TARGETS, entry.name


# ========================================================= scoring and selection


def test_safety_is_compared_first_and_alone() -> None:
    """Section 18. **Not a weighted sum**: a weight is a price, and with a big
    enough gain elsewhere the sum tips. Lexicographic ordering has no price."""
    worse_safety = Score(safety=1, robustness=100, stability=100, complexity=0)
    better_safety = Score(safety=2, robustness=0, stability=0, complexity=50)
    assert better_safety.beats(worse_safety)
    assert not worse_safety.beats(better_safety)


def test_a_tie_goes_to_the_incumbent() -> None:
    """Section 20: never replace the baseline solely because a challenger
    scored well. An incumbent has evidence from running that a candidate does
    not."""
    same = Score(safety=3, robustness=3, stability=3, complexity=2)
    ok, why = better(same, Score(safety=3, robustness=3, stability=3, complexity=2))
    assert not ok
    assert "tie goes to the incumbent" in why


def test_the_simpler_of_two_equal_policies_wins() -> None:
    """Section 19's complexity penalty, as a tie-break rather than a term."""
    simple = Score(safety=3, robustness=3, stability=3, complexity=1)
    baroque = Score(safety=3, robustness=3, stability=3, complexity=9)
    assert simple.beats(baroque)


def test_complexity_is_measured_not_asserted() -> None:
    assert candidate(oscillation_limit=Decimal("3")).complexity() == 1
    assert (
        candidate(
            oscillation_limit=Decimal("3"),
            stability_preference=Decimal("0.5"),
            regime_preference=Decimal("0.5"),
        ).complexity()
        == 3
    )


# ================================================================ diff and hypothesis


def test_a_diff_flags_a_hard_safety_change_rather_than_reporting_it() -> None:
    """Section 12: hard safety changes must be explicitly flagged and rejected."""
    d = diff(
        {"oscillation_limit": Decimal("3")},
        candidate(oscillation_limit=Decimal("4"), max_leverage=Decimal("30")),
    )
    assert d.hard_safety_touched == ("max_leverage",)
    assert not d.safe_to_review
    assert d.changed["oscillation_limit"] == (Decimal("3"), Decimal("4"))


def test_a_clean_diff_is_reviewable() -> None:
    d = diff({"oscillation_limit": Decimal("3")}, candidate(oscillation_limit=Decimal("5")))
    assert d.safe_to_review
    assert d.hard_safety_touched == ()


def test_a_hypothesis_carries_its_category() -> None:
    """Section 7's example pair: one acceptable, one automatically rejected."""
    fine = Hypothesis(
        "h1",
        "Reducing adaptive allocation frequency may decrease churn",
        "the control loop reverses direction more than the conditions do",
        "oscillation_limit",
        "pv-baseline",
        "less churn, similar responsiveness",
        "replay, walk-forward, shadow",
        "restore the previous value",
    )
    assert fine.as_dict()["category"] == Category.GOVERNANCE.value

    forbidden = Hypothesis(
        "h2",
        "Increase maximum portfolio risk to improve returns",
        "returns are low",
        "max_portfolio_risk",
        "pv-baseline",
        "higher returns",
        "-",
        "-",
    )
    assert forbidden.as_dict()["category"] == Category.HARD_SAFETY.value


def test_the_validation_record_says_it_approved_nothing() -> None:
    record = validate(candidate(oscillation_limit=Decimal("4"))).as_dict()
    assert "has not been approved, certified or deployed" in record["authority"]
    assert "RiskEngine remains the final veto" in record["authority"]
