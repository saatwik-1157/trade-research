"""Model 3: is this bar unlike the ones the model was fitted on?

**A statistical detector, and section 17 lists that as a first choice.** Per
feature, the training median and the median absolute deviation; per bar, the
robust z-score `|x - median| / (1.4826 * MAD)`. The score reported is the
largest of those across the required features, squashed into [0, 1]. Median and
MAD rather than mean and standard deviation because the thing being detected is
an outlier, and an outlier moves a mean far more than it moves a median — a
detector fitted with means is partly fitted to the events it exists to find.

**What the score means, stated once because section 10 asks.** It is a
monotone function of how many robust standard deviations the most unusual
feature is from its training median. It is **not** a probability, not a
likelihood, and not a measure of how bad the bar is. `1.0` means "at or beyond
the saturation point", which is a cap rather than a certainty.

**Rare is not the same as bad.** Section 22 and, more forcefully, the project's
own record: the largest single loss in the live log came from a bar whose M1
range was 282 points, and that bar was real. This model labels; it does not
veto, and the exit policies and the risk engine remain the only things that
stop a trade.

**Three statuses, and UNKNOWN is one of them.** A bar the model cannot score —
because a required feature is absent — is `UNKNOWN` via the base class's
`MODEL_INPUT_ERROR`, never `NORMAL`. "We could not look" and "we looked and it
was fine" are different facts, and only one of them should read as reassuring.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.ai.base import BaseModel
from app.ai.contract import FeatureContract, ModelIdentity, Prediction, PredictionStatus

# Scales the MAD so that, for normally distributed data, it estimates the same
# quantity as the standard deviation. Written down rather than inlined because
# a reader will otherwise wonder where 1.4826 came from.
MAD_TO_SIGMA = 1.4826

DEFAULT_FEATURES: tuple[str, ...] = (
    "return_1",
    "range_pct",
    "atr_pct_14",
    "volatility_20",
)


class AnomalyStatus(StrEnum):
    normal = "NORMAL"
    anomalous = "ANOMALOUS"
    unknown = "UNKNOWN"


@dataclass(frozen=True)
class RobustBaseline:
    """Per-feature median and MAD, from the training segment only."""

    medians: dict[str, float]
    deviations: dict[str, float]
    fitted_rows: int
    fitted_on: str = "train"
    # Features whose training values never varied. A zero MAD would make every
    # reading infinitely unusual, so they are named and skipped rather than
    # divided by.
    constant_features: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "medians": {k: round(v, 12) for k, v in sorted(self.medians.items())},
            "deviations": {k: round(v, 12) for k, v in sorted(self.deviations.items())},
            "fitted_rows": self.fitted_rows,
            "fitted_on": self.fitted_on,
            "constant_features": list(self.constant_features),
            "method": "median and median absolute deviation, scaled by 1.4826",
        }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def fit_baseline(
    rows: list[dict[str, float | None]], *, features: tuple[str, ...] = DEFAULT_FEATURES
) -> RobustBaseline:
    """Fit medians and MADs on training rows. Refuses rather than guessing."""
    medians: dict[str, float] = {}
    deviations: dict[str, float] = {}
    constant: list[str] = []
    counts: list[int] = []

    for name in features:
        values = [float(v) for r in rows if (v := r.get(name)) is not None]
        if len(values) < 30:
            raise ValueError(
                f"feature {name!r} has {len(values)} usable training values. Too few for "
                "a median absolute deviation anyone should act on, and inventing one "
                "would make every later score meaningless in a way nothing reports."
            )
        counts.append(len(values))
        median = _median(values)
        mad = _median([abs(v - median) for v in values])
        medians[name] = median
        deviations[name] = mad * MAD_TO_SIGMA
        if deviations[name] == 0.0:
            constant.append(name)

    return RobustBaseline(
        medians=medians,
        deviations=deviations,
        fitted_rows=min(counts),
        constant_features=tuple(constant),
    )


class AnomalyModel(BaseModel):
    """How unusual this bar is against its training baseline."""

    kind = "other"

    def __init__(
        self,
        *,
        version: str = "1.0",
        feature_version: str,
        baseline: RobustBaseline | None = None,
        features: tuple[str, ...] = DEFAULT_FEATURES,
        # Robust z at which the score saturates at 1.0. Six is deliberately
        # generous: at three, ordinary volatile sessions read as anomalies and
        # the label stops meaning anything.
        saturation_z: float = 6.0,
        threshold: float = 0.5,
        max_feature_age: timedelta | None = None,
        dataset_version: str | None = None,
        dataset_fingerprint: str | None = None,
        trained_at: datetime | None = None,
    ) -> None:
        super().__init__(
            ModelIdentity(
                key="anomaly",
                version=version,
                kind=self.kind,
                feature_version=feature_version,
                dataset_version=dataset_version,
                dataset_fingerprint=dataset_fingerprint,
                trained_at=trained_at,
            ),
            FeatureContract(features=features, feature_version=feature_version, lookback=50),
            max_feature_age=max_feature_age,
        )
        self.baseline = baseline
        self.saturation_z = saturation_z
        self.threshold = threshold

    @property
    def fitted(self) -> bool:
        return self.baseline is not None

    def _infer(
        self,
        features: dict[str, float | None],
        *,
        at: datetime,
        symbol: str,
        timeframe: str,
        features_at: datetime | None,
        input_digest: str,
    ) -> Prediction:
        assert self.baseline is not None  # `fitted` gated this
        scores: dict[str, float] = {}
        for name in self.contract.features:
            deviation = self.baseline.deviations.get(name, 0.0)
            if deviation == 0.0:
                # A constant feature carries no information about unusualness.
                # Skipped and named, never treated as infinitely unusual.
                continue
            value = float(features[name])  # type: ignore[arg-type]
            scores[name] = abs(value - self.baseline.medians[name]) / deviation

        if not scores:
            return Prediction(
                identity=self.identity,
                at=at,
                symbol=symbol,
                timeframe=timeframe,
                status=PredictionStatus.insufficient_data,
                detail=(
                    "every required feature was constant across the training segment, so "
                    "there is no spread to measure unusualness against."
                ),
                features_at=features_at,
                input_digest=input_digest,
                metadata={"constant_features": list(self.baseline.constant_features)},
            )

        worst_feature = max(scores, key=lambda name: scores[name])
        worst_z = scores[worst_feature]
        score = min(1.0, worst_z / self.saturation_z)
        label = AnomalyStatus.anomalous if score >= self.threshold else AnomalyStatus.normal

        return Prediction(
            identity=self.identity,
            at=at,
            symbol=symbol,
            timeframe=timeframe,
            status=PredictionStatus.ok,
            value=str(label),
            confidence=round(score, 6),
            # Not a probability. Saying so by leaving the field empty rather
            # than by a caveat somebody has to read.
            probability=None,
            calibrated=False,
            label_definition=(
                "score = min(1, max robust z / saturation_z), where robust z is "
                "|x - training median| / (1.4826 * training MAD). A monotone measure of "
                "unusualness against the training window. NOT a probability, and not a "
                "judgement: a real market event is unusual and is not a defect."
            ),
            features_at=features_at,
            input_digest=input_digest,
            metadata={
                "score": round(score, 6),
                "worst_feature": worst_feature,
                "worst_z": round(worst_z, 6),
                "z_by_feature": {k: round(v, 6) for k, v in sorted(scores.items())},
                "saturation_z": self.saturation_z,
                "threshold": self.threshold,
                "skipped_constant_features": list(self.baseline.constant_features),
            },
        )

    def explanation(self, features: Mapping[str, float | None]) -> list[dict[str, Any]]:
        if self.baseline is None:
            return []
        out: list[dict[str, Any]] = []
        for name in self.contract.features:
            deviation = self.baseline.deviations.get(name, 0.0)
            value = features.get(name)
            out.append(
                {
                    "feature": name,
                    "value": value,
                    "median": self.baseline.medians.get(name),
                    "robust_z": (
                        round(abs(float(value) - self.baseline.medians[name]) / deviation, 6)
                        if value is not None and deviation
                        else None
                    ),
                }
            )
        out.sort(key=lambda item: item["robust_z"] or -1.0, reverse=True)
        return out

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "classes": [str(s) for s in AnomalyStatus],
            "baseline": self.baseline.as_dict() if self.baseline else None,
            "saturation_z": self.saturation_z,
            "threshold": self.threshold,
            "score_meaning": (
                "a monotone function of robust standard deviations from the training "
                "median, capped at 1.0. Not a probability and not a severity."
            ),
            "does_not": "veto a trade. It labels; risk and the exit policies decide.",
        }
