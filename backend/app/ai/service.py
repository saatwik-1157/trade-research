"""Recording what a model said, and reading what a model is.

The one module in `app/ai/` that touches the database, kept separate so the
models themselves stay pure functions of their inputs — which is what makes
section 24's determinism checkable.

**Every prediction is recorded, including the refusals.** A `MODEL_UNAVAILABLE`
that leaves no row is indistinguishable from a model nobody asked, and the two
have opposite meanings when someone later counts how often the AI layer
answered. Section 39's monitoring preparation needs the denominator.

**Recording is idempotent on `prediction_key`.** Inference is deterministic, so
the same model over the same inputs produces the same id; writing it twice is
one row. The same mechanism `orders.intent_id` uses, and for the same reason.

**Nothing here promotes, trains or replaces a model.** Section 36: level 24
ensures models load, predict, and carry their metadata. `register_version`
writes a `draft`; moving a version to `promoted` is level 28's, and the database
will refuse a promotion that cannot say what it was fitted on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import BaseModel
from app.ai.contract import Prediction, PredictionStatus
from app.models.ai import AIModel, ModelPrediction, ModelVersion

# Keys that must never appear in a model's stored parameters. Section 46: no
# secret goes in an artifact, and the cheapest enforcement is to look.
FORBIDDEN_PARAM_KEYS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "passphrase",
    "login",
)


class AiServiceError(Exception):
    """A model record that cannot be written. Refused rather than half-written."""


def _contains_secret(payload: Any, path: str = "") -> str | None:
    """The first credential-shaped key found, or None. Walks nested structures."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            for banned in FORBIDDEN_PARAM_KEYS:
                if banned in lowered:
                    return f"{path}{key}"
            found = _contains_secret(value, f"{path}{key}.")
            if found:
                return found
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found = _contains_secret(value, f"{path}[{index}].")
            if found:
                return found
    return None


async def register_version(
    db: AsyncSession,
    model: BaseModel,
    *,
    params: dict[str, Any],
    name: str | None = None,
    description: str | None = None,
    code_version: str | None = None,
    metrics: dict[str, Any] | None = None,
    training_period: tuple[datetime, datetime] | None = None,
    validation_period: tuple[datetime, datetime] | None = None,
    test_period: tuple[datetime, datetime] | None = None,
    owner_user_id: str | None = None,
) -> ModelVersion:
    """Record a model version as a **draft**. Never promotes it.

    Idempotent on `(model.key, model.version)`: registering the same version
    twice returns the existing row rather than minting a second identity, on the
    same reasoning `ModelRegistry.register` refuses a duplicate — a caller
    holding "regime v1.0" must be holding the same thing tomorrow.
    """
    secret = _contains_secret(params)
    if secret is not None:
        raise AiServiceError(
            f"the parameters contain a credential-shaped key ({secret}). Section 46: no "
            "secret goes in a model artifact, and a model file is one of the places a "
            "credential is hardest to notice."
        )

    identity = model.identity
    row = await db.scalar(select(AIModel).where(AIModel.key == identity.key))
    if row is None:
        row = AIModel(
            key=identity.key,
            name=name or identity.key.replace("_", " ").title(),
            kind=identity.kind,
            description=description,
            owner_user_id=owner_user_id,
        )
        db.add(row)
        await db.flush()

    # `version` on the table is an integer sequence; the model's own version is
    # a string like "1.0". They are different things and both are kept: the
    # integer orders, the string identifies.
    existing = await db.scalar(
        select(ModelVersion).where(
            ModelVersion.model_id == row.id,
            ModelVersion.artifact_ref == f"{identity.key}:{identity.version}",
        )
    )
    if existing is not None:
        return existing

    sequence = len(
        list((await db.scalars(select(ModelVersion).where(ModelVersion.model_id == row.id))).all())
    )
    version = ModelVersion(
        model_id=row.id,
        version=sequence + 1,
        artifact_ref=f"{identity.key}:{identity.version}",
        features=list(model.contract.features),
        labels={"definition": getattr(model, "label_definition", None)},
        metrics=metrics,
        # Draft, always. A promotion is an operator decision and level 28's
        # subject, and the database refuses one that cannot state its provenance.
        status="draft",
        feature_version=identity.feature_version,
        label_version=identity.label_version,
        dataset_version=identity.dataset_version,
        dataset_fingerprint=identity.dataset_fingerprint,
        preprocessing_version=identity.preprocessing_version,
        code_version=code_version,
        params=params,
        calibration=getattr(getattr(model, "calibration", None), "as_dict", lambda: None)(),
        training_start=training_period[0] if training_period else None,
        training_end=training_period[1] if training_period else None,
        validation_start=validation_period[0] if validation_period else None,
        validation_end=validation_period[1] if validation_period else None,
        test_start=test_period[0] if test_period else None,
        test_end=test_period[1] if test_period else None,
    )
    db.add(version)
    await db.flush()
    return version


async def record(
    db: AsyncSession,
    prediction: Prediction,
    *,
    model_version_id: str,
    signal_id: str | None = None,
    symbol_id: str | None = None,
    horizon: str | None = None,
) -> ModelPrediction:
    """Persist one prediction, refusal included. Idempotent on its key."""
    key = prediction.prediction_id()
    existing = await db.scalar(select(ModelPrediction).where(ModelPrediction.prediction_key == key))
    if existing is not None:
        return existing

    row = ModelPrediction(
        model_version_id=model_version_id,
        signal_id=signal_id,
        symbol_id=symbol_id,
        predicted_at=prediction.at.replace(tzinfo=None) if prediction.at.tzinfo else prediction.at,
        horizon=horizon,
        prediction=(
            {
                "value": prediction.value,
                "probability": prediction.probability,
                "label_definition": prediction.label_definition,
                "calibrated": prediction.calibrated,
            }
            if prediction.ok
            else None
        ),
        confidence=(
            Decimal(str(prediction.confidence)) if prediction.confidence is not None else None
        ),
        prediction_key=key,
        feature_version=prediction.identity.feature_version,
        dataset_version=prediction.identity.dataset_version,
        timeframe=prediction.timeframe,
        status=str(prediction.status),
        features_at=(
            prediction.features_at.replace(tzinfo=None)
            if prediction.features_at and prediction.features_at.tzinfo
            else prediction.features_at
        ),
        detail=prediction.detail or None,
        prediction_metadata=prediction.metadata or None,
    )
    db.add(row)
    await db.flush()
    return row


async def answer_rate(db: AsyncSession, *, model_version_id: str) -> dict[str, Any]:
    """How often this version answered, and how often it refused, and why.

    Section 39 asks for enough metadata for level 29's monitoring. This is the
    denominator that makes the rest meaningful: a model with a wonderful hit
    rate on the 3% of bars it was willing to score is a different thing from one
    that scored every bar.
    """
    rows = list(
        (
            await db.scalars(
                select(ModelPrediction).where(ModelPrediction.model_version_id == model_version_id)
            )
        ).all()
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    answered = counts.get(str(PredictionStatus.ok), 0)
    return {
        "predictions": len(rows),
        "answered": answered,
        "refused": len(rows) - answered,
        "answer_rate": round(answered / len(rows), 6) if rows else None,
        "by_status": dict(sorted(counts.items())),
        "note": (
            "refusals are recorded as refusals. A model that scored 3% of bars very well "
            "is a different thing from one that scored all of them, and a missing row "
            "would make the two look identical."
        ),
    }


def summarise_version(row: ModelVersion, model_row: AIModel | None = None) -> dict[str, Any]:
    """One model version as an API row."""
    return {
        "id": row.id,
        "model": model_row.key if model_row else None,
        "model_id": row.model_id,
        "sequence": row.version,
        "artifact_ref": row.artifact_ref,
        "status": row.status,
        "features": row.features,
        "feature_version": row.feature_version,
        "label_version": row.label_version,
        "dataset_version": row.dataset_version,
        "dataset_fingerprint": row.dataset_fingerprint,
        "preprocessing_version": row.preprocessing_version,
        "code_version": row.code_version,
        "metrics": row.metrics,
        "calibration": row.calibration,
        "calibrated": row.calibration is not None,
        "training_period": _period(row.training_start, row.training_end),
        "validation_period": _period(row.validation_start, row.validation_end),
        "test_period": _period(row.test_start, row.test_end),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _period(start: datetime | None, end: datetime | None) -> list[str] | None:
    if start is None or end is None:
        return None
    return [start.isoformat(), end.isoformat()]


def utcnow() -> datetime:
    """The clock, in one place, so no model reads it."""
    return datetime.now(UTC).replace(tzinfo=None)
