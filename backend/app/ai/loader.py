"""Reading a stored model version back into a model that answers.

L24 wrote `artifact_of()` — a fitted model as JSON — and nothing ever read it
back. Training holds its model in memory and registers it in the same call, so
the round trip was never exercised; validation is the first consumer that starts
from a database row, and section 8 asks it to prove the artifact **loads**.

That belongs here rather than in `app/validation/`, for the reason the whole
project keeps applying: deserialising an L24 model is an `app.ai` concern, and a
copy living in the validation package would be a second answer to "what is a
trade_probability v1.2" the moment either changed. L28's promotion will want the
same function.

**A version that cannot be rebuilt is refused, never approximated.** Missing
coefficients do not become zeros and a missing feature list does not become the
default set: either would produce a model that predicts confidently about
nothing. `ArtifactError` says which field was absent.

**Nothing here promotes.** The function returns a model object. The `status`
column is untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.ai.anomaly import AnomalyModel, RobustBaseline
from app.ai.base import BaseModel
from app.ai.probability import LogisticCoefficients, TradeProbabilityModel
from app.ai.regime import RegimeCuts, RegimeModel
from app.models.ai import ModelVersion


class ArtifactError(Exception):
    """A stored version that cannot be turned back into a model."""


def model_from_version(row: ModelVersion) -> BaseModel:
    """Rebuild the fitted model a `model_versions` row describes.

    The inverse of `trainers.artifact_of()` plus `ai_service.register_version()`.
    Deterministic: the same row always produces a model that predicts the same
    thing, which is what makes a validation report re-checkable later.
    """
    params = row.params or {}
    artifact = params.get("artifact")
    if not isinstance(artifact, dict) or not artifact.get("kind"):
        raise ArtifactError(
            f"model version {row.id} carries no artifact, so there are no fitted "
            "parameters to load. An unfitted model is not validated as a failing one; "
            "there is nothing to measure."
        )
    if not row.feature_version:
        raise ArtifactError(
            f"model version {row.id} does not say which feature set it was fitted "
            "against. Without it the model cannot refuse mismatched inputs, which is "
            "the check every prediction depends on."
        )

    kind = str(artifact["kind"])
    common: dict[str, Any] = {
        "version": _version_label(row),
        "feature_version": row.feature_version,
        "dataset_version": row.dataset_version,
        "dataset_fingerprint": row.dataset_fingerprint,
        "trained_at": _aware(row.training_end or row.created_at),
    }

    if kind == "logistic":
        return TradeProbabilityModel(
            coefficients=_coefficients(artifact.get("coefficients"), row.id),
            label_version=row.label_version,
            **common,
        )
    if kind == "quantile_cuts":
        return RegimeModel(cuts=_cuts(artifact.get("cuts"), row.id), **common)
    if kind == "robust_baseline":
        baseline = _baseline(artifact.get("baseline"), row.id)
        return AnomalyModel(
            baseline=baseline,
            features=tuple(sorted(baseline.medians)),
            **common,
        )
    raise ArtifactError(
        f"model version {row.id} declares artifact kind {kind!r}, which this deployment "
        "cannot load. A kind that is not recognised is refused rather than guessed at: "
        "loading it as something else would produce confident answers from the wrong "
        "arithmetic."
    )


def scaler_from_version(row: ModelVersion) -> dict[str, Any] | None:
    """The preprocessing parameters the version was fitted with, if any.

    Returned as the stored mapping rather than a `Scaler`, because the caller
    that needs one can rebuild it and the caller that only needs to *report* the
    parameters should not have to.
    """
    stored = (row.params or {}).get("scaler")
    return stored if isinstance(stored, dict) else None


def _version_label(row: ModelVersion) -> str:
    """The model's own version string, from the artifact reference.

    `artifact_ref` is written as `"{key}:{version}"` by `register_version`. The
    integer `version` column orders rows; it is not the model's identity.
    """
    reference = row.artifact_ref or ""
    if ":" in reference:
        return reference.split(":", 1)[1]
    return str(row.version)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _coefficients(payload: Any, version_id: str) -> LogisticCoefficients:
    if not isinstance(payload, dict):
        raise ArtifactError(
            f"model version {version_id} has a logistic artifact with no coefficients"
        )
    try:
        return LogisticCoefficients(
            features=tuple(payload["features"]),
            weights=tuple(float(w) for w in payload["weights"]),
            bias=float(payload["bias"]),
            fitted_rows=int(payload.get("fitted_rows", 0)),
            fitted_on=str(payload.get("fitted_on", "train")),
            iterations=int(payload.get("iterations", 0)),
            l2=float(payload.get("l2", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        # `LogisticCoefficients.__post_init__` also raises here when the weight
        # vector does not line up with the feature names -- a stored model that
        # nobody could explain, caught on the way in rather than at inference.
        raise ArtifactError(
            f"model version {version_id} has an unreadable logistic artifact: {exc}"
        ) from exc


def _cuts(payload: Any, version_id: str) -> RegimeCuts:
    if not isinstance(payload, dict):
        raise ArtifactError(f"model version {version_id} has a regime artifact with no cuts")
    try:
        return RegimeCuts(
            trend_low=float(payload["trend_low"]),
            trend_high=float(payload["trend_high"]),
            volatility_low=float(payload["volatility_low"]),
            volatility_high=float(payload["volatility_high"]),
            fitted_rows=int(payload.get("fitted_rows", 0)),
            fitted_on=str(payload.get("fitted_on", "train")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError(
            f"model version {version_id} has an unreadable regime artifact: {exc}"
        ) from exc


def _baseline(payload: Any, version_id: str) -> RobustBaseline:
    if not isinstance(payload, dict):
        raise ArtifactError(f"model version {version_id} has an anomaly artifact with no baseline")
    try:
        return RobustBaseline(
            medians={k: float(v) for k, v in dict(payload["medians"]).items()},
            deviations={k: float(v) for k, v in dict(payload["deviations"]).items()},
            fitted_rows=int(payload.get("fitted_rows", 0)),
            fitted_on=str(payload.get("fitted_on", "train")),
            constant_features=tuple(payload.get("constant_features") or ()),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError(
            f"model version {version_id} has an unreadable anomaly artifact: {exc}"
        ) from exc
