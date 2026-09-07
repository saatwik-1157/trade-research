"""The statistics validation rests on, and where each one already lived.

**Almost nothing here is new.** The project has carried this methodology since
before the platform existed, and `MIGRATION_STATUS.md` has recorded L26 as
"COMPLETE (methodology)" for exactly that reason. This module calls it:

  * `tools/rule_search.clustered_by_date` and `clustered_t` — the correction
    that actually applies to this data. Every FX pair on the book has USD on
    one side, so one dollar move opens correlated trades in all of them at
    once; pooling treats those as independent and inflates t by roughly the
    square root of how many fire together.
  * `app.monitoring.stats` (L29) — Brier score, reliability bins, expected
    calibration error, Benjamini-Hochberg, Welch's t.

What is added is the one thing neither had: **a permutation null over a
model's own predictions.** Sections 13 and 23 ask for a baseline and a
significance test; this project's measured position is sharper than either.
Its searches produced a 36-cell sweep in which the *random* rule scored an
in-sample t of 1.76 against the best real candidate's 0.83. Beating a baseline
is not evidence. Beating your own shuffled self, with the same trade count and
the same exposure, is the minimum bar.

**Bonferroni over the candidates actually tried.** A model selected from twenty
training runs and quoted at p < 0.05 has been selected, not tested. The
correction is applied and the uncorrected figure is reported beside it, because
hiding either one is how a search launders itself into a finding.
"""

from __future__ import annotations

import math
import os
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.monitoring.stats import normal_sf


def _toolkit(module: str):  # noqa: ANN202
    """Import a research-toolkit module without copying it.

    The same mechanism `app/strategies/indicators.py` uses, and for the same
    reason: `rule_search`'s clustering is the canonical implementation in this
    repository, and a second copy would be a second answer.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    tools = os.path.join(root, "tools")
    if os.path.isdir(tools) and tools not in sys.path:
        sys.path.insert(0, tools)
    import importlib

    return importlib.import_module(module)


@dataclass(frozen=True)
class PermutationResult:
    """Where the real score sits in the distribution of its own shuffles."""

    observed: float
    null_mean: float
    null_std: float
    null_best: float
    permutations: int
    p_value: float
    alpha: float
    corrected_alpha: float
    candidates_tried: int
    beats_null: bool
    beats_corrected: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "observed": round(self.observed, 8),
            "null_mean": round(self.null_mean, 8),
            "null_std": round(self.null_std, 8),
            "null_best": round(self.null_best, 8),
            "permutations": self.permutations,
            "p_value": round(self.p_value, 6),
            "alpha": self.alpha,
            "corrected_alpha": round(self.corrected_alpha, 8),
            "candidates_tried": self.candidates_tried,
            "beats_null": self.beats_null,
            "beats_corrected": self.beats_corrected,
            "method": (
                "the model's own predictions shuffled against the outcomes, which "
                "preserves the trade count and the class balance so the null pays the "
                "same costs and takes the same exposure. Beating a baseline is not "
                "evidence; beating your own shuffled self is the minimum bar."
            ),
            "correction": (
                f"Bonferroni over {self.candidates_tried} candidate(s) tried. A model "
                "selected from many runs and quoted uncorrected has been selected, not "
                "tested."
            ),
        }


def permutation_test(
    scores: list[float],
    outcomes: list[int],
    statistic: Callable[[list[float], list[int]], float],
    *,
    permutations: int = 200,
    alpha: float = 0.05,
    candidates_tried: int = 1,
    seed: int = 12345,
) -> PermutationResult:
    """Shuffle the outcomes against the scores and see where the real one sits.

    Shuffling the OUTCOMES rather than the scores keeps the model's own
    distribution of confidence intact, so the null has the same shape of
    predictions and differs only in whether they line up. `seed` is fixed
    because a validation result that changes between runs is not a validation
    result.
    """
    if len(scores) != len(outcomes):
        raise ValueError(f"{len(scores)} scores against {len(outcomes)} outcomes")
    if permutations < 20:
        raise ValueError(
            f"{permutations} permutations cannot resolve a p-value anyone should act "
            "on; the smallest achievable is 1/(n+1)"
        )
    if len(scores) < 20:
        raise ValueError(f"{len(scores)} observations is too few to permute meaningfully")

    observed = statistic(scores, outcomes)
    rng = random.Random(seed)
    shuffled = list(outcomes)
    null: list[float] = []
    for _ in range(permutations):
        rng.shuffle(shuffled)
        null.append(statistic(scores, list(shuffled)))

    at_least_as_good = sum(1 for value in null if value >= observed)
    # The +1s are Phipson-Smyth: a p-value of exactly 0 claims more than
    # `permutations` shuffles can support.
    p_value = (at_least_as_good + 1) / (permutations + 1)
    mean = sum(null) / len(null)
    variance = sum((v - mean) ** 2 for v in null) / max(len(null) - 1, 1)
    corrected = alpha / max(candidates_tried, 1)

    return PermutationResult(
        observed=observed,
        null_mean=mean,
        null_std=math.sqrt(variance),
        null_best=max(null),
        permutations=permutations,
        p_value=p_value,
        alpha=alpha,
        corrected_alpha=corrected,
        candidates_tried=max(candidates_tried, 1),
        beats_null=p_value < alpha,
        beats_corrected=p_value < corrected,
    )


def roc_auc(scores: list[float], outcomes: list[int]) -> float:
    """AUC by rank, ties averaged. Also the permutation statistic.

    Written here rather than pulled from a library for the same reason the
    models are: there is no ML framework in this backend, and an AUC is a rank
    sum. Ties are averaged because a model that outputs the same probability
    for many bars would otherwise score differently depending on sort order.
    """
    positives = sum(outcomes)
    negatives = len(outcomes) - positives
    if positives == 0 or negatives == 0:
        return 0.5

    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1

    positive_rank_sum = sum(r for r, o in zip(ranks, outcomes, strict=True) if o == 1)
    return (positive_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def bootstrap_interval(
    values: list[float], *, samples: int = 500, confidence: float = 0.95, seed: int = 999
) -> dict[str, Any]:
    """A percentile interval around the mean. Section 23.

    Reported rather than turned into a verdict: an interval that includes zero
    says the sample cannot distinguish this from nothing, which is information
    a threshold would throw away.
    """
    if len(values) < 10:
        return {
            "samples": len(values),
            "note": "too few observations to bootstrap; no interval is reported",
        }
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(samples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    low = means[int((1 - confidence) / 2 * samples)]
    high = means[min(int((1 + confidence) / 2 * samples), samples - 1)]
    mean = sum(values) / n
    return {
        "mean": round(mean, 8),
        "low": round(low, 8),
        "high": round(high, 8),
        "confidence": confidence,
        "resamples": samples,
        "includes_zero": low <= 0.0 <= high,
        "note": (
            "a percentile bootstrap on i.i.d. resampling. Trades that overlap in time "
            "are not independent, so this is an optimistic interval -- read the "
            "date-clustered t beside it."
        ),
    }


def clustered(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Date- and symbol-clustered t, from the toolkit's own implementation.

    `rows` need `symbol`, `net` and `entry_time` (epoch seconds), which is the
    shape `rule_search` already uses. Called rather than reimplemented: this is
    the correction the project's own results turned on, and a second copy would
    eventually disagree with the one every recorded figure was measured under.
    """
    if len(rows) < 3:
        return {"note": f"{len(rows)} trades is too few for clustered inference"}
    search = _toolkit("rule_search")
    out: dict[str, Any] = {}
    out.update(search.clustered_by_date(rows))
    out.update(search.clustered_t(rows))
    out["why"] = (
        "every pair on the book has USD on one side, so one dollar move opens "
        "correlated trades in several at once. Pooling treats those as independent "
        "observations and inflates t by roughly the square root of how many fire "
        "together. Quote the DATE-clustered figure; the per-symbol one is a "
        "consistency check and moves the other way."
    )
    return out


def pooled_t(values: list[float]) -> dict[str, Any]:
    """The uncorrected t across observations. Reported, never quoted alone."""
    n = len(values)
    if n < 3:
        return {"n": n, "t_stat_pooled": None, "note": "too few observations"}
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    if variance == 0:
        return {"n": n, "t_stat_pooled": None, "note": "no variation"}
    t = mean / (math.sqrt(variance) / math.sqrt(n))
    return {
        "n": n,
        "mean": round(mean, 8),
        "t_stat_pooled": round(t, 4),
        "p_two_sided": round(2 * normal_sf(abs(t)), 6),
        "note": (
            "pooled across trades, so overlapping and correlated positions each count "
            "as an independent observation. An upper bound on the evidence, not a "
            "clean one -- the date-clustered figure is the one to quote."
        ),
    }
