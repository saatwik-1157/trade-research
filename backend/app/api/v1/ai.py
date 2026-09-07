"""The AI layer's surface: what models exist, what they need, and what they said.

**Nothing here executes.** Section 30 and section 54. This router imports no
order manager, no broker adapter, no risk engine and no sizing calculator; a
test parses it to keep that true. `POST /ai/predict` returns a `Prediction` and
places nothing — the path from a prediction to a venue still runs strategy → AI
→ risk → sizing → OMS → adapter, and the AI seat can only decline.

**A prediction endpoint is not unrestricted model execution.** Section 43. The
input is validated, the feature names are checked against the model's own
contract, and a caller cannot ask for a model that is not registered or feed it
a feature set the model was not fitted against. There is no route that loads a
model from a caller-supplied path, and none that runs arbitrary code.

**Training metrics are never presented as trading performance.** Section 44.
Every figure this router serves is labelled with what produced it and over which
period, and `calibrated` is false until calibration was measured — because
section 20 is right that 0.80 means nothing on its own.

**Nothing here promotes or trains.** Section 36 and section 46 of L23 both hold:
level 24 makes models loadable, predictable and describable. There is no PATCH,
no PUT, no DELETE, and `POST /ai/predict` is the only verb.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel as Body
from pydantic import Field
from pydantic import Field as PydanticField
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import artifacts, comparison, eligibility, lifecycle, resolution
from app.ai import registry_service as registry
from app.ai import service as ai_service
from app.ai import strategy_config as ai_strategy_config
from app.ai.anomaly import AnomalyStatus
from app.ai.contract import PredictionStatus
from app.ai.decision import (
    AiIntegrationError,
    AiMode,
    AiPolicy,
    AiStrategyConfig,
    AiThresholds,
    ModelRequirement,
    ScoringMethod,
)
from app.ai.regime import Regime
from app.ai.registry import ModelRegistry, RegistryError
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.datasets.service import build_dataset_loader, build_validation_loader
from app.models.ai import TRAINING_STATUSES, AIModel, ModelPrediction, ModelVersion, TrainingRun
from app.models.ai_integration import AiDecisionRecord, AiStrategyConfiguration
from app.models.ai_registry import ModelDeployment, ModelLifecycleEvent
from app.models.datasets import DatasetRecord
from app.models.monitoring import ModelAlert, ModelMonitoringSnapshot
from app.models.validation import VALIDATION_STATUSES, ValidationRun
from app.monitoring import collect as monitoring_collect
from app.monitoring import config as monitoring_config
from app.monitoring import service as monitoring
from app.monitoring.service import MonitoringService
from app.monitoring.service import _now as monitoring_utcnow
from app.training.config import (
    MAX_CONCURRENT,
    MAX_QUEUED_PER_USER,
    TRAINING_ENGINE_VERSION,
    ModelFamily,
    SplitConfig,
    TrainingConfig,
    TrainingError,
    TrainingStage,
)
from app.training.config import environment as training_environment
from app.training.service import (
    DuplicateTrainingJob,
    TrainingBusy,
    TrainingService,
)
from app.training.service import summarise as training_summary
from app.validation.config import MAX_CONCURRENT as VALIDATION_MAX_CONCURRENT
from app.validation.config import MAX_QUEUED_PER_USER as VALIDATION_MAX_QUEUED_PER_USER
from app.validation.config import (
    VALIDATION_ENGINE_VERSION,
    Thresholds,
    ValidationConfig,
    ValidationStage,
)
from app.validation.config import ValidationError as ValidationConfigError
from app.validation.report import MEANS as REPORT_MEANS
from app.validation.service import (
    DuplicateValidationJob,
    ValidationBusy,
    ValidationService,
)
from app.validation.service import summarise as validation_summary

router = APIRouter(prefix="/ai", tags=["ai"])

_READ = Depends(require_permission(Permission.manage_ai_models))
_PREDICT = Depends(require_permission(Permission.manage_ai_models))
# Training is the heaviest thing a caller can ask this platform to do, and it
# writes a model version. Same permission as the rest of the AI surface, named
# separately so tightening it later is a one-line change rather than a search.
_TRAIN = Depends(require_permission(Permission.manage_ai_models))
# Validation writes no model status and places nothing, but it is the gate a
# candidate is judged by, so requesting one is held to the same permission as
# training rather than to plain read access.
_VALIDATE = Depends(require_permission(Permission.manage_ai_models))
# Changing how a strategy uses AI changes what a running bot does, so it is held
# to the same permission as training and validation rather than to plain writes.
_CONFIGURE = Depends(require_permission(Permission.manage_ai_models))
# L28 §24. Promotion, rollback, emergency stop and retirement change which model
# a running strategy consults, so they sit ABOVE the permission that trains and
# validates one: a trader may produce a candidate and register it; deciding that
# it is the version a scope resolves to is an administrator's.
_PROMOTE = Depends(require_permission(Permission.promote_ai_models))


def _registry(request: Request) -> ModelRegistry:
    registry: ModelRegistry | None = getattr(request.app.state, "ai_models", None)
    if registry is None:  # pragma: no cover - created in main.py at startup
        raise Conflict("no model registry is attached to this deployment")
    return registry


class PredictBody(Body):
    """One inference request. Every field that changes the answer is required.

    `feature_version` is required rather than defaulted: a caller who does not
    know which feature set produced these numbers cannot be given a prediction,
    because the model's whole compatibility check rests on that string.
    """

    model_key: Annotated[str, Field(min_length=1, max_length=64)]
    model_version: Annotated[str, Field(min_length=1, max_length=16)]
    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    timeframe: Annotated[str, Field(min_length=1, max_length=4)]
    feature_version: Annotated[str, Field(min_length=1, max_length=16)]
    features: dict[str, float | None]
    at: datetime | None = None
    features_at: datetime | None = None


@router.get("/models", summary="Every model this deployment can answer with")
async def list_models(
    request: Request, db: AsyncSession = Depends(get_db), user: User = _READ
) -> dict[str, Any]:
    registry = _registry(request)
    loaded = registry.describe()
    recorded = list((await db.scalars(select(ModelVersion))).all())
    return {
        "items": loaded,
        "counts": {
            "loaded": len(loaded),
            "fitted": sum(1 for m in loaded if m["fitted"]),
            "recorded_versions": len(recorded),
            "promoted": sum(1 for v in recorded if v.status == "promoted"),
        },
        "authority": (
            "advisory. No model here can place, size or approve an order, raise a risk "
            "limit, modify a position or enable live trading."
        ),
        "note": (
            "a model with no fitted parameters answers MODEL_UNAVAILABLE rather than a "
            "default. Under AI_REQUIRED that means no trade."
        ),
    }


@router.get("/models/versions", summary="Recorded model versions and their provenance")
async def list_versions(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    rows = list(
        (
            await db.scalars(
                select(ModelVersion).order_by(ModelVersion.created_at.desc()).limit(limit)
            )
        ).all()
    )
    models = {row.id: row for row in (await db.scalars(select(AIModel))).all()}
    return {
        "items": [ai_service.summarise_version(v, models.get(v.model_id)) for v in rows],
        "note": (
            "a version is written as a draft. Promotion is level 28's, and the database "
            "refuses to promote one that cannot say which feature set, dataset and "
            "parameters produced it."
        ),
    }


@router.get("/models/{model_key}", summary="One model: its contract and its parameters")
async def get_model(
    model_key: str,
    version: str | None = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
    user: User = _READ,
) -> dict[str, Any]:
    registry = _registry(request)
    try:
        model = registry.get(model_key, version) if version else registry.latest(model_key)
    except RegistryError as exc:
        raise NotFound(str(exc)) from exc
    return {
        **model.describe(),
        "versions": registry.versions(model_key),
        "resolution": (
            "an explicit version was requested"
            if version
            else "the latest registered "
            "version was used because none was named -- a running bot should name one"
        ),
    }


@router.get("/vocabulary", summary="What each model family can say")
async def vocabulary(user: User = _READ) -> dict[str, Any]:
    return {
        "prediction_statuses": {
            str(PredictionStatus.ok): "a value was computed",
            str(PredictionStatus.insufficient_data): "the inputs were valid but too thin",
            str(PredictionStatus.model_input_error): (
                "a required feature was absent or null. Nothing is substituted."
            ),
            str(PredictionStatus.feature_version_mismatch): (
                "the model was fitted against a different feature set version"
            ),
            str(PredictionStatus.stale_features): "the features were older than the model accepts",
            str(PredictionStatus.model_unavailable): "there is no fitted model to ask",
        },
        "regime_classes": [str(r) for r in Regime],
        "anomaly_classes": [str(s) for s in AnomalyStatus],
        "rules": [
            "a refusal carries no value; a Prediction cannot be both",
            "a probability is P(a named label), never P(profit)",
            "calibrated is false until calibration has been measured",
            "an anomaly score is not a probability and not a severity",
            "a rare bar is not a bad bar; this layer labels and never vetoes",
        ],
    }


@router.post("/predict", summary="One prediction. Places nothing.")
async def predict(
    body: PredictBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _PREDICT,
) -> dict[str, Any]:
    registry = _registry(request)
    try:
        model = registry.get(body.model_key, body.model_version)
    except RegistryError as exc:
        raise NotFound(str(exc)) from exc

    unexpected = sorted(set(body.features) - set(model.contract.features))
    if unexpected:
        raise ValidationFailed(
            f"{model.identity.key} does not read {', '.join(unexpected)}. Sending a "
            "feature a model was not fitted on is refused rather than ignored, because "
            "a caller who thinks it mattered should be told that it did not."
        )

    at = body.at or datetime.now(UTC).replace(tzinfo=None)
    prediction = model.predict(
        body.features,
        at=at,
        symbol=body.symbol,
        timeframe=body.timeframe,
        feature_version=body.feature_version,
        features_at=body.features_at,
    )

    return {
        **prediction.as_dict(),
        "explanation": model.explanation(body.features) if prediction.ok else [],
        "note": (
            "advisory. This route returns a prediction and places nothing: the path to a "
            "venue runs strategy -> AI -> risk -> sizing -> OMS -> adapter, and the AI "
            "seat can only decline."
        ),
    }


@router.get("/predictions", summary="What models have said, refusals included")
async def list_predictions(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    rows = list(
        (
            await db.scalars(
                select(ModelPrediction).order_by(ModelPrediction.predicted_at.desc()).limit(limit)
            )
        ).all()
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {
        "items": [
            {
                "id": row.id,
                "prediction_key": row.prediction_key,
                "model_version_id": row.model_version_id,
                "predicted_at": row.predicted_at.isoformat(),
                "features_at": row.features_at.isoformat() if row.features_at else None,
                "timeframe": row.timeframe,
                "status": row.status,
                "prediction": row.prediction,
                "confidence": str(row.confidence) if row.confidence is not None else None,
                "feature_version": row.feature_version,
                "detail": row.detail,
            }
            for row in rows
        ],
        "by_status": dict(sorted(counts.items())),
        "note": (
            "refusals are rows too. A model that scored 3% of bars very well is a "
            "different thing from one that scored all of them, and a missing row would "
            "make the two look identical."
        ),
    }


# ======================================================= training (L25)
#
# On this router rather than a new one: section 28 says follow the project's
# API conventions and not to create duplicate routers, and training is the AI
# layer's own subject. The paths are `/v1/ai/training/...`.


class TrainingJobBody(Body):
    """One training request. Every field that changes the fit is here.

    There is no `dataset` field meaning "whatever is current": section 6
    forbids it, and a job that could not name its data could not be reproduced.
    """

    family: str
    dataset_key: Annotated[str, Field(min_length=1, max_length=64)]
    dataset_version: Annotated[str, Field(min_length=1, max_length=16)] = "1"
    model_version: Annotated[str, Field(min_length=1, max_length=16)]
    features: list[str] = Field(default_factory=list)
    train_fraction: float = Field(default=0.70, gt=0, lt=1)
    validation_fraction: float = Field(default=0.15, ge=0, lt=1)
    walk_forward_folds: int = Field(default=4, ge=2, le=20)
    random_seed: int = Field(default=42, ge=0)
    iterations: int = Field(default=400, ge=1, le=100_000)
    learning_rate: float = Field(default=0.1, gt=0, le=10)
    l2: float = Field(default=0.01, ge=0)
    class_weight: str = "none"
    early_stopping_patience: int = Field(default=0, ge=0)
    minimum_rows: int = Field(default=200, ge=50)
    notes: Annotated[str, Field(max_length=500)] = ""


def _training(request: Request) -> TrainingService:
    service: TrainingService | None = getattr(request.app.state, "training", None)
    if service is None:  # pragma: no cover - created in main.py at startup
        raise Conflict("no training service is attached to this deployment")
    return service


@router.get(
    "/training/engine", summary="What the training engine guarantees, and what it will not do"
)
async def training_engine(user: User = _READ) -> dict[str, Any]:
    return {
        "training_engine_version": TRAINING_ENGINE_VERSION,
        "families": [str(f) for f in ModelFamily],
        "stages": [str(s) for s in TrainingStage],
        "statuses": list(TRAINING_STATUSES),
        "environment": training_environment(),
        "guarantees": [
            "the dataset is locked by fingerprint and re-checked after the fit; a "
            "dataset that moved under a running job fails the job",
            "preprocessing is fitted on the training segment only, by API shape",
            "a baseline is fitted first and the candidate is reported beside it",
            "early stopping reads the validation segment; the test segment is never "
            "consulted for any decision",
            "ML metrics and economic metrics are reported separately and never merged",
            "cancellation is cooperative: a cancelled job stops at a boundary and is "
            "recorded as cancelled, never as trained",
        ],
        "does_not": [
            "promote a model, replace an active one, or enable live trading",
            "reach a strategy, the risk engine, position sizing, the OMS or a broker",
            "accept arbitrary code, a model path, or a dataset path from a caller",
            "mark a job completed when it failed or was cancelled",
            "resample a holdout, or resample anything at all - class weighting is used "
            "instead, because duplicating rows in a time series creates the same bar "
            "twice at one timestamp",
        ],
        "handoff": (
            "a successful job ends at validation_pending and writes a DRAFT model "
            "version. L26 validates it and L28 promotes it; neither is this level's."
        ),
        "limits": {
            "max_concurrent": MAX_CONCURRENT,
            "max_queued_per_user": MAX_QUEUED_PER_USER,
        },
    }


@router.post(
    "/training/jobs", summary="Queue a training job. Trains a candidate, promotes nothing."
)
async def create_training_job(
    body: TrainingJobBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _TRAIN,
) -> dict[str, Any]:
    try:
        family = ModelFamily(body.family)
    except ValueError as exc:
        raise ValidationFailed(
            f"unknown model family {body.family!r}; this engine trains "
            f"{', '.join(f.value for f in ModelFamily)}. A family is refused rather than "
            "guessed, because training the wrong one silently is worse than not training."
        ) from exc

    try:
        config = TrainingConfig(
            family=family,
            dataset_key=body.dataset_key,
            dataset_version=body.dataset_version,
            model_version=body.model_version,
            features=tuple(body.features),
            split=SplitConfig(
                train=body.train_fraction,
                validation=body.validation_fraction,
                walk_forward_folds=body.walk_forward_folds,
            ),
            random_seed=body.random_seed,
            iterations=body.iterations,
            learning_rate=body.learning_rate,
            l2=body.l2,
            class_weight=body.class_weight,
            early_stopping_patience=body.early_stopping_patience,
            minimum_rows=body.minimum_rows,
            notes=body.notes,
        )
    except TrainingError as exc:
        raise ValidationFailed(str(exc)) from exc

    record = await db.scalar(
        select(DatasetRecord).where(
            DatasetRecord.key == body.dataset_key, DatasetRecord.version == body.dataset_version
        )
    )
    if record is None:
        raise NotFound(
            f"no dataset {body.dataset_key} v{body.dataset_version}. Training names the "
            "data it locks onto; there is no option to use whatever is current."
        )
    if record.status != "READY":
        raise ValidationFailed(
            f"dataset {body.dataset_key} v{body.dataset_version} is {record.status}, not "
            f"READY: {record.blocked_reason}. L23 marks a dataset READY only when every "
            "leakage check passes, and training on one that did not would make the whole "
            "check ceremonial."
        )

    service = _training(request)
    loader = build_dataset_loader(record)
    try:
        row = await service.queue(db, config, user_id=user.id, loader=loader)
    except (TrainingBusy, DuplicateTrainingJob) as exc:
        raise Conflict(str(exc)) from exc
    except TrainingError as exc:
        raise ValidationFailed(str(exc)) from exc

    return {
        **training_summary(row),
        "note": (
            "queued. Training runs in the background and produces a CANDIDATE: a draft "
            "model version and a job that ends at validation_pending. Nothing is "
            "promoted, and no active model is touched."
        ),
    }


@router.get("/training/jobs", summary="Training jobs, newest first")
async def list_training_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    rows = list(
        (
            await db.scalars(
                select(TrainingRun).order_by(TrainingRun.created_at.desc()).limit(limit)
            )
        ).all()
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {
        "items": [training_summary(row) for row in rows],
        "by_status": dict(sorted(counts.items())),
        "note": (
            "validation_pending is where a SUCCESSFUL run ends. It means a candidate "
            "exists, not that a model is approved for trading."
        ),
    }


@router.get("/training/jobs/{job_id}", summary="One training job")
async def get_training_job(
    job_id: str, db: AsyncSession = Depends(get_db), user: User = _READ
) -> dict[str, Any]:
    row = await db.get(TrainingRun, job_id)
    if row is None:
        raise NotFound(f"no training job {job_id}")
    return {**training_summary(row), "config": (row.params or {}).get("config")}


@router.get(
    "/training/jobs/{job_id}/metrics", summary="What the run measured, ML and economic apart"
)
async def get_training_metrics(
    job_id: str, db: AsyncSession = Depends(get_db), user: User = _READ
) -> dict[str, Any]:
    row = await db.get(TrainingRun, job_id)
    if row is None:
        raise NotFound(f"no training job {job_id}")
    return {
        "job_id": job_id,
        "status": row.status,
        "metrics": row.metrics or {},
        "note": (
            "ML metrics and economic metrics are separate and are never merged. A model "
            "can score well on the first and lose money on the second, and this "
            "repository has measured exactly that."
        ),
    }


@router.post(
    "/training/jobs/{job_id}/cancel", summary="Ask a running job to stop at the next boundary"
)
async def cancel_training_job(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _TRAIN,
) -> dict[str, Any]:
    row = await db.get(TrainingRun, job_id)
    if row is None:
        raise NotFound(f"no training job {job_id}")
    stopped = await _training(request).cancel(job_id)
    return {
        "job_id": job_id,
        "cancelling": stopped,
        "status": row.status,
        "note": (
            "cooperative. The job checks between stages and between gradient iterations, "
            "so it stops at a boundary with its record intact and is marked cancelled -- "
            "never as trained."
            if stopped
            else "this job is not running in this process, so there was nothing to stop."
        ),
    }


# ============================================================== validation
#
# Level 26. Reads a candidate, writes a `validation_runs` row, and concludes.
# There is no route here that promotes, activates or deploys anything: a PASS
# makes a candidate eligible for CONSIDERATION by the model registry, and the
# registry is level 28's. The verbs are POST to queue, POST to cancel, and GET.


class ThresholdsBody(Body):
    """Section 25: every threshold is a choice, so every one is settable.

    Defaults come from `Thresholds` rather than being repeated here, so there
    is one place a bar is defined and the report can quote what it used.
    """

    minimum_samples: Annotated[int, Field(ge=20, le=100_000)] | None = None
    minimum_trades: Annotated[int, Field(ge=5, le=10_000)] | None = None
    minimum_walk_forward_windows: Annotated[int, Field(ge=2, le=20)] | None = None
    minimum_auc: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    maximum_calibration_error: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    minimum_baseline_improvement: Annotated[float, Field(ge=0.0, le=10.0)] | None = None
    minimum_profit_factor: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    maximum_drawdown_ratio: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    threshold_probe: Annotated[float, Field(ge=0.0, le=0.5)] | None = None
    cost_probe_multiplier: Annotated[float, Field(ge=1.0, le=10.0)] | None = None
    minimum_stable_share: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    permutations: Annotated[int, Field(ge=20, le=5_000)] | None = None
    significance_alpha: Annotated[float, Field(gt=0.0, lt=1.0)] | None = None
    candidates_tried: Annotated[int, Field(ge=1, le=10_000)] | None = None
    maximum_train_test_gap: Annotated[float, Field(ge=0.0, le=10.0)] | None = None

    def merged(self) -> Thresholds:
        stated = {k: v for k, v in self.model_dump().items() if v is not None}
        return Thresholds(**stated)


class ValidationBody(Body):
    """One validation run. Every version is named; there is no `latest`."""

    model_version_id: Annotated[str, Field(min_length=1, max_length=36)]
    dataset_key: Annotated[str, Field(min_length=1, max_length=64)]
    dataset_version: Annotated[str, Field(min_length=1, max_length=16)]
    decision_threshold: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.5
    thresholds: ThresholdsBody | None = None
    notes: Annotated[str, Field(max_length=1000)] = ""


def _validation(request: Request) -> ValidationService:
    service: ValidationService | None = getattr(request.app.state, "validation", None)
    if service is None:  # pragma: no cover - created in main.py at startup
        raise Conflict("no validation service is attached to this deployment")
    return service


@router.get("/validation/engine", summary="What validation guarantees, and what it will not do")
async def validation_engine(user: User = _READ) -> dict[str, Any]:
    return {
        "validation_engine_version": VALIDATION_ENGINE_VERSION,
        "stages": [str(s) for s in ValidationStage],
        "statuses": list(VALIDATION_STATUSES),
        "verdicts": {str(verdict): text for verdict, text in REPORT_MEANS.items()},
        "severities": {
            "PASS": "the check was evaluated and met",
            "WARNING": "met, with a caveat that constrains how the result may be read",
            "FAIL": "evaluated and not met",
            "BLOCKED": (
                "could not be evaluated. Never reported as a pass and never as a "
                "failure: both would be claims about the model the evidence does not "
                "support."
            ),
        },
        "default_thresholds": Thresholds().as_dict(),
        "guarantees": [
            "there is no score. The verdict is the most severe check, because a "
            "weighted composite can always be tuned until it hides the one that mattered",
            "every figure is measured on the final test segment, which training never saw",
            "the economic figures come from tools/rule_backtest.simulate -- the same "
            "engine every measured result in this repository came from -- with the "
            "spread charged and entry at the next bar's open",
            "significance is a permutation null over the model's own predictions, "
            "Bonferroni-corrected for the number of candidates tried",
            "preprocessing is applied with the parameters the model was fitted with; "
            "nothing is refitted on the holdout",
            "the dataset is locked by fingerprint and re-checked at the end; a dataset "
            "that moved under a running job blocks the report",
        ],
        "does_not": [
            "promote, activate, deploy or retire a model",
            "write model_versions.status, or any column an execution path reads",
            "place, size or approve an order, or enable live trading",
            "accept arbitrary code, a model path or a dataset path from a caller",
            "fabricate a metric, hardcode a verdict, or report a figure it did not measure",
        ],
        "handoff": (
            "a PASS or CONDITIONAL verdict makes a candidate ELIGIBLE FOR CONSIDERATION "
            "by the model registry. Promotion is level 28's and is a human decision."
        ),
        "limits": {
            "max_concurrent": VALIDATION_MAX_CONCURRENT,
            "max_queued_per_user": VALIDATION_MAX_QUEUED_PER_USER,
        },
    }


@router.post("/validation/runs", summary="Queue a validation run. Concludes; promotes nothing.")
async def create_validation_run(
    body: ValidationBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _VALIDATE,
) -> dict[str, Any]:
    version = await db.get(ModelVersion, body.model_version_id)
    if version is None:
        raise NotFound(
            f"no model version {body.model_version_id}. Validation names its candidate "
            "exactly; there is no option to validate whatever is current."
        )

    record = await db.scalar(
        select(DatasetRecord).where(
            DatasetRecord.key == body.dataset_key,
            DatasetRecord.version == body.dataset_version,
        )
    )
    if record is None:
        raise NotFound(
            f"no dataset {body.dataset_key} v{body.dataset_version}. A report against "
            "data that cannot be named is not re-checkable."
        )
    if record.status != "READY":
        raise ValidationFailed(
            f"dataset {body.dataset_key} v{body.dataset_version} is {record.status}, not "
            f"READY: {record.blocked_reason}. Validating against data the pipeline itself "
            "refused would make the verdict describe the data rather than the model."
        )

    training_run = await db.scalar(
        select(TrainingRun).where(TrainingRun.model_version_id == version.id)
    )

    try:
        config = ValidationConfig(
            model_version_id=version.id,
            training_job_id=training_run.id if training_run else None,
            thresholds=(body.thresholds or ThresholdsBody()).merged(),
            decision_threshold=body.decision_threshold,
            notes=body.notes,
        )
    except ValidationConfigError as exc:
        raise ValidationFailed(str(exc)) from exc

    service = _validation(request)
    loader = build_validation_loader(record)
    try:
        row = await service.queue(
            db,
            config,
            loader=loader,
            user_id=user.id,
            dataset_id=record.id,
            training_run_id=training_run.id if training_run else None,
        )
    except (ValidationBusy, DuplicateValidationJob) as exc:
        raise Conflict(str(exc)) from exc
    except ValidationConfigError as exc:
        raise ValidationFailed(str(exc)) from exc

    return {
        **validation_summary(row),
        "note": (
            "queued. Validation runs in the background and produces a REPORT: a list of "
            "named verdicts and an overall one. It changes no model's status and nothing "
            "downstream reads its result automatically."
        ),
    }


@router.get("/validation/runs", summary="Validation runs, newest first")
async def list_validation_runs(
    model_version_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    statement = select(ValidationRun).order_by(ValidationRun.created_at.desc()).limit(limit)
    if model_version_id:
        statement = statement.where(ValidationRun.model_version_id == model_version_id)
    rows = list((await db.scalars(statement)).all())
    by_verdict: dict[str, int] = {}
    for row in rows:
        if row.verdict:
            by_verdict[row.verdict] = by_verdict.get(row.verdict, 0) + 1
    return {
        "items": [validation_summary(row) for row in rows],
        "by_verdict": dict(sorted(by_verdict.items())),
        "note": (
            "BLOCKED is not FAIL. A blocked run means a check could not be evaluated, so "
            "no conclusion about the candidate may be drawn from it."
        ),
    }


@router.get("/validation/runs/{run_id}", summary="One validation run")
async def get_validation_run(
    run_id: str, db: AsyncSession = Depends(get_db), user: User = _READ
) -> dict[str, Any]:
    row = await db.get(ValidationRun, run_id)
    if row is None:
        raise NotFound(f"no validation run {run_id}")
    return {**validation_summary(row), "config": row.config}


@router.get(
    "/validation/runs/{run_id}/report", summary="The full report: every check and its evidence"
)
async def get_validation_report(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    row = await db.get(ValidationRun, run_id)
    if row is None:
        raise NotFound(f"no validation run {run_id}")
    if row.report is None:
        return {
            "run_id": run_id,
            "status": row.status,
            "report": None,
            "note": (
                f"this run is {row.status} and has produced no report. An absent report "
                "is not a passing one."
            ),
        }
    return {"run_id": run_id, "status": row.status, "report": row.report}


@router.post(
    "/validation/runs/{run_id}/cancel", summary="Ask a running validation to stop at a boundary"
)
async def cancel_validation_run(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _VALIDATE,
) -> dict[str, Any]:
    row = await db.get(ValidationRun, run_id)
    if row is None:
        raise NotFound(f"no validation run {run_id}")
    stopped = await _validation(request).cancel(run_id)
    return {
        "run_id": run_id,
        "cancelling": stopped,
        "status": row.status,
        "note": (
            "cooperative. The job checks between stages, so it stops at a boundary and is "
            "recorded as cancelled -- never as a verdict."
            if stopped
            else "this run is not executing in this process, so there was nothing to stop."
        ),
    }


# ==================================================== strategy integration
#
# Level 27. Reads and writes how a strategy uses AI, and serves the decision
# journal. There is no route here that places, sizes or approves anything, and
# no mode a caller can set that would let one exist: the strongest thing any
# configuration can do is make the AI layer decline a signal the strategy
# produced.


class ModelRefBody(Body):
    """One model a strategy depends on. Named exactly; there is no `latest`."""

    key: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[str, Field(min_length=1, max_length=16)]


class AiConfigBody(Body):
    """A strategy's AI configuration. Section 38, explicit throughout.

    Every field a caller can set is a threshold, a mode or a named model. There
    is deliberately no field for a model PATH, a formula, an expression or a
    code fragment — §41 — and `scoring_method` is an enum rather than a string
    for the same reason.
    """

    mode: Literal["AI_DISABLED", "AI_ADVISORY", "AI_FILTER", "AI_SCORING"] = "AI_DISABLED"
    policy: Literal["AI_REQUIRED", "AI_OPTIONAL"] = "AI_OPTIONAL"
    account_id: Annotated[str, Field(max_length=36)] | None = None
    required_models: list[ModelRefBody] = PydanticField(default_factory=list)
    optional_models: list[ModelRefBody] = PydanticField(default_factory=list)
    feature_version: Annotated[str, Field(max_length=16)] = ""
    minimum_probability: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    maximum_anomaly_score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    allowed_regimes: list[Annotated[str, Field(max_length=32)]] = PydanticField(
        default_factory=list
    )
    minimum_expected_return: float | None = None
    maximum_latency_ms: Annotated[int, Field(gt=0, le=60_000)] = 2000
    scoring_method: Literal["minimum", "weighted", "product"] = "minimum"
    ai_weight: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    minimum_combined_score: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    enabled: bool = True
    notes: Annotated[str, Field(max_length=1000)] = ""

    def to_config(self, strategy_key: str) -> AiStrategyConfig:
        return AiStrategyConfig(
            strategy_key=strategy_key,
            mode=AiMode(self.mode),
            policy=AiPolicy(self.policy),
            models=tuple(
                ModelRequirement(key=m.key, version=m.version, optional=False)
                for m in self.required_models
            )
            + tuple(
                ModelRequirement(key=m.key, version=m.version, optional=True)
                for m in self.optional_models
            ),
            thresholds=AiThresholds(
                minimum_probability=self.minimum_probability,
                maximum_anomaly_score=self.maximum_anomaly_score,
                allowed_regimes=tuple(self.allowed_regimes),
                minimum_expected_return=self.minimum_expected_return,
                maximum_latency_ms=float(self.maximum_latency_ms),
                scoring_method=ScoringMethod(self.scoring_method),
                ai_weight=self.ai_weight,
                minimum_combined_score=self.minimum_combined_score,
            ),
            feature_version=self.feature_version,
            notes=self.notes,
        )


@router.get(
    "/integration", summary="What the AI layer may do to a strategy signal, and what it may not"
)
async def integration_contract(user: User = _READ) -> dict[str, Any]:
    return {
        "modes": {
            "AI_DISABLED": (
                "no inference runs at all. The strategy behaves exactly as the "
                "deterministic strategy would -- the baseline every comparison needs, "
                "and the fallback when the AI layer is unavailable."
            ),
            "AI_ADVISORY": (
                "inference runs and is recorded. The signal is unchanged; the decision "
                "is NEUTRAL and is never counted later as agreement."
            ),
            "AI_FILTER": "inference runs and may REJECT the signal. It cannot create one.",
            "AI_SCORING": (
                "the strategy's own score and the AI probability combine by a NAMED "
                "formula, and the result is compared against a threshold. Still a "
                "filter: the combined score is never read as a size or a risk."
            ),
        },
        "policies": {
            "AI_REQUIRED": "the AI must answer. No answer means no trade.",
            "AI_OPTIONAL": (
                "the AI is advice. No answer means the signal proceeds to the risk "
                "engine, which is unchanged and still authoritative."
            ),
        },
        "decisions": ["ACCEPT", "REJECT", "NEUTRAL", "ERROR"],
        "scoring_methods": {
            "minimum": "min(strategy, ai) -- the default, and the only one that cannot "
            "let a confident AI rescue a weak strategy signal",
            "weighted": "(1-w)*strategy + w*ai",
            "product": "strategy * ai -- always the strictest",
        },
        "pipeline": [
            "TRADINGVIEW / STRATEGY",
            "SIGNAL ENGINE",
            "STRATEGY ENGINE",
            "AI STRATEGY FILTER",
            "RISK ENGINE",
            "POSITION SIZING",
            "OMS",
            "BROKER ADAPTER",
            "MT5",
        ],
        "guarantees": [
            "the AI layer runs BEFORE the risk engine and can only decline; there is no "
            "field on its verdict by which it could approve, size or raise anything",
            "a model must be VALIDATED before a strategy may name it, and the version is "
            "exact -- there is no 'latest'",
            "training and inference use one feature engine; a model fitted against a "
            "different feature set version is refused rather than run",
            "at signal time the AI sees the same closed-bar window the strategy saw, and "
            "nothing in the AI layer fetches market data",
            "an out-of-range probability is an inference ERROR, never a confident model, "
            "and it is not clamped into range",
            "every decision is journalled, including the ones that changed nothing",
        ],
        "does_not": [
            "place, modify or cancel an order",
            "size a position, or change a risk percentage",
            "overturn a risk-engine rejection or disengage a kill switch",
            "enable live trading",
            "accept a model path, a formula, a code fragment or an unvalidated version",
            "turn a flat bar into a trade -- the AI layer is never asked about one",
        ],
        "risk_authority": (
            "absolute and unchanged. Strategy = BUY and AI = ACCEPT still reaches the "
            "risk engine, which may reject for daily loss, drawdown, exposure, position "
            "count, margin, an invalid stop, trading disabled or a kill switch. No AI "
            "configuration can affect any of those."
        ),
        "default": (
            "a strategy with no configuration is AI_DISABLED with AI_OPTIONAL, and runs "
            "exactly as the deterministic strategy does."
        ),
    }


@router.get("/integration/models", summary="Model versions a strategy may legitimately name")
async def integration_models(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    rows = await eligibility.eligible_versions(db, limit=limit)
    return {
        "items": rows,
        "eligible": sum(1 for r in rows if r["eligible"]),
        "note": (
            "eligibility comes from L26's validation verdict today. A version with no "
            "validation is not eligible -- not because it is bad, but because nothing "
            "has been established about it."
        ),
    }


@router.get("/integration/strategies", summary="Every configured strategy")
async def list_ai_configs(db: AsyncSession = Depends(get_db), user: User = _READ) -> dict[str, Any]:
    rows = list((await db.scalars(select(AiStrategyConfiguration))).all())
    return {
        "items": [ai_strategy_config.summarise_config(r) for r in rows],
        "note": (
            "a strategy that is not listed here has no AI configuration and runs "
            "exactly as the deterministic strategy does."
        ),
    }


@router.get("/integration/strategies/{strategy_key}", summary="One strategy's AI configuration")
async def get_ai_config(
    strategy_key: str,
    account_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    config = await ai_strategy_config.config_for(db, strategy_key, account_id=account_id)
    return {"strategy_key": strategy_key, "config": config.as_dict()}


@router.post(
    "/integration/strategies/{strategy_key}",
    summary="Set how a strategy uses AI. Configures a filter; grants no authority.",
)
async def set_ai_config(
    strategy_key: str,
    body: AiConfigBody,
    db: AsyncSession = Depends(get_db),
    user: User = _CONFIGURE,
) -> dict[str, Any]:
    try:
        config = body.to_config(strategy_key)
    except AiIntegrationError as exc:
        raise ValidationFailed(str(exc)) from exc

    try:
        row = await ai_strategy_config.save(
            db,
            strategy_key=strategy_key,
            config=config,
            account_id=body.account_id,
            enabled=body.enabled,
            user_id=user.id,
        )
    except AiIntegrationError as exc:
        # §12: a model that is not validated cannot be named. Refused rather
        # than saved-and-ignored, because a configuration that silently drops a
        # model runs a different pipeline from the one it describes.
        raise ValidationFailed(str(exc)) from exc

    return {
        **ai_strategy_config.summarise_config(row),
        "note": (
            "saved. This configures a FILTER. It grants the AI layer no ability to place, "
            "size or approve an order, and the risk engine is unchanged."
        ),
    }


@router.get("/integration/decisions", summary="The AI decision journal, newest first")
async def list_ai_decisions(
    strategy_key: str | None = Query(default=None),
    decision: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    statement = select(AiDecisionRecord).order_by(AiDecisionRecord.created_at.desc()).limit(limit)
    if strategy_key:
        statement = statement.where(AiDecisionRecord.strategy_key == strategy_key)
    if decision:
        statement = statement.where(AiDecisionRecord.decision == decision.upper())
    rows = list((await db.scalars(statement)).all())

    counts: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for row in rows:
        counts[row.decision] = counts.get(row.decision, 0) + 1
        if row.final_outcome:
            outcomes[row.final_outcome] = outcomes.get(row.final_outcome, 0) + 1

    return {
        "items": [ai_strategy_config.summarise(r) for r in rows],
        "by_decision": dict(sorted(counts.items())),
        "by_final_outcome": dict(sorted(outcomes.items())),
        "note": (
            "an ACCEPT means the AI layer did not object. What happened next is in "
            "final_outcome, and risk_vetoed is a common and correct value there."
        ),
    }


# ================================================== the model registry (L28)
#
# The authoritative surface for "which model versions exist, what happened to
# them, and which are eligible for use?".
#
# Reads require `manage_ai_models`; the three verbs that change what a running
# strategy consults — promote, rollback, retire — require `promote_ai_models`,
# which only an administrator has. §24: a trader may produce a candidate and
# register it; deciding that it is the version a scope resolves to is not the
# same decision.


class ScopeBody(Body):
    """Where a deployment applies. §15.

    Omitting an axis means "not restricted on it". An empty string is refused
    by `registry.Scope`, because '' and None would be two spellings of one fact
    and the uniqueness rule would read them as two different scopes.
    """

    strategy_key: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    symbol: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    timeframe: Annotated[str, Field(min_length=1, max_length=4)] | None = None
    environment: Literal["paper", "demo", "live"] = "paper"

    def to_scope(self) -> registry.Scope:
        return registry.Scope(
            strategy_key=self.strategy_key,
            symbol=self.symbol,
            timeframe=self.timeframe,
            environment=self.environment,
        )


class RegisterBody(Body):
    reason: Annotated[str, Field(max_length=1000)] = ""


class ReasonBody(Body):
    """Every lifecycle verb states why. §19."""

    reason: Annotated[str, Field(min_length=3, max_length=1000)]


class DeployBody(ScopeBody):
    reason: Annotated[str, Field(max_length=1000)] = ""
    config: dict[str, Any] | None = None


class PromoteBody(ScopeBody):
    reason: Annotated[str, Field(min_length=3, max_length=1000)]


async def _version_or_404(db: AsyncSession, model_key: str, version: str) -> ModelVersion:
    model = await db.scalar(select(AIModel).where(AIModel.key == model_key))
    if model is None:
        raise NotFound(f"no model {model_key!r}")
    row = await db.scalar(
        select(ModelVersion).where(
            ModelVersion.model_id == model.id,
            ModelVersion.artifact_ref == f"{model_key}:{version}",
        )
    )
    if row is None:
        raise NotFound(
            f"model {model_key} has no version {version!r}. Refused rather than resolved "
            "to the nearest one: a version that is guessed is a model nobody chose."
        )
    return row


def _hub(request: Request) -> Any:
    return getattr(request.app.state, "hub", None)


@router.get("/registry", summary="The model lifecycle, and what the registry will not do")
async def registry_contract(user: User = _READ) -> dict[str, Any]:
    return {
        **lifecycle.describe(),
        "artifact": {
            "storage": (
                "structured JSON on the model version row. L24 chose that deliberately: "
                "these parameters are a handful of floats and keeping them structured "
                "means a version can be inspected, diffed and queried."
            ),
            "integrity": (
                f"a {artifacts.ALGORITHM} digest over the canonical encoding of the "
                "artifact alone -- not the whole row, because metrics and status change "
                "legitimately and the fitted parameters do not. Taken at registration, "
                "re-checked before every load."
            ),
            "security": (
                "nothing is deserialised: there is no pickle, no joblib and no code path "
                "that turns bytes into behaviour. There is no upload route and no path "
                "field, so there is nothing for a path traversal to traverse."
            ),
            "kinds": sorted(artifacts.KNOWN_KINDS),
        },
        "authorization": {
            "read": "manage_ai_models",
            "register": "manage_ai_models",
            "deploy_to_paper": "manage_ai_models",
            "promote": "promote_ai_models (administrator)",
            "rollback": "promote_ai_models (administrator)",
            "retire": "promote_ai_models (administrator)",
        },
        "trading_safety": (
            "`promoted` means the version a scope resolves to. It does not enable live "
            "trading and cannot: this package reads and writes neither TRADING_MODE nor "
            "LIVE_TRADING, and reaches no risk engine, sizer, OMS or broker adapter."
        ),
    }


@router.get("/models/{model_key}/versions", summary="Every version of one model")
async def list_model_versions(
    model_key: str,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    model = await db.scalar(select(AIModel).where(AIModel.key == model_key))
    if model is None:
        raise NotFound(f"no model {model_key!r}")
    rows = list(
        (
            await db.scalars(
                select(ModelVersion)
                .where(ModelVersion.model_id == model.id)
                .order_by(ModelVersion.version.desc())
            )
        ).all()
    )
    deployments = list(
        (
            await db.scalars(
                select(ModelDeployment).where(
                    ModelDeployment.model_key == model_key, ModelDeployment.status == "active"
                )
            )
        ).all()
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {
        "model": model_key,
        "items": [
            {
                **ai_service.summarise_version(row, model),
                "artifact": {
                    "sha256": row.artifact_sha256,
                    "bytes": row.artifact_bytes,
                    "kind": row.artifact_kind,
                },
                "registered_at": row.registered_at.isoformat() if row.registered_at else None,
                "promoted_at": row.promoted_at.isoformat() if row.promoted_at else None,
                "retired_at": row.retired_at.isoformat() if row.retired_at else None,
                "serves_inference": lifecycle.serves_inference(row.status),
            }
            for row in rows
        ],
        "by_status": dict(sorted(counts.items())),
        "active_deployments": [registry.summarise_deployment(d) for d in deployments],
        "note": (
            "nothing is ever deleted. `rejected` and `retired` are terminal states, not "
            "removals: a historical version is needed for audit, backtesting, trade "
            "review and reproducibility."
        ),
    }


@router.get(
    "/models/{model_key}/versions/{version}",
    summary="One version: its lineage, its artifact and its history",
)
async def get_model_version(
    model_key: str,
    version: str,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    row = await _version_or_404(db, model_key, version)
    chain = await registry.lineage(db, row)
    model = await db.scalar(select(AIModel).where(AIModel.key == model_key))
    return {
        **ai_service.summarise_version(row, model),
        "serves_inference": lifecycle.serves_inference(row.status),
        "lineage": chain.as_dict(),
        "artifact": artifacts.verify(row).as_dict(),
        "scope": {"symbol": row.symbol_scope, "timeframe": row.timeframe_scope},
        "history": await registry.history(db, row.id),
    }


@router.post(
    "/models/{model_key}/versions/{version}/register",
    summary="Accept a validated candidate. Gated on L26's verdict and on the artifact.",
)
async def register_model_version(
    model_key: str,
    version: str,
    body: RegisterBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _CONFIGURE,
) -> dict[str, Any]:
    row = await _version_or_404(db, model_key, version)
    try:
        transition = await registry.register(
            db, row, actor_user_id=user.id, reason=body.reason, hub=_hub(request)
        )
    except (registry.RegistryError, lifecycle.LifecycleError) as exc:
        raise ValidationFailed(str(exc)) from exc
    return {
        **transition.as_dict(),
        "note": (
            "registered. The candidate is now eligible for a paper deployment. It is not "
            "active anywhere and nothing resolves to it yet."
        ),
    }


@router.post(
    "/models/{model_key}/versions/{version}/deploy",
    summary="Deploy a registered version to paper trading for a scope",
)
async def deploy_model_version(
    model_key: str,
    version: str,
    body: DeployBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _CONFIGURE,
) -> dict[str, Any]:
    row = await _version_or_404(db, model_key, version)
    try:
        deployment = await registry.deploy_to_paper(
            db,
            row,
            body.to_scope(),
            actor_user_id=user.id,
            reason=body.reason,
            config=body.config,
            hub=_hub(request),
        )
    except (registry.RegistryError, lifecycle.LifecycleError) as exc:
        raise ValidationFailed(str(exc)) from exc
    return {
        **registry.summarise_deployment(deployment),
        "note": (
            "deployed to PAPER. TRADING_MODE and LIVE_TRADING are unchanged, and a paper "
            "deployment is reversible: stopping it returns the version to `registered`."
        ),
    }


@router.post(
    "/models/{model_key}/versions/{version}/promote",
    summary="Make a paper-deployed version the one a scope resolves to. Administrator only.",
)
async def promote_model_version(
    model_key: str,
    version: str,
    body: PromoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _PROMOTE,
) -> dict[str, Any]:
    row = await _version_or_404(db, model_key, version)
    try:
        transition = await registry.promote(
            db,
            row,
            body.to_scope(),
            actor_user_id=user.id,
            reason=body.reason,
            hub=_hub(request),
        )
    except (registry.RegistryError, lifecycle.LifecycleError) as exc:
        raise ValidationFailed(str(exc)) from exc
    return {
        **transition.as_dict(),
        "note": (
            "promoted. This is the version the scope resolves to. It does NOT enable "
            "live trading: TRADING_MODE remains paper, LIVE_TRADING remains false, and "
            "the registry reads neither."
        ),
    }


@router.post(
    "/models/{model_key}/versions/{version}/rollback",
    summary="Withdraw the active version and restore the one it replaced. Administrator only.",
)
async def rollback_model_version(
    model_key: str,
    version: str,
    body: PromoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _PROMOTE,
) -> dict[str, Any]:
    row = await _version_or_404(db, model_key, version)
    try:
        result = await registry.rollback(
            db,
            row,
            body.to_scope(),
            actor_user_id=user.id,
            reason=body.reason,
            hub=_hub(request),
        )
    except (registry.RegistryError, lifecycle.LifecycleError) as exc:
        raise ValidationFailed(str(exc)) from exc
    return result


@router.post(
    "/models/{model_key}/versions/{version}/stop",
    summary="Emergency deactivation. Stops new inference; deletes nothing.",
)
async def stop_model_deployment(
    model_key: str,
    version: str,
    body: PromoteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _PROMOTE,
) -> dict[str, Any]:
    row = await _version_or_404(db, model_key, version)
    try:
        transition = await registry.stop_deployment(
            db,
            row,
            body.to_scope(),
            actor_user_id=user.id,
            reason=body.reason,
            hub=_hub(request),
        )
    except (registry.RegistryError, lifecycle.LifecycleError) as exc:
        raise ValidationFailed(str(exc)) from exc
    return {
        **transition.as_dict(),
        "note": (
            "the deployment is stopped and the version is back to `registered`. Nothing "
            "was deleted, and no strategy configuration was touched: a strategy whose "
            "model is withdrawn gets no model, which under AI_REQUIRED means no trade "
            "and under AI_OPTIONAL means the deterministic strategy proceeds."
        ),
    }


@router.post(
    "/models/{model_key}/versions/{version}/retire",
    summary="Withdraw a version deliberately. Terminal, and never a deletion.",
)
async def retire_model_version(
    model_key: str,
    version: str,
    body: ReasonBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _PROMOTE,
) -> dict[str, Any]:
    row = await _version_or_404(db, model_key, version)
    try:
        transition = await registry.retire(
            db, row, actor_user_id=user.id, reason=body.reason, hub=_hub(request)
        )
    except (registry.RegistryError, lifecycle.LifecycleError) as exc:
        raise ValidationFailed(str(exc)) from exc
    return {
        **transition.as_dict(),
        "note": (
            "retired. The row, its artifact and its history are all kept: audit, "
            "backtesting, trade review, reproducibility and rollback all need them."
        ),
    }


@router.get("/models/{model_key}/deployments", summary="What is deployed where")
async def list_model_deployments(
    model_key: str,
    environment: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    statement = (
        select(ModelDeployment)
        .where(ModelDeployment.model_key == model_key)
        .order_by(ModelDeployment.activated_at.desc())
    )
    if environment:
        statement = statement.where(ModelDeployment.environment == environment)
    rows = list((await db.scalars(statement)).all())
    return {
        "model": model_key,
        "items": [registry.summarise_deployment(r) for r in rows],
        "active": [registry.summarise_deployment(r) for r in rows if r.status == "active"],
    }


@router.get("/models/{model_key}/history", summary="Every lifecycle transition, newest first")
async def model_history(
    model_key: str,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    rows = list(
        (
            await db.scalars(
                select(ModelLifecycleEvent)
                .where(ModelLifecycleEvent.model_key == model_key)
                .order_by(ModelLifecycleEvent.occurred_at.desc())
                .limit(limit)
            )
        ).all()
    )
    return {
        "model": model_key,
        "items": [registry.summarise_event(r) for r in rows],
        "note": "append-only. Nothing in the registry updates or deletes a history row.",
    }


@router.get("/models/{model_key}/resolve", summary="Which version does this scope resolve to?")
async def resolve_model(
    model_key: str,
    strategy_key: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    environment: Literal["paper", "demo", "live"] = Query(default="paper"),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    request_ = resolution.Request(
        model_key=model_key,
        strategy_key=strategy_key,
        symbol=symbol,
        timeframe=timeframe,
        environment=environment,
    )
    try:
        answer = await resolution.resolve(db, request_)
    except resolution.ResolutionError as exc:
        return {
            "request": request_.as_dict(),
            "resolved": None,
            "reason": str(exc),
            "note": (
                "no model resolves here. Under AI_REQUIRED that means no trade, which is "
                "the safe reading; under AI_OPTIONAL the deterministic strategy proceeds."
            ),
        }
    return {"request": request_.as_dict(), "resolved": answer.as_dict()}


@router.get("/models/{model_key}/compare", summary="Two versions, side by side")
async def compare_model_versions(
    model_key: str,
    a: str = Query(..., min_length=1, max_length=16),
    b: str = Query(..., min_length=1, max_length=16),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    left = await _version_or_404(db, model_key, a)
    right = await _version_or_404(db, model_key, b)
    return comparison.compare(left, right)


# ================================================== AI model monitoring (L29)
#
# Reads what monitoring measured, and lets an operator acknowledge an alert.
# There is no route here that retrains, promotes, replaces or deploys anything,
# and no field a caller can set that would change what a model does: §26's rule
# is that monitoring detects and alerts, and the surface reflects that.


class AcknowledgeBody(Body):
    """Acknowledging an alert is not resolving it. Section 24.

    A person has SEEN it; the condition may well still be there. Conflating the
    two would let a dashboard be cleared without anything being fixed, which is
    the failure mode an acknowledge button usually has.
    """

    note: Annotated[str, Field(max_length=500)] = ""


@router.get("/monitoring", summary="What monitoring measures, and what it will not do")
async def monitoring_contract(user: User = _READ) -> dict[str, Any]:
    return monitoring_config.describe()


@router.get("/monitoring/snapshots", summary="Monitoring snapshots, newest first")
async def list_monitoring_snapshots(
    model_key: str | None = Query(default=None),
    model_version_id: str | None = Query(default=None),
    health: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    statement = (
        select(ModelMonitoringSnapshot)
        .order_by(ModelMonitoringSnapshot.created_at.desc())
        .limit(limit)
    )
    if model_key:
        statement = statement.where(ModelMonitoringSnapshot.model_key == model_key)
    if model_version_id:
        statement = statement.where(ModelMonitoringSnapshot.model_version_id == model_version_id)
    if health:
        statement = statement.where(ModelMonitoringSnapshot.health_state == health.upper())
    rows = list((await db.scalars(statement)).all())

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.health_state] = counts.get(row.health_state, 0) + 1
    return {
        "items": [monitoring.summarise_snapshot(r) for r in rows],
        "by_health": dict(sorted(counts.items())),
        "note": (
            "INSUFFICIENT_DATA is not a pass and OFFLINE is not a clean bill of health. "
            "The first says the sample could not support a conclusion; the second says "
            "nothing is deployed."
        ),
    }


@router.get("/monitoring/health", summary="The current health of every deployed version")
async def monitoring_health(
    db: AsyncSession = Depends(get_db), user: User = _READ
) -> dict[str, Any]:
    deployments = await monitoring_collect.active_deployments(db)
    out: list[dict[str, Any]] = []
    for deployment in deployments:
        latest = await db.scalar(
            select(ModelMonitoringSnapshot)
            .where(ModelMonitoringSnapshot.deployment_id == deployment.id)
            .order_by(ModelMonitoringSnapshot.created_at.desc())
            .limit(1)
        )
        out.append(
            {
                "model": deployment.model_key,
                "version": deployment.model_version_label,
                "model_version_id": deployment.model_version_id,
                "environment": deployment.environment,
                "scope": {
                    "strategy_key": deployment.strategy_key,
                    "symbol": deployment.symbol,
                    "timeframe": deployment.timeframe,
                },
                "health": latest.health_state if latest else "INSUFFICIENT_DATA",
                "measured_at": latest.created_at.isoformat()
                if latest and latest.created_at
                else None,
                "sample_count": latest.sample_count if latest else 0,
                "note": (
                    None
                    if latest
                    else "this deployment has never been monitored, which is not a healthy state"
                ),
            }
        )
    return {
        "items": out,
        "deployments": len(deployments),
        "note": (
            "a deployment with no snapshot reports INSUFFICIENT_DATA, never HEALTHY: "
            "never measured and measured-and-fine must not look the same."
        ),
    }


@router.get("/monitoring/alerts", summary="Monitoring alerts, open first")
async def list_monitoring_alerts(
    model_key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    statement = select(ModelAlert).order_by(ModelAlert.last_seen_at.desc()).limit(limit)
    if model_key:
        statement = statement.where(ModelAlert.model_key == model_key)
    if status:
        statement = statement.where(ModelAlert.status == status)
    rows = list((await db.scalars(statement)).all())

    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        if row.status != "resolved":
            by_severity[row.severity] = by_severity.get(row.severity, 0) + 1
    return {
        "items": [monitoring.summarise_alert(r) for r in rows],
        "by_status": dict(sorted(by_status.items())),
        "open_by_severity": dict(sorted(by_severity.items())),
        "note": (
            "one row per condition, not per observation. A condition already open and "
            "inside its cooldown is suppressed rather than repeated, and one that "
            "cleared is resolved with the time it cleared."
        ),
    }


@router.post(
    "/monitoring/alerts/{alert_id}/acknowledge",
    summary="Record that somebody has seen an alert. Does not resolve it.",
)
async def acknowledge_alert(
    alert_id: str,
    body: AcknowledgeBody,
    db: AsyncSession = Depends(get_db),
    user: User = _CONFIGURE,
) -> dict[str, Any]:
    row = await db.get(ModelAlert, alert_id)
    if row is None:
        raise NotFound(f"no alert {alert_id}")
    if row.status == "resolved":
        raise ValidationFailed(
            "this alert already resolved. Acknowledging a resolved alert would record "
            "that somebody saw a condition that is no longer there."
        )
    row.acknowledged_at = monitoring_utcnow()
    row.acknowledged_by_user_id = user.id
    row.status = "acknowledged"
    if body.note:
        row.detail = {**(row.detail or {}), "acknowledgement": body.note}
    await db.commit()
    await db.refresh(row)
    return {
        **monitoring.summarise_alert(row),
        "note": (
            "acknowledged, not resolved. The condition is still whatever it was; this "
            "records that a person has seen it. Monitoring resolves an alert when the "
            "condition itself clears, and never because somebody clicked."
        ),
    }


@router.post(
    "/monitoring/run",
    summary="Run monitoring now over every active deployment. Detects; changes nothing.",
)
async def run_monitoring(
    request: Request,
    model_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = _CONFIGURE,
) -> dict[str, Any]:
    service = MonitoringService(hub=getattr(request.app.state, "hub", None))
    results = await service.run_all(db, model_key=model_key)
    return {
        "runs": [r.as_dict() for r in results],
        "note": (
            "monitoring detects, records and alerts. It never retrains, promotes, "
            "replaces or deploys a model, and never modifies a strategy, a risk limit "
            "or a position size."
        ),
    }
