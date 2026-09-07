"""What a model may say, and what it must be given before it says it.

One prediction type for every model family. Section 11 asks for exactly that,
and the reason is arithmetic: counters, drift checks and a dashboard can only
be built over predictions that share a shape. Three families with three output
formats is three of everything downstream.

**A prediction that failed carries no value.** `Prediction.value` is `None`
unless `status is PredictionStatus.ok`, enforced in `__post_init__`. That is
the difference between a caller having to check a status and a caller being
unable to read a number that was never computed — and it is the same argument
the sizing engine makes for refusing rather than substituting.

**A missing feature is never substituted.** Section 13. `FeatureContract.check`
returns the names that are absent and the model returns `MODEL_INPUT_ERROR`;
nothing here defaults, imputes, or reaches for a mean. A model fed a zero where
a reading should have been produces a number that looks exactly like a real
one.

**Feature versions must match, and compatibility is declared rather than
assumed.** Sections 14 and 51. A model built against feature set 1.2 handed 1.3
blocks unless 1.3 is named in `compatible_feature_versions`. The default is to
refuse: a feature set version is bumped precisely when some feature's arithmetic
changed, so "probably fine" is the assumption that makes the version pointless.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class ModelError(Exception):
    """A model that cannot be constructed or loaded. Never a silent fallback."""


class PredictionStatus(StrEnum):
    """Why a prediction is, or is not, a number.

    Every non-`ok` value is a refusal with a reason attached. There is
    deliberately no `degraded` or `best_effort`: a prediction is either
    computed from the inputs the model requires, or it is not made.
    """

    ok = "OK"
    # Section 13's two named refusals.
    insufficient_data = "INSUFFICIENT_DATA"
    model_input_error = "MODEL_INPUT_ERROR"
    # Section 51: the versions do not line up.
    feature_version_mismatch = "FEATURE_VERSION_MISMATCH"
    # Section 25: the features are real but too old to describe now.
    stale_features = "STALE_FEATURES"
    # Section 26: there is no model to ask.
    model_unavailable = "MODEL_UNAVAILABLE"


@dataclass(frozen=True)
class FeatureContract:
    """What a model requires before it will answer.

    Section 13. The lookback is declared rather than inferred because a caller
    building features for one timestamp needs to know how much history to feed
    the feature engine, and getting it wrong produces `None`s rather than an
    error.
    """

    features: tuple[str, ...]
    feature_version: str
    lookback: int
    compatible_feature_versions: tuple[str, ...] = ()

    def accepts_version(self, version: str) -> bool:
        return version == self.feature_version or version in self.compatible_feature_versions

    def missing(self, values: dict[str, float | None]) -> tuple[str, ...]:
        """Required features that are absent or null. Order is the contract's."""
        return tuple(name for name in self.features if values.get(name) is None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "features": list(self.features),
            "feature_version": self.feature_version,
            "lookback": self.lookback,
            "compatible_feature_versions": list(self.compatible_feature_versions),
            "policy": (
                "a missing or null required feature is a MODEL_INPUT_ERROR, never a "
                "substituted value; an unlisted feature version blocks inference"
            ),
        }


@dataclass(frozen=True)
class ModelIdentity:
    """Which model, at which version, fitted against what.

    Section 15 and section 33. `dataset_version`, `feature_version` and
    `label_version` travel with the model rather than being looked up, because
    the question a prediction has to answer six months later is "what was this
    fitted on", and an id that has to be joined against three tables to answer
    it is an id that will be quoted without the join.
    """

    key: str
    version: str
    kind: str
    feature_version: str
    label_version: str | None = None
    dataset_version: str | None = None
    dataset_fingerprint: str | None = None
    preprocessing_version: str | None = None
    trained_at: datetime | None = None
    training_period: tuple[datetime, datetime] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.key,
            "version": self.version,
            "kind": self.kind,
            "feature_version": self.feature_version,
            "label_version": self.label_version,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "preprocessing_version": self.preprocessing_version,
            "trained_at": self.trained_at.isoformat() if self.trained_at else None,
            "training_period": (
                [self.training_period[0].isoformat(), self.training_period[1].isoformat()]
                if self.training_period
                else None
            ),
        }


@dataclass(frozen=True)
class Prediction:
    """One model's answer, in the one shape every model uses.

    `probability` is separate from `confidence` on purpose and the difference
    matters. A probability is an estimate of an outcome under a stated label
    definition; a confidence is how sure the model is of its own output. A
    classifier reporting 0.51 for UP is confident-ish about a near-coin-flip,
    and collapsing the two would lose that.

    `calibrated` is false unless calibration has been measured. Section 20: do
    not call 0.80 an actual 80% success rate unless something tested it.
    """

    identity: ModelIdentity
    at: datetime
    symbol: str
    timeframe: str
    status: PredictionStatus = PredictionStatus.ok
    value: str | None = None
    probability: float | None = None
    confidence: float | None = None
    calibrated: bool = False
    label_definition: str | None = None
    detail: str = ""
    features_at: datetime | None = None
    # A digest of the feature values this answer was computed from. Part of the
    # identity because section 24's determinism is about the INPUT: two
    # different feature vectors at one timestamp are two predictions, and an id
    # that could not tell them apart would silently collapse them into one row.
    # Found by `test_the_answer_rate_counts_the_refusals_too`, which recorded
    # three predictions and got one.
    input_digest: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is not PredictionStatus.ok:
            if self.value is not None or self.probability is not None:
                raise ModelError(
                    f"a {self.status} prediction carries a value; a refusal that also "
                    "answers is the thing this type exists to make impossible"
                )
        for name, number in (("probability", self.probability), ("confidence", self.confidence)):
            if number is not None and not 0.0 <= number <= 1.0:
                raise ModelError(f"{name} must be between 0 and 1, not {number}")

    @property
    def ok(self) -> bool:
        return self.status is PredictionStatus.ok

    def prediction_id(self) -> str:
        """A deterministic id for this exact prediction.

        Section 24 requires deterministic inference; a random id would make two
        identical predictions look like two events. Derived from the model, the
        timestamp and the instrument, so re-running produces the same id and a
        duplicate is visible as one.
        """
        material = json.dumps(
            {
                "model": self.identity.key,
                "version": self.identity.version,
                "at": self.at.isoformat(),
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "features_at": self.features_at.isoformat() if self.features_at else None,
                "input": self.input_digest,
                "status": str(self.status),
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def as_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id(),
            **self.identity.as_dict(),
            "timestamp": self.at.isoformat(),
            "features_at": self.features_at.isoformat() if self.features_at else None,
            "input_digest": self.input_digest,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": str(self.status),
            "prediction": self.value,
            "probability": self.probability,
            "confidence": self.confidence,
            "calibrated": self.calibrated,
            "label_definition": self.label_definition,
            "detail": self.detail,
            "metadata": dict(self.metadata),
        }


def refuse(
    identity: ModelIdentity,
    status: PredictionStatus,
    detail: str,
    *,
    at: datetime,
    symbol: str,
    timeframe: str,
    features_at: datetime | None = None,
    input_digest: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Prediction:
    """A prediction that is not one, with the reason attached."""
    return Prediction(
        identity=identity,
        at=at,
        symbol=symbol,
        timeframe=timeframe,
        status=status,
        detail=detail,
        features_at=features_at,
        input_digest=input_digest,
        metadata=metadata or {},
    )


def digest_features(features: dict[str, float | None]) -> str:
    """A stable hash of a feature vector.

    Sorted by name and rendered with `repr` so that 0.1 and 0.1000000000000001
    are different inputs -- which they are, and rounding them together here
    would make two distinguishable predictions share one id.
    """
    material = json.dumps(
        {name: repr(value) for name, value in sorted(features.items())}, sort_keys=True
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def is_stale(features_at: datetime | None, now: datetime, limit: timedelta | None) -> bool:
    """True when the features are older than the model will accept.

    Section 25. Returns False when either the limit or the stamp is absent
    rather than guessing: "we do not know how old this is" is not the same as
    "this is fresh", and the caller that omitted the stamp is the one that
    should be told, which `BaseModel` does by refusing when a limit is set and
    a stamp is not.
    """
    if limit is None or features_at is None:
        return False
    return (now - features_at) > limit
