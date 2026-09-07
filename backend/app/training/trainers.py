"""Fitting a candidate, and the baseline it has to beat.

**A baseline is fitted first, always.** Section 13, and this project has a
sharper reason than convention. Its own searches have produced a long list of
candidates that beat *something* and nothing out of sample; the one defence
that worked was always a null with the same exposure. So every classification
run here fits a `MajorityBaseline` — the constant prior of the training
segment — and reports the candidate beside it. A model that cannot beat "always
say WIN" has told you something, and it is not that it needs more epochs.

**Nothing here fits on anything but the training segment.** Section 10. The
scaler comes from `app.datasets.scaler`, whose `fit()` takes a split and reads
`split.train`, so the leak has no spelling. The class weights are computed from
the training rows. The baseline's prior is the training prior. A holdout is
transformed and scored and never contributes a parameter.

**Nothing is resampled.** Section 15 allows controlled resampling inside the
training data; class weighting achieves the same thing without duplicating
rows, and duplicating rows in a time series creates the same bar twice at the
same timestamp — which every causal guarantee downstream assumes cannot happen.
Weighting is the version that does not lie about how much data there is.

**Early stopping reads the validation segment.** Section 16, and never the test
one. With `patience = 0` it is off, and the run says so rather than reporting a
best epoch it did not choose.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.ai.anomaly import AnomalyModel, fit_baseline
from app.ai.contract import ModelIdentity
from app.ai.probability import LogisticCoefficients, TradeProbabilityModel, _sigmoid
from app.ai.regime import RegimeModel, fit_cuts
from app.datasets.scaler import Scaler
from app.training.config import ModelFamily, TrainingConfig, TrainingError


@dataclass(frozen=True)
class MajorityBaseline:
    """Always predict the training segment's prior. The bar every model clears or does not.

    Not "random": a coin flip is a weaker baseline than the majority class on
    an imbalanced label, and using the weaker one would flatter every candidate.
    """

    prior: float
    fitted_rows: int

    def probability(self) -> float:
        return self.prior

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "majority_class_prior",
            "prior": round(self.prior, 8),
            "fitted_rows": self.fitted_rows,
            "why": (
                "the constant prior of the TRAINING segment. Stronger than a coin flip "
                "on an imbalanced label, and a candidate that cannot beat it has not "
                "learned anything about the market."
            ),
        }


def fit_majority(targets: list[int]) -> MajorityBaseline:
    if not targets:
        raise TrainingError("cannot fit a baseline on no rows")
    return MajorityBaseline(prior=sum(targets) / len(targets), fitted_rows=len(targets))


def class_weights(targets: list[int], mode: str) -> tuple[float, float]:
    """Weights for the negative and positive class. `none` means (1, 1).

    Balanced weighting scales each class by `n / (2 * class_count)`, the
    standard form, so the two classes contribute equally to the gradient
    without any row being duplicated.
    """
    if mode == "none":
        return (1.0, 1.0)
    n = len(targets)
    positives = sum(targets)
    negatives = n - positives
    if positives == 0 or negatives == 0:
        raise TrainingError(
            f"class_weight='balanced' needs both classes present; this training segment "
            f"has {positives} positive and {negatives} negative rows. Weighting an "
            "absent class is a division by zero dressed as a hyperparameter."
        )
    return (n / (2 * negatives), n / (2 * positives))


@dataclass
class FitReport:
    """What the fit did, beside what it produced."""

    stopped_because: str = "iterations exhausted"
    best_iteration: int | None = None
    best_validation_log_loss: float | None = None
    iterations_run: int = 0
    weights_used: tuple[float, float] = (1.0, 1.0)
    history: list[dict[str, float]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stopped_because": self.stopped_because,
            "best_iteration": self.best_iteration,
            "best_validation_log_loss": (
                round(self.best_validation_log_loss, 8)
                if self.best_validation_log_loss is not None
                else None
            ),
            "iterations_run": self.iterations_run,
            "class_weights": {"negative": self.weights_used[0], "positive": self.weights_used[1]},
            # Trimmed: a 400-iteration history in a JSON column is noise, and
            # the first, last and best are what anyone reads.
            "history": self.history[:1] + self.history[-1:] if self.history else [],
            "early_stopping": ("on the VALIDATION segment. The test segment is never consulted."),
        }


def _log_loss(probabilities: list[float], targets: list[int]) -> float:
    epsilon = 1e-12
    return -sum(
        t * math.log(min(max(p, epsilon), 1 - epsilon))
        + (1 - t) * math.log(1 - min(max(p, epsilon), 1 - epsilon))
        for p, t in zip(probabilities, targets, strict=True)
    ) / max(len(targets), 1)


def fit_weighted_logistic(
    train_rows: list[list[float]],
    train_targets: list[int],
    *,
    features: tuple[str, ...],
    config: TrainingConfig,
    validation_rows: list[list[float]] | None = None,
    validation_targets: list[int] | None = None,
    should_stop: Any = None,
) -> tuple[LogisticCoefficients, FitReport]:
    """Deterministic batch gradient descent with class weights and early stopping.

    A widened form of `app.ai.probability.fit_logistic` rather than a second
    implementation of it: same update, same zero initialisation, same absence of
    shuffling. What this adds is the two things a *training run* needs and an
    inference-side fitter does not — weights and a stopping rule — and the
    weights collapse to (1, 1) when they are off, so the two agree exactly.

    `should_stop` is a callable checked between iterations, which is what makes
    cancellation cooperative rather than a killed task with a half-written row.
    """
    if not features:
        raise TrainingError("a model with no features cannot be fitted")
    if len(train_rows) != len(train_targets):
        raise TrainingError("rows and targets do not line up")

    negative_weight, positive_weight = class_weights(train_targets, config.class_weight)
    report = FitReport(weights_used=(negative_weight, positive_weight))

    n = len(features)
    weights = [0.0] * n
    bias = 0.0
    scale = 1.0 / len(train_rows)
    best: tuple[float, list[float], float, int] | None = None
    since_best = 0

    for iteration in range(1, config.iterations + 1):
        if should_stop is not None and should_stop():
            report.stopped_because = "cancelled"
            report.iterations_run = iteration - 1
            break

        gradient = [0.0] * n
        bias_gradient = 0.0
        for values, target in zip(train_rows, train_targets, strict=True):
            z = bias + sum(w * v for w, v in zip(weights, values, strict=True))
            weight = positive_weight if target == 1 else negative_weight
            error = (_sigmoid(z) - target) * weight
            bias_gradient += error
            for i, value in enumerate(values):
                gradient[i] += error * value
        for i in range(n):
            weights[i] -= config.learning_rate * (gradient[i] * scale + config.l2 * weights[i])
        bias -= config.learning_rate * bias_gradient * scale
        report.iterations_run = iteration

        if config.early_stopping_patience and validation_rows and validation_targets:
            probabilities = [
                _sigmoid(bias + sum(w * v for w, v in zip(weights, row, strict=True)))
                for row in validation_rows
            ]
            loss = _log_loss(probabilities, validation_targets)
            report.history.append({"iteration": iteration, "validation_log_loss": round(loss, 8)})
            if best is None or loss < best[0]:
                best = (loss, list(weights), bias, iteration)
                since_best = 0
            else:
                since_best += 1
                if since_best >= config.early_stopping_patience:
                    report.stopped_because = "early stopping on validation log loss"
                    break

    if best is not None and report.stopped_because.startswith("early stopping"):
        # Restore the best parameters, not the last ones. Stopping at the point
        # performance turned and then keeping the worse weights would make the
        # patience setting cosmetic.
        _, weights, bias, iteration = best
        report.best_iteration = iteration
        report.best_validation_log_loss = best[0]
    elif best is not None:
        report.best_iteration = best[3]
        report.best_validation_log_loss = best[0]

    return (
        LogisticCoefficients(
            features=features,
            weights=tuple(weights),
            bias=bias,
            fitted_rows=len(train_rows),
            iterations=report.iterations_run,
            l2=config.l2,
        ),
        report,
    )


def vectorise(
    rows: list[dict[str, float | None]], features: tuple[str, ...]
) -> tuple[list[list[float]], list[int]]:
    """Rows and their indices, keeping only those with every feature present.

    Returns the indices too, so a caller can line the kept rows up with their
    labels rather than assuming nothing was dropped.
    """
    values: list[list[float]] = []
    kept: list[int] = []
    for index, row in enumerate(rows):
        picked = [row.get(name) for name in features]
        if any(v is None for v in picked):
            continue
        values.append([float(v) for v in picked if v is not None])
        kept.append(index)
    return values, kept


def build_model(
    family: ModelFamily,
    *,
    config: TrainingConfig,
    feature_version: str,
    dataset_version: str,
    dataset_fingerprint: str,
    coefficients: LogisticCoefficients | None = None,
    train_rows: list[dict[str, float | None]] | None = None,
    scaler: Scaler | None = None,
) -> RegimeModel | TradeProbabilityModel | AnomalyModel:
    """The trained candidate, as an L24 model rather than a new type.

    Section 3: training must work with the interface level 24 established. So a
    run produces one of the three existing classes, which means the artifact is
    immediately usable by inference, by the registry and by a backtest without
    a conversion step that could disagree with the fit.
    """
    common = {
        "version": config.model_version,
        "feature_version": feature_version,
        "dataset_version": dataset_version,
        "dataset_fingerprint": dataset_fingerprint,
    }
    if family is ModelFamily.trade_probability:
        if coefficients is None:
            raise TrainingError("a probability model needs coefficients")
        return TradeProbabilityModel(coefficients=coefficients, **common)  # type: ignore[arg-type]
    if family is ModelFamily.regime:
        if train_rows is None:
            raise TrainingError("a regime model needs training rows to fit its quantiles")
        return RegimeModel(cuts=fit_cuts(train_rows), **common)  # type: ignore[arg-type]
    if family is ModelFamily.anomaly:
        if train_rows is None:
            raise TrainingError("an anomaly model needs training rows to fit its baseline")
        return AnomalyModel(baseline=fit_baseline(train_rows), **common)  # type: ignore[arg-type]
    raise TrainingError(f"unknown model family {family!r}")


def artifact_of(model: RegimeModel | TradeProbabilityModel | AnomalyModel) -> dict[str, Any]:
    """The fitted parameters, as JSON.

    Section 20 says not to put a large binary model in the database unless the
    architecture intends to. These are not large: a handful of floats each, and
    keeping them structured means a version can be inspected, diffed and
    queried rather than only loaded. When a family arrives whose parameters are
    megabytes, it will need object storage and that is a decision to take then.
    """
    if isinstance(model, TradeProbabilityModel):
        assert model.coefficients is not None
        return {"kind": "logistic", "coefficients": model.coefficients.as_dict()}
    if isinstance(model, RegimeModel):
        assert model.cuts is not None
        return {"kind": "quantile_cuts", "cuts": model.cuts.as_dict()}
    assert isinstance(model, AnomalyModel) and model.baseline is not None
    return {"kind": "robust_baseline", "baseline": model.baseline.as_dict()}


def identity_of(model: RegimeModel | TradeProbabilityModel | AnomalyModel) -> ModelIdentity:
    return model.identity
