"""Building a dataset from what the platform has already stored, and recording it.

Two responsibilities and no more: read `market_bars`, and write the registry.

**It reads `market_bars` and never writes them.** Section 10's rule — raw data
is never overwritten by cleaned values — holds here because this module has no
write path to that table. The cleaning is a read.

**Registering a dataset is idempotent on `(key, version)`.** Re-running a build
updates that row's verdict rather than minting a second identity, and the
fingerprint on it says whether the rows reproduce. Two rows under one key and
version would be two answers to "which dataset is this", and the one nobody
looked at would be the one a model got trained on.

**A feature set or label set version is written once and never updated.**
`_feature_set_row` and `_label_set_row` return the stored row untouched when one
exists. If a version's definition were mutable, a dataset built six months ago
would silently start describing something else; a changed formula is expected to
arrive as a new version string from `app.datasets.features`, and a different
label horizon as a different key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.datasets import features as feature_engine
from app.datasets import labels as label_engine
from app.datasets.builder import Dataset, DatasetConfig, DatasetStatus, build
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.models.datasets import DatasetCheck, DatasetRecord, FeatureSet, LabelSet
from app.models.market import MarketBar, Symbol

# A ceiling on how much history one build reads. Section 52 asks for large
# datasets to be handled without loading everything into memory; this pipeline
# is O(n) in bars and the honest answer for now is a bound with a stated
# number rather than a claim of streaming that is not implemented.
MAX_BARS = 200_000


class DatasetServiceError(Exception):
    """A build that cannot proceed. Refused rather than run on partial input."""


def _to_bar(row: MarketBar, symbol: str) -> Bar:
    """One stored row as the canonical `Bar` the pipeline works in.

    `spread_availability` travels with the value rather than being inferred
    from it, because a recorded 0 and an unrecorded spread are different facts
    and the project has measured what conflating them costs.
    """
    return Bar(
        symbol=symbol,
        provider=Provider(row.provider),
        timeframe=Timeframe(row.timeframe),
        bar_time=row.bar_time,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        spread=row.spread,
        spread_availability=Availability(row.spread_availability),
    )


async def load_bars(
    db: AsyncSession,
    *,
    provider: Provider,
    provider_symbol: str,
    timeframe: Timeframe,
    symbol: str,
    limit: int = 5_000,
) -> list[Bar]:
    """Ordered bars from the raw layer, oldest first.

    Ordered ascending here rather than anywhere downstream: every causal
    guarantee in this pipeline assumes it, and `splits._require_ordered`
    refuses rather than sorts, so the ordering has to be established at the one
    place that reads the table.
    """
    statement = (
        select(MarketBar)
        .where(
            MarketBar.provider == str(provider),
            MarketBar.provider_symbol == provider_symbol,
            MarketBar.timeframe == str(timeframe),
        )
        .order_by(MarketBar.bar_time.asc())
        .limit(min(limit, MAX_BARS))
    )
    rows = list((await db.scalars(statement)).all())
    return [_to_bar(row, symbol) for row in rows]


async def _feature_set_row(db: AsyncSession, names: tuple[str, ...]) -> FeatureSet:
    version = feature_engine.FEATURE_SET_VERSION
    key = "market_bars_v1"
    existing = await db.scalar(
        select(FeatureSet).where(FeatureSet.key == key, FeatureSet.version == version)
    )
    if existing is not None:
        return existing
    row = FeatureSet(
        key=key,
        version=version,
        description=(
            "Causal, dimensionless features over normalised OHLCV bars. Indicators come "
            "from app.strategies.indicators; no raw price level is offered."
        ),
        definition=feature_engine.feature_set_manifest(names),
        warmup_bars=feature_engine.warmup_for(names),
    )
    db.add(row)
    await db.flush()
    return row


async def _label_set_row(
    db: AsyncSession, config: label_engine.LabelConfig, names: tuple[str, ...]
) -> LabelSet:
    version = label_engine.LABEL_SET_VERSION
    # The fingerprint is part of the key, not the version: two label sets at
    # v1.0 with different horizons are different label sets, and giving them
    # one row would make a dataset unable to say which it used.
    key = f"outcome_{config.fingerprint()}"
    existing = await db.scalar(
        select(LabelSet).where(LabelSet.key == key, LabelSet.version == version)
    )
    if existing is not None:
        return existing
    row = LabelSet(
        key=key,
        version=version,
        description=(
            f"Forward outcomes over a {config.horizon}-bar horizon, charged "
            f"{config.spread_points} of spread. Labels read bars strictly after T."
        ),
        params=label_engine.label_set_manifest(config, names),
        horizon=config.horizon,
    )
    db.add(row)
    await db.flush()
    return row


async def register(
    db: AsyncSession,
    dataset: Dataset,
    *,
    version: str = "1",
    symbol_id: str | None = None,
    user_id: str | None = None,
) -> DatasetRecord:
    """Persist the manifest and the verdict. Idempotent on `(key, version)`."""
    config = dataset.config
    feature_row = await _feature_set_row(db, config.feature_names)
    label_row = await _label_set_row(db, config.label_config, config.label_names)

    if symbol_id is None:
        symbol_id = await db.scalar(select(Symbol.id).where(Symbol.code == config.symbol.upper()))

    existing = await db.scalar(
        select(DatasetRecord).where(
            DatasetRecord.key == config.key, DatasetRecord.version == version
        )
    )
    record = existing or DatasetRecord(key=config.key, version=version)

    record.feature_set_id = feature_row.id
    record.label_set_id = label_row.id
    record.symbol_id = symbol_id
    record.provider = str(config.provider)
    record.timeframe = str(config.timeframe)
    record.status = str(dataset.status)
    # Null unless the dataset is usable: a fingerprint on a blocked row would
    # read as "this reproduces", which is exactly what it does not do.
    record.fingerprint = dataset.fingerprint if dataset.rows else None
    record.row_count = len(dataset.rows)
    record.start_at = dataset.rows[0].at if dataset.rows else None
    record.end_at = dataset.rows[-1].at if dataset.rows else None
    record.quality_score = int(round(dataset.quality.score * 100))
    record.manifest = dataset.manifest()
    record.blocked_reason = dataset.blocked_reason
    record.created_by_user_id = user_id
    if existing is None:
        db.add(record)
    await db.flush()

    # Replace this dataset's checks rather than appending: a rebuild's verdict
    # supersedes the previous one, and keeping both would leave two answers to
    # "did the leakage check pass" with nothing to order them by.
    for row in (
        await db.scalars(select(DatasetCheck).where(DatasetCheck.dataset_id == record.id))
    ).all():
        await db.delete(row)

    ran_at = datetime.now(UTC).replace(tzinfo=None)
    for finding in dataset.leakage.findings:
        db.add(
            DatasetCheck(
                dataset_id=record.id,
                category="leakage",
                check_name=finding.check,
                passed=finding.passed,
                detail=finding.detail,
                evidence=finding.evidence,
                ran_at=ran_at,
            )
        )
    db.add(
        DatasetCheck(
            dataset_id=record.id,
            category="quality",
            check_name="series_is_usable",
            passed=dataset.quality.usable,
            detail="; ".join(dataset.quality.blocking) or "no blocking data-quality problem",
            evidence={
                "score": round(dataset.quality.score, 4),
                "warnings": list(dataset.quality.warnings),
                "unusable_features": list(dataset.quality.unusable_features),
            },
            ran_at=ran_at,
        )
    )
    if dataset.split is not None:
        db.add(
            DatasetCheck(
                dataset_id=record.id,
                category="split",
                check_name="chronological",
                passed=True,
                detail="train -> validation -> test in time order; nothing is shuffled",
                evidence=dataset.split.as_dict(),
                ran_at=ran_at,
            )
        )
    await db.flush()
    return record


async def build_and_register(
    db: AsyncSession,
    *,
    key: str,
    symbol: str,
    provider_symbol: str,
    provider: Provider,
    timeframe: Timeframe,
    horizon: int,
    spread_points: Decimal,
    version: str = "1",
    limit: int = 5_000,
    user_id: str | None = None,
    now: datetime | None = None,
) -> tuple[Dataset, DatasetRecord]:
    """The whole pipeline, from the stored raw layer to a recorded verdict."""
    bars = await load_bars(
        db,
        provider=provider,
        provider_symbol=provider_symbol,
        timeframe=timeframe,
        symbol=symbol,
        limit=limit,
    )
    if not bars:
        raise DatasetServiceError(
            f"no stored bars for {provider_symbol} {timeframe} from {provider}. Ingest "
            "history through /v1/market before building a dataset from it -- building "
            "from an empty series would produce a dataset that describes nothing."
        )

    config = DatasetConfig(
        key=key,
        symbol=symbol,
        timeframe=timeframe,
        provider=provider,
        provider_symbol=provider_symbol,
        label_config=label_engine.LabelConfig(spread_points=spread_points, horizon=horizon),
    )
    dataset = build(bars, config, now=now)
    record = await register(db, dataset, version=version, user_id=user_id)
    return dataset, record


def summarise(record: DatasetRecord) -> dict[str, Any]:
    """One dataset as an API row. The manifest is served separately."""
    manifest = record.manifest or {}
    leakage = manifest.get("leakage") or {}
    return {
        "id": record.id,
        "key": record.key,
        "version": record.version,
        "status": record.status,
        "ready": record.status == str(DatasetStatus.ready),
        "provider": record.provider,
        "timeframe": record.timeframe,
        "symbol_id": record.symbol_id,
        "rows": record.row_count,
        "start": record.start_at.isoformat() if record.start_at else None,
        "end": record.end_at.isoformat() if record.end_at else None,
        "quality_score": record.quality_score,
        "fingerprint": record.fingerprint,
        "feature_set_version": (manifest.get("config") or {}).get("feature_set_version"),
        "label_set_version": (manifest.get("config") or {}).get("label_set_version"),
        "leakage_passed": leakage.get("passed"),
        "leakage_failed_checks": leakage.get("failed"),
        "blocked_reason": record.blocked_reason,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def rebuild_config(record: DatasetRecord) -> DatasetConfig:
    """The `DatasetConfig` that produced a stored dataset, from its manifest.

    This is what makes "a dataset is a recipe" usable rather than merely true:
    a consumer holding a `datasets` row can reconstruct the exact rows without
    the rows having been stored. The label parameters come back from the
    manifest too, so a rebuild charges the same spread over the same horizon.
    """
    manifest = record.manifest or {}
    config = manifest.get("config") or {}
    label = config.get("label_config") or {}
    try:
        return DatasetConfig(
            key=config["key"],
            symbol=config["symbol"],
            timeframe=Timeframe(config["timeframe"]),
            provider=Provider(config["provider"]),
            label_config=label_engine.LabelConfig(
                spread_points=Decimal(str(label["spread_points"])),
                horizon=int(label["horizon"]),
                flat_threshold=float(label.get("flat_threshold", 0.0)),
                stop_atr=float(label.get("stop_atr", 1.5)),
                take_profit_atr=float(label.get("take_profit_atr", 1.5)),
                atr_period=int(label.get("atr_period", 14)),
            ),
            feature_names=tuple(config.get("features") or feature_engine.DEFAULT_FEATURES),
            label_names=tuple(config.get("labels") or label_engine.DEFAULT_LABELS),
            train_fraction=float(config.get("train_fraction", 0.6)),
            validation_fraction=float(config.get("validation_fraction", 0.2)),
            walk_forward_folds=int(config.get("walk_forward_folds", 4)),
            provider_symbol=str(config.get("provider_symbol") or config["symbol"]),
            normalise=bool(config.get("normalise", True)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DatasetServiceError(
            f"dataset {record.key} v{record.version} cannot be rebuilt from its manifest: "
            f"{exc}. A dataset whose recipe cannot be read is not reproducible, which is "
            "the one property this design rests on."
        ) from exc


def build_validation_loader(record: DatasetRecord, *, limit: int = 5_000) -> Any:
    """An awaitable returning `(bars, dataset)` from one read of the raw layer.

    Validation needs both: the dataset to score the model on, and the bars to
    run `tools/rule_backtest.simulate` over. Returning them from ONE call is the
    point — two loaders would be two queries and, on a live raw layer, two
    different windows of history. The dataset would then have been built from
    bars the economic evaluation never saw, and every alignment between them
    would be an assumption.

    Rows are aligned to bars by `bar_time` rather than by position: the builder
    drops a warm-up prefix and a horizon suffix, so a dataset index is not a bar
    index and treating it as one silently shifts every trade.
    """
    config = rebuild_config(record)
    provider_symbol = config.provider_symbol or config.symbol

    async def load(db: AsyncSession) -> tuple[list[Bar], Dataset]:
        bars = await load_bars(
            db,
            provider=config.provider,
            provider_symbol=provider_symbol,
            timeframe=config.timeframe,
            symbol=config.symbol,
            limit=limit,
        )
        if not bars:
            raise DatasetServiceError(
                f"no stored bars for {provider_symbol} {config.timeframe}. The dataset "
                "record exists but the raw layer it was built from is empty, so the rows "
                "cannot be reproduced."
            )
        return bars, build(bars, config)

    return load


def build_dataset_loader(record: DatasetRecord, *, limit: int = 5_000) -> Any:
    """An awaitable that rebuilds this dataset from the raw layer.

    Handed to the training service rather than imported by it, so training
    never grows a second way to produce a dataset -- and so a test can supply a
    prepared one without the service knowing the difference.
    """
    config = rebuild_config(record)
    provider_symbol = config.provider_symbol or config.symbol

    async def load(db: AsyncSession) -> Dataset:
        bars = await load_bars(
            db,
            provider=config.provider,
            provider_symbol=provider_symbol,
            timeframe=config.timeframe,
            symbol=config.symbol,
            limit=limit,
        )
        if not bars:
            raise DatasetServiceError(
                f"no stored bars for {provider_symbol} {config.timeframe}. The dataset "
                "record exists but the raw layer it was built from is empty, so the rows "
                "cannot be reproduced."
            )
        return build(bars, config)

    return load
