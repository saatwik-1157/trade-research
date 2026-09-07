"""The AI layer: three model families, one prediction shape, no authority.

    features (L23)
        |
        v
    BaseModel.predict          the gate: fitted? version? complete? fresh?
        |
   +----+----+----------+
   |         |          |
 regime  probability  anomaly
   |         |          |
   +----+----+----------+
        |
     Prediction         one shape, and a refusal carries no value
        |
        v
    app/execution/ai.py        the seat: AiVerdict, which can only decline
        |
        v
    RiskEngine -> sizing -> OMS -> adapter      unchanged, and authoritative

**Nothing in this package can act.** It imports no order manager, no broker
adapter, no risk engine, no position manager and no sizing calculator, and a
test parses every module to keep it that way. The dependency runs one way:
`app.execution` knows about `app.ai`, and `app.ai` knows nothing about
`app.execution`. That direction is deliberate — the reverse is what produced the
import cycle L22 had to break.

**Nothing here trains.** Section 46: level 25 owns training. `fit_logistic`,
`fit_cuts` and `fit_baseline` exist for section 37's non-production smoke test
and are called by tests and by an explicit operator action, never at import and
never by a route.

**No model is loaded by default.** The registry starts empty. A deployment with
no model registered answers `MODEL_UNAVAILABLE`, and the AI policy decides what
that means — which, under `AI_REQUIRED`, is no trade.
"""

from app.ai.anomaly import AnomalyModel, AnomalyStatus, RobustBaseline, fit_baseline
from app.ai.base import BaseModel
from app.ai.contract import (
    FeatureContract,
    ModelError,
    ModelIdentity,
    Prediction,
    PredictionStatus,
)
from app.ai.probability import (
    DEFAULT_LABEL_DEFINITION,
    CalibrationReport,
    LogisticCoefficients,
    TradeProbabilityModel,
    fit_logistic,
)
from app.ai.regime import Regime, RegimeCuts, RegimeModel, fit_cuts
from app.ai.registry import ModelRegistry, RegistryError

__all__ = [
    "DEFAULT_LABEL_DEFINITION",
    "AnomalyModel",
    "AnomalyStatus",
    "BaseModel",
    "CalibrationReport",
    "FeatureContract",
    "LogisticCoefficients",
    "ModelError",
    "ModelIdentity",
    "ModelRegistry",
    "Prediction",
    "PredictionStatus",
    "Regime",
    "RegimeCuts",
    "RegimeModel",
    "RegistryError",
    "RobustBaseline",
    "TradeProbabilityModel",
    "fit_baseline",
    "fit_cuts",
    "fit_logistic",
]
