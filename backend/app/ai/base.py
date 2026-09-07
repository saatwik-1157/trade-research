"""The gate every model passes through before it is allowed to answer.

Four checks, in one place, in a fixed order:

  1. **Is there a fitted model at all?** An unfitted one returns
     `MODEL_UNAVAILABLE` rather than a default. Section 26.
  2. **Do the feature versions line up?** `FEATURE_VERSION_MISMATCH` if not.
     Sections 14 and 51.
  3. **Are the required features present?** `MODEL_INPUT_ERROR` naming the
     missing ones. Section 13, and nothing is substituted.
  4. **Are they fresh enough?** `STALE_FEATURES`. Section 25.

Only then does `_infer` run. Putting the gate in the base class rather than in
each model means a fourth family added later cannot forget one of them, and the
order is fixed because the reasons compose badly: a model asked with the wrong
feature version usually also has missing features, and reporting the second
would send a caller looking for data rather than for a version.

**Inference is deterministic.** No model here reads a clock, calls a random
number generator, or mutates state during `predict`. The timestamp is an
argument. Section 24, and it is what makes `Prediction.prediction_id()` stable.

**No model imports anything that trades.** `app/ai/` has no path to the OMS,
the risk engine, the sizing calculator, a position or a broker adapter, and a
test parses every module to keep it that way. The strongest thing this layer
can do is decline, and it does that through the seat in `app/execution/ai.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from app.ai.contract import (
    FeatureContract,
    ModelIdentity,
    Prediction,
    PredictionStatus,
    digest_features,
    is_stale,
    refuse,
)


class BaseModel(ABC):
    """A model that answers in one shape and refuses in five."""

    def __init__(
        self,
        identity: ModelIdentity,
        contract: FeatureContract,
        *,
        max_feature_age: timedelta | None = None,
    ) -> None:
        self.identity = identity
        self.contract = contract
        self.max_feature_age = max_feature_age

    # ------------------------------------------------------------ subclass

    @property
    @abstractmethod
    def fitted(self) -> bool:
        """False until the model has the parameters it needs to answer."""

    @abstractmethod
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
        """The model's own arithmetic. Only reached once the gate passes."""

    # ---------------------------------------------------------------- gate

    def predict(
        self,
        features: dict[str, float | None],
        *,
        at: datetime,
        symbol: str,
        timeframe: str,
        feature_version: str,
        features_at: datetime | None = None,
    ) -> Prediction:
        """One prediction, or one refusal with the reason attached."""
        # Computed once, before the gate, so a refusal is identified by the same
        # input an answer would have been. Two refusals over different features
        # are two events, and an id that could not tell them apart would record
        # one.
        digest = digest_features(features)

        def deny(status: PredictionStatus, detail: str) -> Prediction:
            return refuse(
                self.identity,
                status,
                detail,
                at=at,
                symbol=symbol,
                timeframe=timeframe,
                features_at=features_at,
                input_digest=digest,
            )

        if not self.fitted:
            return deny(
                PredictionStatus.model_unavailable,
                f"{self.identity.key} v{self.identity.version} has no fitted parameters. "
                "An unfitted model returns nothing rather than a default, because a "
                "default is indistinguishable from an answer downstream.",
            )

        if not self.contract.accepts_version(feature_version):
            return deny(
                PredictionStatus.feature_version_mismatch,
                f"{self.identity.key} v{self.identity.version} was fitted against feature "
                f"set {self.contract.feature_version} and was given {feature_version}. A "
                "feature set version is bumped when some feature's arithmetic changed, so "
                "this blocks rather than assuming the change was harmless.",
            )

        missing = self.contract.missing(features)
        if missing:
            return deny(
                PredictionStatus.model_input_error,
                f"{len(missing)} required feature(s) are absent or null: "
                f"{', '.join(missing)}. Nothing is substituted -- a model fed a zero "
                "where a reading should have been produces a number that looks exactly "
                "like a real one.",
            )

        if self.max_feature_age is not None and features_at is None:
            return deny(
                PredictionStatus.stale_features,
                f"{self.identity.key} accepts features no older than "
                f'{self.max_feature_age}, and none was stamped. "We do not know how old '
                'this is" is not the same as "this is fresh".',
            )
        if is_stale(features_at, at, self.max_feature_age):
            assert features_at is not None  # narrowed by is_stale
            return deny(
                PredictionStatus.stale_features,
                f"the features are stamped {features_at.isoformat()} and the limit is "
                f"{self.max_feature_age}. A prediction from stale features describes a "
                "market that has moved.",
            )

        return self._infer(
            features,
            at=at,
            symbol=symbol,
            timeframe=timeframe,
            features_at=features_at,
            input_digest=digest,
        )

    # ------------------------------------------------------------ metadata

    def describe(self) -> dict[str, Any]:
        """Everything a caller needs to decide whether to trust this model."""
        return {
            **self.identity.as_dict(),
            "fitted": self.fitted,
            "contract": self.contract.as_dict(),
            "max_feature_age_seconds": (
                self.max_feature_age.total_seconds() if self.max_feature_age else None
            ),
            "deterministic": True,
            "authority": (
                "advisory. This model cannot place, size or approve an order, cannot "
                "raise a risk limit and cannot enable live trading."
            ),
        }

    def explanation(self, features: Mapping[str, float | None]) -> list[dict[str, Any]]:
        """Which inputs moved this answer, or nothing at all.

        Section 23 asks for explanations where practical and forbids claiming a
        feature influenced a model when it did not. The default is an empty
        list rather than a plausible-looking ranking: a model that cannot say
        which inputs mattered should say nothing, and every model here that CAN
        say overrides this.
        """
        return []
