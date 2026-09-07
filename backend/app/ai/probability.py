"""Model 2: the probability of a defined trade outcome.

**"Probability of what" is the whole question, and section 8 says so.** This
model does not estimate "the probability that the trade wins". It estimates the
probability of a *named label* — by default L23's `bracket_outcome == "WIN"`,
which means: a long entry at this bar's close, with a stop at `stop_atr × ATR`
below and a target at `take_profit_atr × ATR` above, reached the target before
the stop within `horizon` bars, net of a stated spread. Change any of those and
it is a different quantity. `label_definition` travels on every prediction so
the number cannot be quoted without it.

**Logistic regression, in the standard library.** Not because a gradient
boosting model would be worse, but because §17 says start with an explainable
baseline and use what the project already supports, and because a logistic
model's coefficients *are* the feature importance §23 asks for — no SHAP, no
surrogate, no explanation that might not correspond to the model. Adding
scikit-learn to a backend image that does not currently carry numpy either (see
`PROJECT_AUDIT.md` §21) would deepen a deployment gap for no measured gain.

**It refuses to predict until it has coefficients.** An unfitted model returns
`MODEL_UNAVAILABLE`. There is no default weight vector, because a default one
answers 0.5 for everything and 0.5 is a number a caller will act on.

**Calibration is claimed only when measured.** Section 20. `calibrated` is
false unless a `CalibrationReport` has been attached, and the reliability
figures come from `app.monitoring.stats` — the Brier score and expected
calibration error that L29 already implements — rather than a second copy.

**Fitting lives here but is not production training.** `fit_logistic` is
deterministic (fixed iterations, no randomness, no shuffling) and exists for
section 37's non-production smoke test. Level 25 owns training; nothing in this
module writes a model version, promotes one, or is called at import.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.ai.base import BaseModel
from app.ai.contract import FeatureContract, ModelIdentity, Prediction, PredictionStatus

# The default label this model estimates. Written here as a constant so that a
# model fitted against a different one has to say so rather than inherit this.
DEFAULT_LABEL_DEFINITION = (
    "P(bracket_outcome == WIN): a long entry at this bar's close reached "
    "take_profit_atr x ATR before stop_atr x ATR within the label set's horizon, "
    "net of the stated spread. Not 'the probability the trade is profitable', and "
    "not a guarantee."
)


@dataclass(frozen=True)
class CalibrationReport:
    """Measured, or absent. There is no assumed calibration.

    `brier` and `expected_calibration_error` come from `app.monitoring.stats`,
    which L29 already built; this type carries them beside the sample size that
    produced them, because a calibration measured on 40 predictions is a number
    with a confidence interval wider than the thing it describes.
    """

    brier: float
    expected_calibration_error: float
    samples: int
    measured_on: str
    bins: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "brier": round(self.brier, 6),
            "expected_calibration_error": round(self.expected_calibration_error, 6),
            "samples": self.samples,
            "measured_on": self.measured_on,
            "bins": list(self.bins),
        }


@dataclass(frozen=True)
class LogisticCoefficients:
    """The fitted model. `weights` is ordered to match `features`."""

    features: tuple[str, ...]
    weights: tuple[float, ...]
    bias: float
    fitted_rows: int
    fitted_on: str = "train"
    iterations: int = 0
    l2: float = 0.0

    def __post_init__(self) -> None:
        if len(self.features) != len(self.weights):
            raise ValueError(
                f"{len(self.features)} features against {len(self.weights)} weights. A "
                "coefficient vector that does not line up with its feature names is a "
                "model nobody can explain or reproduce."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "features": list(self.features),
            "weights": [round(w, 10) for w in self.weights],
            "bias": round(self.bias, 10),
            "fitted_rows": self.fitted_rows,
            "fitted_on": self.fitted_on,
            "iterations": self.iterations,
            "l2": self.l2,
        }


def _sigmoid(z: float) -> float:
    # Split on the sign so neither branch overflows: exp(710) is inf in float64,
    # and an inf here becomes a nan probability that passes every range check.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def fit_logistic(
    rows: list[dict[str, float | None]],
    targets: list[int],
    *,
    features: tuple[str, ...],
    iterations: int = 400,
    learning_rate: float = 0.1,
    l2: float = 0.01,
) -> LogisticCoefficients:
    """Deterministic batch gradient descent. Section 37's smoke test, not training.

    Deliberately unremarkable: full-batch, fixed iteration count, no shuffling,
    no early stopping, weights initialised to zero. Every one of those choices
    removes a source of run-to-run variation, because section 24 asks for
    deterministic inference and a model whose *fit* is nondeterministic cannot
    deliver it.

    Rows with a missing required feature are skipped and counted rather than
    imputed, on the same reasoning as everywhere else in this pipeline.
    """
    if len(rows) != len(targets):
        raise ValueError(f"{len(rows)} rows against {len(targets)} targets")
    if not features:
        raise ValueError("a model with no features cannot be fitted")

    usable: list[tuple[list[float], int]] = []
    for row, target in zip(rows, targets, strict=True):
        raw = [row.get(name) for name in features]
        if any(v is None for v in raw):
            continue
        if target not in (0, 1):
            raise ValueError(f"target {target!r} is not 0 or 1; this is a binary model")
        usable.append(([float(v) for v in raw if v is not None], target))

    if len(usable) < 20:
        raise ValueError(
            f"{len(usable)} complete rows. Too few to fit anything worth acting on, and "
            "fitting anyway would produce coefficients indistinguishable from real ones."
        )

    n = len(features)
    weights = [0.0] * n
    bias = 0.0
    scale = 1.0 / len(usable)

    for _ in range(iterations):
        gradient = [0.0] * n
        bias_gradient = 0.0
        for values, target in usable:
            z = bias + sum(w * v for w, v in zip(weights, values, strict=True))
            error = _sigmoid(z) - target
            bias_gradient += error
            for i, value in enumerate(values):
                gradient[i] += error * value
        for i in range(n):
            weights[i] -= learning_rate * (gradient[i] * scale + l2 * weights[i])
        bias -= learning_rate * bias_gradient * scale

    return LogisticCoefficients(
        features=features,
        weights=tuple(weights),
        bias=bias,
        fitted_rows=len(usable),
        iterations=iterations,
        l2=l2,
    )


class TradeProbabilityModel(BaseModel):
    """P(a named outcome), with the name attached to every answer."""

    kind = "classifier"

    def __init__(
        self,
        *,
        version: str = "1.0",
        feature_version: str,
        coefficients: LogisticCoefficients | None = None,
        label_definition: str = DEFAULT_LABEL_DEFINITION,
        label_version: str | None = None,
        calibration: CalibrationReport | None = None,
        max_feature_age: timedelta | None = None,
        dataset_version: str | None = None,
        dataset_fingerprint: str | None = None,
        trained_at: datetime | None = None,
        lookback: int = 50,
    ) -> None:
        features = coefficients.features if coefficients else ()
        super().__init__(
            ModelIdentity(
                key="trade_probability",
                version=version,
                kind=self.kind,
                feature_version=feature_version,
                label_version=label_version,
                dataset_version=dataset_version,
                dataset_fingerprint=dataset_fingerprint,
                trained_at=trained_at,
            ),
            FeatureContract(features=features, feature_version=feature_version, lookback=lookback),
            max_feature_age=max_feature_age,
        )
        self.coefficients = coefficients
        self.label_definition = label_definition
        self.calibration = calibration

    @property
    def fitted(self) -> bool:
        return self.coefficients is not None

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
        assert self.coefficients is not None  # `fitted` gated this
        values = [float(features[name]) for name in self.coefficients.features]  # type: ignore[arg-type]
        z = self.coefficients.bias + sum(
            w * v for w, v in zip(self.coefficients.weights, values, strict=True)
        )
        probability = _sigmoid(z)

        # Confidence is distance from the coin flip, not the probability
        # itself. A 0.51 is a confident-ish reading of a near-coin-flip, and
        # reporting 0.51 as the confidence would overstate it.
        confidence = abs(probability - 0.5) * 2.0

        return Prediction(
            identity=self.identity,
            at=at,
            symbol=symbol,
            timeframe=timeframe,
            status=PredictionStatus.ok,
            value="ABOVE" if probability >= 0.5 else "BELOW",
            probability=round(probability, 6),
            confidence=round(confidence, 6),
            calibrated=self.calibration is not None,
            label_definition=self.label_definition,
            features_at=features_at,
            input_digest=input_digest,
            metadata={
                "logit": round(z, 6),
                "calibration": self.calibration.as_dict() if self.calibration else None,
                "caveat": (
                    "an uncalibrated probability is a ranking, not a frequency. Do not "
                    "read 0.80 as an 80% success rate unless `calibrated` is true."
                )
                if self.calibration is None
                else None,
            },
        )

    def explanation(self, features: Mapping[str, float | None]) -> list[dict[str, Any]]:
        """Contribution per feature: the weight times the value, sorted.

        For a logistic model these ARE the contributions to the logit — not an
        approximation of them — so naming them cannot claim an influence the
        model did not have. Section 23.
        """
        if self.coefficients is None:
            return []
        out: list[dict[str, Any]] = []
        for name, weight in zip(self.coefficients.features, self.coefficients.weights, strict=True):
            value = features.get(name)
            out.append(
                {
                    "feature": name,
                    "value": value,
                    "weight": round(weight, 8),
                    "contribution": round(weight * float(value), 8) if value is not None else None,
                }
            )
        out.sort(key=lambda item: abs(item["contribution"] or 0.0), reverse=True)
        return out

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "label_definition": self.label_definition,
            "coefficients": self.coefficients.as_dict() if self.coefficients else None,
            "calibration": self.calibration.as_dict() if self.calibration else None,
            "calibrated": self.calibration is not None,
            "caveat": (
                "this is P(a named label), not P(profit). The label definition is part "
                "of the number and is attached to every prediction."
            ),
        }
