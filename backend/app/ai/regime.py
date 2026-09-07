"""Model 1: what kind of market this is.

Section 6 is explicit — *do not hardcode regime labels without a defined
methodology* — so the methodology is written down here and the thresholds are
**fitted**, not chosen.

**The methodology.** Two axes, each measured by one dimensionless feature that
L23 already computes:

  * *trend*, from `ema_spread_10_50` — the fast EMA minus the slow one as a
    fraction of the slow one. Its sign is the direction and its magnitude is
    the strength.
  * *volatility*, from `atr_pct_14` — the average true range as a fraction of
    price.

Both are cut at **quantiles of the training data**, not at constants. A 0.3%
ATR is high volatility in EURUSD and quiet in BTC, so any fixed number would
encode one instrument's habits as a universal fact — which is the same mistake
as a raw price level being a feature, one level up.

```
                 volatility (atr_pct_14 quantile)
                   low            middle          high
   trend  strong+  TRENDING_UP    TRENDING_UP     HIGH_VOLATILITY
          flat     LOW_VOLATILITY RANGING         HIGH_VOLATILITY
          strong-  TRENDING_DOWN  TRENDING_DOWN   HIGH_VOLATILITY
```

Volatility wins at the top of the range on purpose. A market moving violently
in one direction is more usefully described as violent than as trending: the
thing a risk decision needs to know is that the range has widened, and the
direction is already in the signal.

**Confidence is distance from a boundary, and low confidence means UNKNOWN.**
Section 7. A reading sitting exactly on the trend cut is a coin flip between
two labels, and forcing one produces a regime series that flickers. Below
`minimum_confidence` the answer is `UNKNOWN`, which is a real answer and not a
missing one.

**Fitting is quantile estimation on the training segment only.** It reads the
rows a `Split` calls train and nothing else, so a regime model inherits L23's
leakage discipline rather than restating it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.ai.base import BaseModel
from app.ai.contract import FeatureContract, ModelIdentity, Prediction, PredictionStatus

TREND_FEATURE = "ema_spread_10_50"
VOLATILITY_FEATURE = "atr_pct_14"
REQUIRED: tuple[str, ...] = (TREND_FEATURE, VOLATILITY_FEATURE)


class Regime(StrEnum):
    trending_up = "TRENDING_UP"
    trending_down = "TRENDING_DOWN"
    ranging = "RANGING"
    high_volatility = "HIGH_VOLATILITY"
    low_volatility = "LOW_VOLATILITY"
    unknown = "UNKNOWN"


@dataclass(frozen=True)
class RegimeCuts:
    """The fitted boundaries. Every one is a quantile of the training rows."""

    trend_low: float
    trend_high: float
    volatility_low: float
    volatility_high: float
    fitted_rows: int
    fitted_on: str = "train"

    def as_dict(self) -> dict[str, Any]:
        return {
            "trend_low": self.trend_low,
            "trend_high": self.trend_high,
            "volatility_low": self.volatility_low,
            "volatility_high": self.volatility_high,
            "fitted_rows": self.fitted_rows,
            "fitted_on": self.fitted_on,
            "method": (
                "quantiles of the training segment: trend at the 33rd and 67th "
                "percentiles of ema_spread_10_50, volatility at the 33rd and 67th of "
                "atr_pct_14. Fixed constants would encode one instrument's habits as a "
                "universal fact."
            ),
        }


def _quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile. Deterministic, and no dependency."""
    if not sorted_values:
        raise ValueError("cannot take a quantile of nothing")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def fit_cuts(
    rows: list[dict[str, float | None]], *, low: float = 0.33, high: float = 0.67
) -> RegimeCuts:
    """Fit the four boundaries from training rows.

    Raises rather than falling back to constants when there is not enough data:
    a regime model with invented thresholds is worse than no regime model,
    because its output looks identical to a fitted one's.
    """
    trend = sorted(float(v) for r in rows if (v := r.get(TREND_FEATURE)) is not None)
    volatility = sorted(float(v) for r in rows if (v := r.get(VOLATILITY_FEATURE)) is not None)
    if len(trend) < 30 or len(volatility) < 30:
        raise ValueError(
            f"{len(trend)} trend and {len(volatility)} volatility readings: too few to "
            "estimate a quantile anyone should act on. Refused rather than defaulted, "
            "because invented thresholds produce output indistinguishable from fitted "
            "ones."
        )
    return RegimeCuts(
        trend_low=_quantile(trend, low),
        trend_high=_quantile(trend, high),
        volatility_low=_quantile(volatility, low),
        volatility_high=_quantile(volatility, high),
        fitted_rows=min(len(trend), len(volatility)),
    )


class RegimeModel(BaseModel):
    """Which of five regimes this bar is in, or UNKNOWN."""

    kind = "classifier"

    def __init__(
        self,
        *,
        version: str = "1.0",
        feature_version: str,
        cuts: RegimeCuts | None = None,
        minimum_confidence: float = 0.15,
        max_feature_age: timedelta | None = None,
        dataset_version: str | None = None,
        dataset_fingerprint: str | None = None,
        trained_at: datetime | None = None,
    ) -> None:
        super().__init__(
            ModelIdentity(
                key="regime",
                version=version,
                kind=self.kind,
                feature_version=feature_version,
                dataset_version=dataset_version,
                dataset_fingerprint=dataset_fingerprint,
                trained_at=trained_at,
            ),
            FeatureContract(features=REQUIRED, feature_version=feature_version, lookback=50),
            max_feature_age=max_feature_age,
        )
        self.cuts = cuts
        self.minimum_confidence = minimum_confidence

    @property
    def fitted(self) -> bool:
        return self.cuts is not None

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
        assert self.cuts is not None  # `fitted` gated this
        trend = float(features[TREND_FEATURE])  # type: ignore[arg-type]
        volatility = float(features[VOLATILITY_FEATURE])  # type: ignore[arg-type]
        cuts = self.cuts

        label, confidence = self._classify(trend, volatility, cuts)
        if confidence < self.minimum_confidence:
            label, confidence = Regime.unknown, confidence

        return Prediction(
            identity=self.identity,
            at=at,
            symbol=symbol,
            timeframe=timeframe,
            status=PredictionStatus.ok,
            value=str(label),
            confidence=round(confidence, 6),
            # A regime label is not a probability of anything, so the field
            # stays empty rather than borrowing the confidence.
            probability=None,
            calibrated=False,
            label_definition=(
                "a rule over two fitted quantile boundaries; see the model's cuts. "
                "Not a forecast: it describes the bar it was given."
            ),
            features_at=features_at,
            input_digest=input_digest,
            metadata={
                "trend": trend,
                "volatility": volatility,
                "cuts": cuts.as_dict(),
                "minimum_confidence": self.minimum_confidence,
            },
        )

    def _classify(self, trend: float, volatility: float, cuts: RegimeCuts) -> tuple[Regime, float]:
        """The table in the module docstring, plus a distance-based confidence."""
        span = max(cuts.volatility_high - cuts.volatility_low, 1e-12)
        trend_span = max(cuts.trend_high - cuts.trend_low, 1e-12)

        if volatility > cuts.volatility_high:
            margin = (volatility - cuts.volatility_high) / span
            return Regime.high_volatility, _confidence(margin)
        if volatility < cuts.volatility_low:
            if cuts.trend_low <= trend <= cuts.trend_high:
                margin = (cuts.volatility_low - volatility) / span
                return Regime.low_volatility, _confidence(margin)

        if trend > cuts.trend_high:
            return Regime.trending_up, _confidence((trend - cuts.trend_high) / trend_span)
        if trend < cuts.trend_low:
            return Regime.trending_down, _confidence((cuts.trend_low - trend) / trend_span)

        # Inside both bands. Confidence rises the closer the trend sits to the
        # middle of the band, because that is what makes RANGING the right word.
        middle = (cuts.trend_low + cuts.trend_high) / 2.0
        distance = abs(trend - middle) / (trend_span / 2.0)
        return Regime.ranging, _confidence(1.0 - distance)

    def explanation(self, features: Mapping[str, float | None]) -> list[dict[str, Any]]:
        """The two inputs and where each sits against its fitted boundaries.

        Honest by construction: these are the only two numbers the model reads,
        so naming them cannot overstate what influenced the answer.
        """
        if self.cuts is None:
            return []
        return [
            {
                "feature": TREND_FEATURE,
                "value": features.get(TREND_FEATURE),
                "low": self.cuts.trend_low,
                "high": self.cuts.trend_high,
            },
            {
                "feature": VOLATILITY_FEATURE,
                "value": features.get(VOLATILITY_FEATURE),
                "low": self.cuts.volatility_low,
                "high": self.cuts.volatility_high,
            },
        ]

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "classes": [str(r) for r in Regime],
            "cuts": self.cuts.as_dict() if self.cuts else None,
            "minimum_confidence": self.minimum_confidence,
            "methodology": (
                "two dimensionless features cut at training-set quantiles. Volatility "
                "wins at the top of its range: a market moving violently in one "
                "direction is more usefully described as violent than as trending."
            ),
        }


def _confidence(margin: float) -> float:
    """Squash a boundary distance into [0, 1] without pretending it is a probability."""
    return max(0.0, min(1.0, margin))
