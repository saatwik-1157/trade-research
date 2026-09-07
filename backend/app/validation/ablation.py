"""Does each component actually add anything? L75 §33/§52, L76 §52, L77 §44/§70.

Four levels ask for this and it is the only one of their requirements that was
both absent and unblocked, because it needs nothing that does not already
exist: `economics.evaluate` runs a model's decisions through the one simulator,
`datasets.splits` supplies out-of-sample folds, and `research.selection` counts
how many candidates clear by chance.

**The question it answers is the one this project has never asked.** Every
component here -- the AI seat, the regime model, the anomaly detector -- was
built because it sounded like it should help. None has been measured against
the system without it. L77's closing principle is *"Never confuse complexity
with edge"*, and an ablation is the only measurement that can tell the two
apart.

**Removal, not addition.** The arm is the whole system minus one part, because
that is the counterfactual a decision to keep the part rests on: not "does it
help when added to nothing" but "would we lose anything by deleting it".

**The guard that matters most.**

    A component cannot demonstrate incremental value inside a system that has
    no demonstrated value of its own.

If the full system does not beat its own null, then "removing X makes it worse"
compares two quantities that are both noise, and the sign of the difference is
a coin flip dressed as a finding. `ablate` therefore refuses to award any
component a positive verdict when the baseline arm has not cleared -- it
returns `BASELINE_NOT_ESTABLISHED` for every component instead. This repository
has the receipts for why: a 36-cell bracket sweep in which the RANDOM rule
scored 1.76 in sample against the best real candidate's 0.83.

**Ablating N components is N hypotheses**, so the report carries
`research.selection.assess` over the whole family. Picking the component with
the largest delta out of eight and calling it valuable is the selection error
that module exists to catch.

**The default verdict is `NO_INCREMENTAL_VALUE_DEMONSTRATED`.** It is not a
failure and L75 §33 says so directly: if removing a component does not reduce
out-of-sample performance, that component has no demonstrated incremental edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.research import selection
from app.validation import statistics

#: Out-of-sample trades an arm needs before its figure is worth differencing.
#: Below this the estimate is dominated by which trades happened to land in the
#: fold. Not a measured boundary -- recorded as an assumption, in the same terms
#: `portfolio.decision.DEFAULT_MAX_AGE` records itself.
MIN_TRADES = 30

#: How much of the baseline's own figure a delta must exceed before it is called
#: anything but noise, as a fraction. Deliberately coarse: a finer threshold
#: would imply a precision the sample sizes here do not support.
MATERIAL_FRACTION = 0.10


@dataclass(frozen=True)
class Arm:
    """One configuration evaluated out of sample.

    `removed` is `None` for the baseline -- the full system -- and otherwise
    names the single component this arm was run without.

    `result` is whatever `economics.evaluate` returned, so an arm carries the
    trade rows as well as the summary and the significance can be recomputed
    from the same trades the economics came from.
    """

    name: str
    removed: str | None
    result: dict[str, Any] = field(default_factory=dict)

    @property
    def trades(self) -> int:
        return int(self.result.get("trades", 0) or 0)

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self.result.get("rows", []) or [])

    def figure(self, metric: str) -> float | None:
        """The arm's out-of-sample figure, or None when it was not measured."""
        value = self.result.get(metric)
        return None if value is None else float(value)


@dataclass(frozen=True)
class Contribution:
    """What removing one component did."""

    component: str
    with_it: float | None
    without_it: float | None
    delta: float | None
    trades_with: int
    trades_without: int
    verdict: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "with_it": self.with_it,
            "without_it": self.without_it,
            "delta": self.delta,
            "trades_with": self.trades_with,
            "trades_without": self.trades_without,
            "verdict": self.verdict,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AblationReport:
    """What an ablation of one system established, and what it did not."""

    metric: str
    baseline_figure: float | None
    baseline_trades: int
    baseline_established: bool
    baseline_detail: str
    contributions: tuple[Contribution, ...]
    family: selection.SearchAssessment | None

    @property
    def demonstrated(self) -> tuple[str, ...]:
        return tuple(
            c.component for c in self.contributions if c.verdict == "VALUE_DEMONSTRATED"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": {
                "figure": self.baseline_figure,
                "trades": self.baseline_trades,
                "established": self.baseline_established,
                "detail": self.baseline_detail,
            },
            "contributions": [c.as_dict() for c in self.contributions],
            "family": None if self.family is None else self.family.__dict__,
            "demonstrated": list(self.demonstrated),
            "note": (
                "A component's value is the difference between the system with it "
                "and the system without it, measured out of sample. Removing N "
                "components is N hypotheses, so read `family` before reading any "
                "single delta."
            ),
        }


def baseline_established(arm: Arm) -> tuple[bool, str]:
    """Whether the full system beats its own null.

    The gate the rest of this module hangs on. It asks the date-clustered
    question rather than the pooled one, because every pair on this book has USD
    on one side and pooling inflates t by roughly the square root of how many
    fire on one move.
    """
    if arm.trades < MIN_TRADES:
        return False, (
            f"the baseline made {arm.trades} out-of-sample trades, below the {MIN_TRADES} "
            "this module will difference. Nothing is concluded about any component"
        )
    stats = statistics.clustered(arm.rows)
    t = stats.get("t_stat_clustered_by_date")
    if t is None:
        return False, "the baseline's trades could not be clustered, so it is not established"
    if float(t) < selection.CONVENTIONAL_T:
        return False, (
            f"the baseline scores a date-clustered t of {float(t):.2f}, below "
            f"{selection.CONVENTIONAL_T}. A component cannot demonstrate value inside a "
            "system that has not demonstrated any: the difference between two "
            "quantities that are both noise has a sign, and the sign means nothing"
        )
    return True, f"the baseline clears its null at a date-clustered t of {float(t):.2f}"


def _verdict(
    baseline_ok: bool,
    with_it: float | None,
    without_it: float | None,
    trades_without: int,
    material: float,
) -> tuple[str, str]:
    if with_it is None or without_it is None:
        return "NOT_MEASURED", "one of the two arms produced no figure for this metric"
    if trades_without < MIN_TRADES:
        return "INSUFFICIENT_SAMPLE", (
            f"the arm without it made {trades_without} trades, below {MIN_TRADES}. "
            "The difference is dominated by which trades landed in the fold"
        )
    delta = with_it - without_it
    if not baseline_ok:
        return "BASELINE_NOT_ESTABLISHED", (
            f"removing it moves the figure by {delta:+.4f}, and that is not evidence: "
            "the full system has not cleared its own null, so this is a difference "
            "between two noise estimates"
        )
    if delta <= material:
        return "NO_INCREMENTAL_VALUE_DEMONSTRATED", (
            f"the system without it scored {without_it:+.4f} against {with_it:+.4f}. "
            f"Removing it did not cost more than the {material:.4f} materiality "
            "threshold, so nothing here argues for keeping it"
        )
    return "VALUE_DEMONSTRATED", (
        f"removing it cost {delta:+.4f}, from {with_it:+.4f} to {without_it:+.4f}, "
        "out of sample. Read `family` before treating one delta as a finding"
    )


def ablate(
    baseline: Arm,
    variants: list[Arm],
    *,
    metric: str = "expectancy",
    material_fraction: float = MATERIAL_FRACTION,
) -> AblationReport:
    """Measure what each removed component was contributing.

    `baseline` is the full system; each variant is the system with exactly one
    component removed. Both must have been evaluated on the SAME out-of-sample
    fold through `economics.evaluate`, or the deltas compare two different
    questions.

    Nothing here selects, promotes or adopts anything. It reports, and L77 §38
    requires the promotion decision to pass a champion/challenger review that a
    function cannot give itself.
    """
    ok, detail = baseline_established(baseline)
    base_figure = baseline.figure(metric)
    material = abs(base_figure or 0.0) * material_fraction

    contributions: list[Contribution] = []
    for arm in variants:
        if arm.removed is None:
            # A variant that removed nothing is a second baseline, not an
            # ablation. Refusing is cheaper than silently reporting a zero.
            raise ValueError(f"variant {arm.name!r} names no removed component")
        without = arm.figure(metric)
        verdict, why = _verdict(ok, base_figure, without, arm.trades, material)
        contributions.append(
            Contribution(
                component=arm.removed,
                with_it=base_figure,
                without_it=without,
                delta=None if (base_figure is None or without is None) else base_figure - without,
                trades_with=baseline.trades,
                trades_without=arm.trades,
                verdict=verdict,
                detail=why,
            )
        )

    # N components is N hypotheses. Without this, the largest of eight deltas
    # gets read as a finding when several are expected to look positive by
    # chance alone.
    family = (
        selection.assess(
            tested=len(contributions),
            cleared=sum(1 for c in contributions if c.verdict == "VALUE_DEMONSTRATED"),
        )
        if contributions
        else None
    )

    return AblationReport(
        metric=metric,
        baseline_figure=base_figure,
        baseline_trades=baseline.trades,
        baseline_established=ok,
        baseline_detail=detail,
        contributions=tuple(contributions),
        family=family,
    )
