"""How many candidates clear by chance. **L58/L59's selection-bias gate.**

L59 step 21: *"Do not repeatedly search until one backtest looks excellent and
then treat it as unbiased evidence."* Step 22: *"best result discovered"* is not
*"robust result."*

The strongest tests here are the ones that reproduce this repository's own
recorded searches. `CLAUDE.md` states the expected-by-chance figure for four of
them, and the gate has to agree — a second set of numbers for one question
would be worse than none.
"""

from __future__ import annotations

import pytest
from app.research.selection import (
    CONVENTIONAL_T,
    assess,
    bonferroni_threshold,
    clearing_p,
    expected_by_chance,
    survives_correction,
)

# ============================ 1. it agrees with the project's own reports


@pytest.mark.parametrize(
    "search,tested,documented",
    [
        ("41 candidates across 10 rule families", 41, 1.0),
        ("128 entry x exit combinations at D1", 128, 3.2),
        ("200 combinations at H4", 200, 4.9),
        ("28 shape candidates", 28, 0.7),
    ],
)
def test_expected_by_chance_matches_the_documented_searches(
    search: str, tested: int, documented: float
) -> None:
    """`CLAUDE.md` states each of these figures. The gate must reproduce them.

    This is what pins the methodology as one-sided: two-sided would double
    every one of these and silently disagree with every report in the
    repository.
    """
    got = expected_by_chance(tested)
    assert got == pytest.approx(documented, abs=0.15), (
        f"{search}: gate says {got:.2f}, CLAUDE.md says {documented}"
    )


def test_the_bonferroni_threshold_matches_the_documented_one() -> None:
    """`CLAUDE.md` cites "the 3.546 Bonferroni threshold" for the D1 exit
    search, which ran 128 combinations."""
    assert bonferroni_threshold(128) == pytest.approx(3.546, abs=0.01)


@pytest.mark.parametrize(
    "search,tested,cleared",
    [
        ("41 candidates", 41, 0),
        ("128 at D1", 128, 0),
        ("28 shape candidates", 28, 0),
        ("200 at H4", 200, 5),
    ],
)
def test_every_documented_search_establishes_nothing(
    search: str, tested: int, cleared: int
) -> None:
    """All four reached the conclusion this gate reaches independently.

    The H4 case is the instructive one: **five** candidates cleared 1.96 out of
    sample, which reads as a discovery until you notice chance predicts about
    five over that grid.
    """
    verdict = assess(tested=tested, cleared=cleared)
    assert verdict.at_or_below_chance, f"{search} was not at or below chance"
    assert verdict.verdict == "NOTHING_ESTABLISHED"
    assert "coin flip" in verdict.detail


# ================================================ 2. the arithmetic itself


def test_a_search_that_beats_chance_is_still_not_called_significant() -> None:
    """More than chance is a reason to look further, not a finding.

    The verdict name says so, and the detail names the bar the winner would
    have to clear for the search size.
    """
    verdict = assess(tested=20, cleared=8)
    assert verdict.at_or_below_chance is False
    assert verdict.verdict == "ABOVE_CHANCE_NOT_SIGNIFICANT"
    assert "APPROVED" not in verdict.verdict
    assert "out-of-sample" in verdict.detail


def test_the_bar_rises_with_the_number_of_things_tried() -> None:
    """The whole point of the correction. A t of 2.5 is interesting in one
    pre-registered test and unremarkable as the best of two hundred."""
    one = bonferroni_threshold(1)
    hundred = bonferroni_threshold(100)
    thousand = bonferroni_threshold(1000)
    assert one < hundred < thousand
    assert one == pytest.approx(1.96, abs=0.01)


def test_one_sided_and_two_sided_differ_by_exactly_double() -> None:
    assert clearing_p(CONVENTIONAL_T, one_sided=False) == pytest.approx(
        2 * clearing_p(CONVENTIONAL_T, one_sided=True)
    )
    # And the one-sided figure at 1.96 is the familiar 2.5%.
    assert clearing_p(1.96) == pytest.approx(0.025, abs=0.001)


@pytest.mark.parametrize("tested", [0, -1])
def test_an_empty_search_expects_nothing(tested: int) -> None:
    assert expected_by_chance(tested) == 0.0


def test_impossible_counts_are_refused() -> None:
    """More winners than entrants is a bug in the caller, not a great search."""
    with pytest.raises(ValueError, match="not possible"):
        assess(tested=10, cleared=11)
    with pytest.raises(ValueError, match="negative"):
        assess(tested=-1, cleared=0)


# ================================ 3. clearing the bar is not a verdict


def test_the_documented_false_positive_is_caught_by_out_of_sample_not_by_this() -> None:
    """**The most important test in this file.**

    `donchian_brk_100` scored an in-sample t of 6.59 over 128 combinations. It
    cleared the 3.546 threshold AND a permutation null that reached 5.5 — and
    then posted -1.82 out of sample at -204 points.

    So `survives_correction` returning True must not read as a finding, and the
    reason it returns says exactly that.
    """
    ok, reason = survives_correction(tested=128, best_statistic=6.59)
    assert ok is True
    assert "in-sample bar only" in reason
    assert "went negative out of it" in reason


def test_the_best_of_a_big_search_usually_does_not_clear_its_own_bar() -> None:
    """A t of 2.2 is publishable alone and meaningless as the best of 200."""
    ok, reason = survives_correction(tested=200, best_statistic=2.2)
    assert ok is False
    assert "the best of the search, not a result" in reason


def test_nothing_here_approves_anything() -> None:
    """The module's central claim. It computes and returns; it grants nothing.

    Read from the syntax tree rather than the text, because the docstrings
    discuss approval at length — the mistake the L45 C-4 guard made and the
    L57 lifecycle test repeated.
    """
    import ast
    import inspect

    from app.research import selection

    tree = ast.parse(inspect.getsource(selection))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("approve", "deploy", "promote", "execute", "commit", "set_limits"):
        assert forbidden not in called, f"the selection gate calls {forbidden}()"

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    reaching = [m for m in imported if m.startswith("app.")]
    assert reaching == [], f"the selection gate reaches into the platform: {reaching}"


def test_no_verdict_is_an_approval() -> None:
    """Three verdicts exist and none of them permits anything."""
    verdicts = {assess(tested=n, cleared=c).verdict for n, c in [(10, 0), (10, 5), (1, 1)]}
    for verdict in verdicts:
        assert "APPROV" not in verdict
        assert "DEPLOY" not in verdict
