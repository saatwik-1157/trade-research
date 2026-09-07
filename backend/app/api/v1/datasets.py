"""The data pipeline's surface: what exists, how it was made, and whether it may be used.

**Read-mostly, and the one write builds rather than mutates.** Section 48 warns
against exposing unsafe data mutation. There is no route here that edits a bar,
a feature value or a label: the only mutating verb rebuilds a dataset from
`market_bars` and records what validation concluded. Raw data has no write path
from this router at all.

**Nothing here trains anything.** Section 46. `POST /datasets/build` produces a
dataset and a verdict; deciding to fit a model on it is level 25's, and the
package this router calls imports no model.

**A dataset's status is reported, never set by a caller.** There is no
`PATCH /datasets/{id}` that marks one READY. The builder decides, and it cannot
reach READY while a leakage check fails — so an operator who disagrees with the
verdict has to fix the data, which is the point.

**The manifest is the product.** `GET /datasets/{id}/manifest` returns the whole
recipe: config, feature definitions, label parameters, quality, split
boundaries, walk-forward folds, scaler parameters and every leakage finding.
That is what makes a dataset explainable six months later without reading the
code that built it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import NotFound, ValidationFailed
from app.datasets import features as feature_engine
from app.datasets import labels as label_engine
from app.datasets import service as dataset_service
from app.datasets.builder import BUILDER_VERSION, BuildError
from app.marketdata.types import Provider, TimeframeError, parse_timeframe
from app.models.datasets import DatasetCheck, DatasetRecord, FeatureSet, LabelSet

router = APIRouter(prefix="/datasets", tags=["datasets"])

# Training data is research material, and the permission table already has the
# right answer for it: `manage_ai_models` is what every other AI-side group
# asks for. A USER reading it would be reading the inputs a trading model is
# fitted on, which is not the read-only view of results a new account gets.
_READ = Depends(require_permission(Permission.manage_ai_models))
_BUILD = Depends(require_permission(Permission.manage_ai_models))


class BuildBody(BaseModel):
    """What to build. Every field that changes the output is here and required.

    `spread_points` has no default, matching `LabelConfig` and `BacktestConfig`:
    the one effect this repository has measured to significance is cost drag, so
    a caller must say what trading costs rather than inheriting a zero.
    """

    key: Annotated[str, Field(min_length=1, max_length=64)]
    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    provider_symbol: Annotated[str, Field(min_length=1, max_length=64)]
    provider: str = "mt5"
    timeframe: str = "H1"
    horizon: int = Field(default=24, ge=1, le=1000)
    spread_points: Decimal = Field(gt=0)
    version: Annotated[str, Field(min_length=1, max_length=16)] = "1"
    limit: int = Field(default=5000, ge=100, le=200_000)


@router.get("", summary="Every dataset, with its verdict")
async def list_datasets(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    rows = list(
        (
            await db.scalars(
                select(DatasetRecord).order_by(DatasetRecord.created_at.desc()).limit(limit)
            )
        ).all()
    )
    items = [dataset_service.summarise(row) for row in rows]
    return {
        "items": items,
        "counts": {
            "total": len(items),
            "ready": sum(1 for i in items if i["ready"]),
            "blocked": sum(1 for i in items if not i["ready"]),
        },
        "note": (
            "READY means every leakage check passed. A dataset that failed one stays at "
            "CLEAN with the failure attached, and no caller can override that."
        ),
    }


@router.get("/features", summary="The feature registry: name, formula, lookback, unit")
async def feature_registry(user: User = _READ) -> dict[str, Any]:
    return {
        "feature_set_version": feature_engine.FEATURE_SET_VERSION,
        "features": feature_engine.catalogue(),
        "warmup_bars": feature_engine.warmup_for(feature_engine.DEFAULT_FEATURES),
        "indicator_source": (
            "app.strategies.indicators, which calls tools/rule_backtest.py and "
            "tools/rule_search.py. There is no second indicator engine."
        ),
        "units": (
            "every feature is dimensionless. No raw price level is offered: a model "
            "trained on a moving average learns the price of the instrument, and pooling "
            "price-scaled quantities across symbols is the arithmetic error this project "
            "has already measured."
        ),
        "timestamp_policy": "every feature at T reads bars at or before T",
    }


@router.get("/labels", summary="The label registry, and what each one reads")
async def label_registry(user: User = _READ) -> dict[str, Any]:
    return {
        "label_set_version": label_engine.LABEL_SET_VERSION,
        "labels": label_engine.label_catalogue(),
        "separation": (
            "a label reads bars strictly after T and a feature reads bars at or before "
            "T. No name appears in both, and a dataset whose columns overlap fails "
            "validation."
        ),
        "tail_policy": (
            "the last `horizon` rows of a series have no future and are dropped, never "
            "filled. A fabricated label teaches a model that the end of a dataset is a "
            "particular kind of market."
        ),
        "ambiguity": (
            "a bracket whose stop and target are both inside one bar's range is labelled "
            "AMBIGUOUS. Bar data cannot order two prices inside a bar, and a coin flip "
            "dressed as a label is worse than a dropped row."
        ),
    }


@router.get("/engine", summary="What the builder guarantees, and what it does not")
async def engine(user: User = _READ) -> dict[str, Any]:
    return {
        "builder_version": BUILDER_VERSION,
        "feature_set_version": feature_engine.FEATURE_SET_VERSION,
        "label_set_version": label_engine.LABEL_SET_VERSION,
        "guarantees": [
            "features at T read bars at or before T, proved by recomputing with future "
            "bars appended and requiring identical output",
            "labels read bars strictly after T",
            "train, validation and test are chronological; nothing is shuffled",
            "the scaler is fitted on the training segment only, and re-fitting is "
            "checked rather than trusted",
            "rows are dropped rather than imputed, and the counts are in the manifest",
            "the same bars and configuration reproduce the same rows",
        ],
        "does_not": [
            "train, deploy or replace a model - that is level 24 and level 25",
            "write to market_bars, or to any raw data at all",
            "reach a broker, an order or the risk engine",
            "mark a dataset READY when a leakage check failed",
            "fill a missing candle, a missing feature or a missing label",
            "remove a market anomaly - a crash is data, not a defect",
        ],
        "limits": [
            "one symbol and one timeframe per dataset; multi-symbol sets are built by "
            "combining single-symbol ones so the instrument stays on the row",
            f"at most {dataset_service.MAX_BARS} bars are read per build",
            "rows are rebuilt on demand and never stored, so a build over a large "
            "history costs its time again each time it is asked for",
        ],
    }


@router.post("/build", summary="Build a dataset from stored bars and record the verdict")
async def build_dataset(
    body: BuildBody,
    db: AsyncSession = Depends(get_db),
    user: User = _BUILD,
) -> dict[str, Any]:
    """Reads `market_bars`, writes nothing to it, and trains nothing."""
    try:
        timeframe = parse_timeframe(body.timeframe)
    except TimeframeError as exc:
        raise ValidationFailed(str(exc)) from exc
    try:
        provider = Provider(body.provider)
    except ValueError as exc:
        raise ValidationFailed(
            f"unknown provider {body.provider!r}; this platform knows "
            f"{', '.join(p.value for p in Provider)}"
        ) from exc

    try:
        dataset, record = await dataset_service.build_and_register(
            db,
            key=body.key,
            symbol=body.symbol,
            provider_symbol=body.provider_symbol,
            provider=provider,
            timeframe=timeframe,
            horizon=body.horizon,
            spread_points=body.spread_points,
            version=body.version,
            limit=body.limit,
            user_id=user.id,
        )
    except (dataset_service.DatasetServiceError, BuildError) as exc:
        raise ValidationFailed(str(exc)) from exc

    # Committed here rather than in the service, so a caller that builds several
    # datasets in one transaction still decides when they become visible.
    await db.commit()

    return {
        **dataset_service.summarise(record),
        "dropped": dataset.dropped,
        "leakage": dataset.leakage.as_dict(),
        "quality": dataset.quality.as_dict(),
        "note": (
            "Nothing was trained. This route builds data and records what validation "
            "concluded; fitting a model on it is level 25."
        ),
    }


@router.get("/{dataset_id}", summary="One dataset")
async def get_dataset(
    dataset_id: str, db: AsyncSession = Depends(get_db), user: User = _READ
) -> dict[str, Any]:
    record = await db.get(DatasetRecord, dataset_id)
    if record is None:
        raise NotFound(f"no dataset {dataset_id}")
    feature_set = await db.get(FeatureSet, record.feature_set_id)
    label_set = await db.get(LabelSet, record.label_set_id)
    return {
        **dataset_service.summarise(record),
        "feature_set": {
            "key": feature_set.key,
            "version": feature_set.version,
            "warmup_bars": feature_set.warmup_bars,
        }
        if feature_set
        else None,
        "label_set": {
            "key": label_set.key,
            "version": label_set.version,
            "horizon": label_set.horizon,
        }
        if label_set
        else None,
    }


@router.get("/{dataset_id}/manifest", summary="The whole recipe, and how to rebuild it")
async def get_manifest(
    dataset_id: str, db: AsyncSession = Depends(get_db), user: User = _READ
) -> dict[str, Any]:
    record = await db.get(DatasetRecord, dataset_id)
    if record is None:
        raise NotFound(f"no dataset {dataset_id}")
    return record.manifest or {}


@router.get("/{dataset_id}/checks", summary="Every validation finding, passed or failed")
async def get_checks(
    dataset_id: str, db: AsyncSession = Depends(get_db), user: User = _READ
) -> dict[str, Any]:
    record = await db.get(DatasetRecord, dataset_id)
    if record is None:
        raise NotFound(f"no dataset {dataset_id}")
    rows = list(
        (
            await db.scalars(
                select(DatasetCheck)
                .where(DatasetCheck.dataset_id == dataset_id)
                .order_by(DatasetCheck.category, DatasetCheck.check_name)
            )
        ).all()
    )
    return {
        "dataset_id": dataset_id,
        "status": record.status,
        "items": [
            {
                "category": row.category,
                "check": row.check_name,
                "passed": row.passed,
                "detail": row.detail,
                "evidence": row.evidence,
                "ran_at": row.ran_at.isoformat(),
            }
            for row in rows
        ],
        "note": (
            "a passing check is recorded too. A dataset whose leakage report is absent "
            "and one whose report passed look identical from a status column, and only "
            "one of them has been looked at."
        ),
    }
