"""How many candidates clear by chance. **L58/L59's selection-bias gate.**

L59 step 21 states the rule: *"Do not repeatedly search until one backtest
looks excellent and then treat it as unbiased evidence."* Step 22 names the
distinction: *"best result discovered"* is not *"robust result."*

**This repository has the receipts.** `CLAUDE.md` records search after search
where the arithmetic here is the only thing that separates a finding from an
artefact:

* 41 candidates across 10 rule families -- zero cleared 1.96 out of sample
  against ~1 expected by chance.
* A 36-cell bracket sweep where the RANDOM rule scored 1.76 in sample against
  the best real candidate's 0.83.
* 128 entry-by-exit combinations at D1 -- zero cleared out of sample against
  3.2 expected, so the search came in BELOW chance.
* 200 combinations at H4 where five cleared against 4.9 expected, and none of
  the five was pickable in advance.

Every one of those is a search that would look like a discovery if you reported
only the winner. **The number that makes them readable is how many winners a
coin flip would have produced over the same grid**, and that is what this
module computes.

**It grants nothing and rejects nothing on its own.** It returns an assessment
a caller records beside a candidate, so a promotion decision is made in front
of the search that produced it rather than in front of one backtest.

The normal approximation comes from `statistics.NormalDist` -- no new
dependency, and the t-distribution converges to it at the sample sizes any of
these searches reach.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

#: The conventional two-sided threshold these reports use throughout.
CONVENTIONAL_T = 1.96

#: Family-wise error rate a Bonferroni correction is computed against.
DEFAULT_FAMILY_ALPHA = 0.05

#: How far above the expected count a search must land to be worth pursuing,
#: in standard deviations of the null's own binomial spread.
#:
#: Two, and the reason is the H4 exit search: it cleared **5** against
#: **4.99958** expected. A strict `cleared > expected` calls that a result;
#: `CLAUDE.md` reads it as nothing, and `CLAUDE.md` is right. The count of
#: winners under the null has its own noise, and a figure inside that noise
#: says nothing whichever side of the mean it falls.
NOTABLE_SD = 2.0

_NORMAL = NormalDist()


def clearing_p(threshold: float, *, one_sided: bool = True) -> float:
    """The chance ONE candidate clears `threshold` on noise alone.

    **One-sided by default, because that is what this repository's reports
    use** and a gate that disagreed with them would produce two sets of
    numbers for one question. Checked against every search `CLAUDE.md`
    records:

    ===========================  ========  ==============
    search                       one-sided  CLAUDE.md says
    ===========================  ========  ==============
    41 candidates                     1.0  "~1 expected"
    128 combinations at D1            3.2  "3.2 expected"
    200 combinations at H4            4.9  "4.9 expected"
    28 shape candidates               0.7  "0.7 expected"
    ===========================  ========  ==============

    One-sided is also the right test for the question actually being asked. A
    strategy that loses significantly is not a discovery to be promoted; only
    the upper tail is a candidate. `CLAUDE.md`'s own significant results in the
    lower tail -- `body_ride_0.6` at t = -4.65, the `random` rule at -3.60 --
    are read as cost drag, not as edges.
    """
    tail = 1.0 - _NORMAL.cdf(abs(threshold))
    return tail if one_sided else 2.0 * tail


def expected_by_chance(
    tested: int, threshold: float = CONVENTIONAL_T, *, one_sided: bool = True
) -> float:
    """How many of `tested` candidates a coin flip clears at `threshold`.

    The number that makes a search readable. Five winners out of two hundred is
    not five findings; it is half of what chance predicts.
    """
    if tested <= 0:
        return 0.0
    return tested * clearing_p(threshold, one_sided=one_sided)


def bonferroni_threshold(tested: int, family_alpha: float = DEFAULT_FAMILY_ALPHA) -> float:
    """The threshold one candidate must clear for the SEARCH to mean something.

    Rises with the number of things tried, which is the whole point: a t of 2.5
    is interesting in one pre-registered test and unremarkable as the best of
    two hundred.

    Conservative by construction — it assumes the tests are independent, and
    correlated candidates make it stricter than it needs to be. That is the
    safe direction for a gate whose failure mode is promoting an artefact.
    """
    if tested <= 1:
        return abs(_NORMAL.inv_cdf(1.0 - family_alpha / 2.0))
    return abs(_NORMAL.inv_cdf(1.0 - (family_alpha / tested) / 2.0))


@dataclass(frozen=True)
class SearchAssessment:
    """What a search of `tested` candidates actually established."""

    tested: int
    cleared: int
    threshold: float
    expected: float
    bonferroni: float
    verdict: str
    detail: str

    #: How far above `expected` a count must sit before it is worth a second
    #: look, measured in standard deviations of the null itself.
    margin_sd: float = 0.0

    @property
    def notable_at(self) -> float:
        """The count this search would have to reach to be worth pursuing."""
        return self.expected + self.margin_sd

    @property
    def at_or_below_chance(self) -> bool:
        """True when the search found no more than a coin flip plausibly would.

        **Not `cleared <= expected`.** That comparison is a hairline, and the
        hairline matters: the H4 exit search cleared 5 against 4.99958
        expected, which a strict inequality calls "above chance" and which
        `CLAUDE.md` correctly reads as nothing at all.

        The count of winners under the null is binomial, so its own spread is
        the natural unit. A search is only notable when it exceeds what chance
        predicts by more than the null's own noise.
        """
        return self.cleared <= self.notable_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates_tested": self.tested,
            "cleared_threshold": self.cleared,
            "threshold": round(self.threshold, 4),
            "expected_by_chance": round(self.expected, 3),
            "bonferroni_threshold": round(self.bonferroni, 4),
            "at_or_below_chance": self.at_or_below_chance,
            "verdict": self.verdict,
            "detail": self.detail,
        }


def assess(*, tested: int, cleared: int, threshold: float = CONVENTIONAL_T) -> SearchAssessment:
    """What a search established, stated so a winner cannot be read alone.

    Three verdicts, and none of them is "approved":

    ``NOTHING_ESTABLISHED``
        The search cleared no more than chance predicts. This is the verdict
        every search in `CLAUDE.md` received, and it is the expected one.

    ``ABOVE_CHANCE_NOT_SIGNIFICANT``
        More cleared than chance predicts, but no single candidate clears the
        threshold the search size demands. Worth another look; not evidence.

    ``SURVIVES_CORRECTION``
        More cleared than chance AND the bar was raised for the search size.
        Still not an approval — it is the point at which out-of-sample and
        walk-forward evidence becomes worth gathering.
    """
    if tested < 0 or cleared < 0:
        raise ValueError("counts cannot be negative")
    if cleared > tested:
        raise ValueError(f"{cleared} cleared out of {tested} tested is not possible")

    expected = expected_by_chance(tested, threshold)
    corrected = bonferroni_threshold(tested)
    # The binomial standard deviation of the winner count under the null.
    # `expected` alone is a hairline; this is the noise around it.
    p = clearing_p(threshold)
    sd = (tested * p * (1.0 - p)) ** 0.5 if tested > 0 else 0.0
    margin = NOTABLE_SD * sd
    notable_at = expected + margin

    if cleared <= notable_at:
        verdict = "NOTHING_ESTABLISHED"
        detail = (
            f"{cleared} of {tested} candidates cleared {threshold:.2f} against "
            f"{expected:.1f} expected by chance (notable above {notable_at:.1f}). The "
            "search found no more than a coin flip would, so the best candidate in it "
            "is the best of some noise"
        )
    else:
        verdict = "ABOVE_CHANCE_NOT_SIGNIFICANT"
        detail = (
            f"{cleared} of {tested} cleared {threshold:.2f} against {expected:.1f} "
            f"expected and {notable_at:.1f} to be notable. Searching {tested} "
            f"candidates raises the bar any one of them "
            f"must clear to {corrected:.3f}; report whether the winner does, and "
            "gather out-of-sample evidence before treating it as a finding"
        )
    return SearchAssessment(
        tested=tested,
        cleared=cleared,
        threshold=threshold,
        expected=expected,
        bonferroni=corrected,
        verdict=verdict,
        detail=detail,
        margin_sd=margin,
    )


def survives_correction(
    *, tested: int, best_statistic: float, family_alpha: float = DEFAULT_FAMILY_ALPHA
) -> tuple[bool, str]:
    """Does the best candidate clear the bar its own search size demands?

    The question that separates "we found something" from "we looked a lot".
    `CLAUDE.md`'s sharpest example: `donchian_brk_100` scored an in-sample t of
    **6.59**, cleared both a 3.546 Bonferroni threshold and a permutation null
    that reached 5.5 -- and then posted **-1.82 out of sample**.

    So clearing this is necessary and nowhere near sufficient, and the returned
    reason says so rather than letting a `True` read as a verdict.
    """
    bar = bonferroni_threshold(tested, family_alpha)
    if abs(best_statistic) < bar:
        return False, (
            f"the best of {tested} candidates scores {best_statistic:.2f} against a "
            f"{bar:.3f} threshold for a search that size; it is the best of the "
            "search, not a result"
        )
    return True, (
        f"{best_statistic:.2f} clears the {bar:.3f} threshold for {tested} candidates. "
        "That is the in-sample bar only: out-of-sample and walk-forward evidence "
        "decide whether it is real, and this repository has a candidate that cleared "
        "6.59 in sample and went negative out of it"
    )


__all__ = [
    "CONVENTIONAL_T",
    "DEFAULT_FAMILY_ALPHA",
    "NOTABLE_SD",
    "SearchAssessment",
    "assess",
    "bonferroni_threshold",
    "expected_by_chance",
    "survives_correction",
    "clearing_p",
]
