"""The dataset builder: bars in, one validated and versioned dataset out.

    RAW BARS -> quality -> features (<= T) -> labels (> T) -> alignment
             -> chronological split -> scaler on train only -> leakage checks
             -> READY, or blocked with the reason

**A dataset is a recipe, not a pile of rows.** Section 30 asks for
reproducibility: the same raw data, feature version, label version and
configuration must produce the same dataset. If that holds, storing the rows is
optional — and storing them would mean a second copy of `market_bars` that can
drift from it. So what is persisted is the manifest and the fingerprint, and
`build()` reproduces the rows on demand. The backtester already works this way
and for the same reason.

**Alignment is section 25, and it is one line of code and the whole point.**
Row `i` holds the features of bar `i`, computed from bars `<= i`, and the
labels of bar `i`, computed from bars `> i`. The two never overlap, and the
row's timestamp is bar `i`'s open time. Everything else in this module is
making sure that stays true.

**Rows are dropped, never filled.** A row is kept only when every requested
feature has warmed up and every requested label has a future. The counts of
what was dropped and why travel in the manifest, so a dataset that lost most of
itself says so instead of looking small.

**A dataset cannot reach READY on its own.** `status` goes RAW -> CLEAN ->
READY, and the step to READY requires the leakage report to pass. A failing
check leaves it at CLEAN with the failure attached, which is section 54's rule:
if leakage validation fails, block the dataset.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.datasets import features as feature_engine
from app.datasets import labels as label_engine
from app.datasets import leakage, quality, splits
from app.datasets.scaler import Scaler
from app.datasets.scaler import fit as fit_scaler
from app.marketdata.types import Bar, Provider, Timeframe

# Bumped when the builder's own arithmetic changes -- alignment, drop rules,
# the order of operations. Separate from the feature and label versions
# because a dataset can be wrong through none of their fault.
BUILDER_VERSION = "1.0.0"


class BuildError(Exception):
    """A dataset that cannot be built. Refused rather than partially produced."""


class DatasetStatus(StrEnum):
    """Section 35's lifecycle, with only the states this level can reach.

    `RAW` and `CLEAN` are real intermediate states: a dataset stops at CLEAN
    when validation fails, and the distinction between "not validated yet" and
    "validated and rejected" is the one an operator needs. `LOCKED`, `TRAINING`
    and `ARCHIVED` belong to levels 25 and 28 and are deliberately absent --
    declaring a state nothing can enter would make the vocabulary a wish list.
    """

    raw = "RAW"
    clean = "CLEAN"
    ready = "READY"


@dataclass(frozen=True)
class DatasetConfig:
    """Everything that could change the output. All of it hashed.

    `symbol` and `timeframe` are single rather than lists because a dataset
    that pools two instruments has to answer the unit question first, and
    section 42 requires the instrument to travel with the row. Multi-symbol
    datasets are built by combining single-symbol ones, which keeps the
    provenance per row instead of per file.
    """

    key: str
    symbol: str
    timeframe: Timeframe
    provider: Provider
    label_config: label_engine.LabelConfig
    # The provider's own name for the instrument, which is what `market_bars`
    # is keyed by. Part of the recipe rather than a lookup: rebuilding a
    # dataset needs it, and a rebuild that had to guess which provider symbol
    # produced these rows would not be a rebuild. Defaults to the internal
    # symbol, which is right whenever the two agree.
    provider_symbol: str = ""
    feature_names: tuple[str, ...] = feature_engine.DEFAULT_FEATURES
    label_names: tuple[str, ...] = label_engine.DEFAULT_LABELS
    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    walk_forward_folds: int = 4
    normalise: bool = True

    def __post_init__(self) -> None:
        if not self.key or len(self.key) > 64:
            raise BuildError("a dataset key is required and must be at most 64 characters")
        for name in self.feature_names:
            feature_engine.spec_for(name)
        for name in self.label_names:
            if name not in label_engine.LABEL_CATALOGUE:
                raise BuildError(f"unknown label {name!r}")
        if not self.feature_names:
            raise BuildError("a dataset with no features is not a dataset")
        if not self.label_names:
            raise BuildError("a dataset with no labels cannot be trained on")

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "symbol": self.symbol,
            "provider_symbol": self.provider_symbol or self.symbol,
            "timeframe": str(self.timeframe),
            "provider": str(self.provider),
            "feature_set_version": feature_engine.FEATURE_SET_VERSION,
            "features": list(self.feature_names),
            "label_set_version": label_engine.LABEL_SET_VERSION,
            "labels": list(self.label_names),
            "label_config": self.label_config.as_dict(),
            "train_fraction": self.train_fraction,
            "validation_fraction": self.validation_fraction,
            "walk_forward_folds": self.walk_forward_folds,
            "normalise": self.normalise,
            "builder_version": BUILDER_VERSION,
        }

    def fingerprint(self, *, bars_digest: str) -> str:
        """The dataset's reproducible identity.

        Includes a digest of the raw bars, so two datasets built from the same
        recipe over different data are different datasets. Without it the
        fingerprint would say "same recipe" and be read as "same dataset".
        """
        material = json.dumps(
            {"config": self.as_dict(), "bars": bars_digest}, sort_keys=True, default=str
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DatasetRow:
    """One training example. Features from `<= at`, labels from `> at`."""

    at: datetime
    symbol: str
    timeframe: str
    features: dict[str, float | None]
    labels: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "features": self.features,
            "labels": self.labels,
        }


@dataclass
class Dataset:
    """A built dataset: rows, how they were made, and whether they may be used."""

    config: DatasetConfig
    rows: list[DatasetRow]
    split: splits.Split | None
    folds: list[splits.Fold]
    scaler: Scaler | None
    quality: quality.QualityReport
    leakage: leakage.LeakageReport
    status: DatasetStatus
    fingerprint: str
    dropped: dict[str, int] = field(default_factory=dict)
    blocked_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ready(self) -> bool:
        return self.status is DatasetStatus.ready

    def manifest(self) -> dict[str, Any]:
        """Section 29. Everything needed to explain or rebuild this dataset."""
        return {
            "dataset_key": self.config.key,
            "fingerprint": self.fingerprint,
            "status": str(self.status),
            "blocked_reason": self.blocked_reason,
            "created_at": self.created_at.isoformat(),
            "rows": len(self.rows),
            "dropped": dict(self.dropped),
            "start": self.rows[0].at.isoformat() if self.rows else None,
            "end": self.rows[-1].at.isoformat() if self.rows else None,
            "config": self.config.as_dict(),
            "feature_set": feature_engine.feature_set_manifest(self.config.feature_names),
            "label_set": label_engine.label_set_manifest(
                self.config.label_config, self.config.label_names
            ),
            "quality": self.quality.as_dict(),
            "split": self.split.as_dict() if self.split else None,
            "walk_forward": [f.as_dict() for f in self.folds],
            "scaler": self.scaler.as_dict() if self.scaler else None,
            "leakage": self.leakage.as_dict(),
            "alignment": (
                "row i holds features computed from bars at or before bar i, and labels "
                "computed from bars strictly after bar i. The row's timestamp is bar i's "
                "open time."
            ),
            "reproducibility": (
                "rows are not stored. The same bars, feature version, label version and "
                "configuration rebuild them exactly; the fingerprint covers all four."
            ),
        }


def bars_digest(bars: list[Bar]) -> str:
    """A hash of the raw series, so the fingerprint covers the data too."""
    material = "\n".join(
        f"{b.bar_time.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}|{b.spread}"
        for b in bars
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build(bars: list[Bar], config: DatasetConfig, *, now: datetime | None = None) -> Dataset:
    """Build, validate, and only then allow READY."""
    digest = bars_digest(bars)
    fingerprint = config.fingerprint(bars_digest=digest)

    report = quality.assess(bars, config.timeframe, now=now)
    if not report.usable:
        return _blocked(
            config,
            report,
            fingerprint,
            "; ".join(report.blocking),
            DatasetStatus.raw,
        )

    # A source whose candle shape cannot be trusted loses those features and
    # keeps the rest. Dropping the series instead would throw away usable
    # close-based data over a vendor's construction.
    names = tuple(n for n in config.feature_names if n not in report.unusable_features)
    if not names:
        return _blocked(
            config,
            report,
            fingerprint,
            "every requested feature reads the candle's shape, and this source's OHLC is "
            "not trustworthy. There is nothing left to build.",
            DatasetStatus.clean,
        )

    feature_rows = feature_engine.compute_features(bars, names)
    label_rows = label_engine.compute_labels(bars, config.label_config, config.label_names)

    kept: list[DatasetRow] = []
    kept_indices: list[int] = []
    dropped = {"cold_start": 0, "no_future": 0, "incomplete_feature": 0}
    warmup = feature_engine.warmup_for(names)
    horizon = config.label_config.horizon

    for index, bar in enumerate(bars):
        if index < warmup:
            dropped["cold_start"] += 1
            continue
        if not label_rows[index].is_complete(config.label_names):
            # Either the horizon ran off the end, or a bracket had no ATR.
            if index + horizon >= len(bars):
                dropped["no_future"] += 1
            else:
                dropped["incomplete_feature"] += 1
            continue
        if not feature_engine.feature_row_is_complete(feature_rows[index], names):
            dropped["incomplete_feature"] += 1
            continue
        kept.append(
            DatasetRow(
                at=bar.bar_time,
                symbol=config.symbol,
                timeframe=str(config.timeframe),
                features=dict(feature_rows[index]),
                labels=label_rows[index].as_dict(),
            )
        )
        kept_indices.append(index)

    if len(kept) < 10:
        return _blocked(
            config,
            report,
            fingerprint,
            f"only {len(kept)} usable rows after a {warmup}-bar warm-up and a "
            f"{horizon}-bar horizon. A dataset this small cannot be split three ways, "
            "and padding it would mean inventing rows.",
            DatasetStatus.clean,
            dropped=dropped,
        )

    times = [row.at for row in kept]
    try:
        split = splits.chronological(
            times, train=config.train_fraction, validation=config.validation_fraction
        )
        folds = splits.walk_forward(times, folds=config.walk_forward_folds)
    except splits.SplitError as exc:
        return _blocked(config, report, fingerprint, str(exc), DatasetStatus.clean, dropped=dropped)

    scaler: Scaler | None = None
    if config.normalise:
        scaler = fit_scaler(
            [row.features for row in kept],
            split,
            feature_version=feature_engine.FEATURE_SET_VERSION,
            features=names,
        )

    findings = [
        leakage.no_future_influence(
            bars, lambda window: feature_engine.compute_features(window, names)
        ),
        leakage.labels_are_not_features(names, config.label_names),
        leakage.splits_are_ordered(times, split),
        leakage.labels_have_a_future(len(bars), kept_indices, horizon),
        leakage.feature_timestamps_are_causal(times, [bars[i].bar_time for i in kept_indices]),
    ]
    if scaler is not None:
        findings.append(
            leakage.scaler_fitted_on_train_only(
                scaler, [row.features for row in kept], split, features=names
            )
        )
    audit = leakage.LeakageReport(findings)

    status = DatasetStatus.ready if audit.passed else DatasetStatus.clean
    blocked = (
        None if audit.passed else "; ".join(f"{f.check}: {f.detail}" for f in audit.failures())
    )

    return Dataset(
        config=config,
        rows=kept,
        split=split,
        folds=folds,
        scaler=scaler,
        quality=report,
        leakage=audit,
        status=status,
        fingerprint=fingerprint,
        dropped=dropped,
        blocked_reason=blocked,
    )


def _blocked(
    config: DatasetConfig,
    report: quality.QualityReport,
    fingerprint: str,
    reason: str,
    status: DatasetStatus,
    *,
    dropped: dict[str, int] | None = None,
) -> Dataset:
    """A dataset that stopped, with the reason attached rather than raised.

    Returned rather than raised because "this data cannot make a dataset" is an
    answer a caller wants to read, store and show — not an exception to catch
    and turn back into one.
    """
    return Dataset(
        config=config,
        rows=[],
        split=None,
        folds=[],
        scaler=None,
        quality=report,
        leakage=leakage.LeakageReport([]),
        status=status,
        fingerprint=fingerprint,
        dropped=dropped or {},
        blocked_reason=reason,
    )


def class_balances(dataset: Dataset) -> list[dict[str, Any]]:
    """Section 39: the distribution of each classification label, reported only."""
    out: list[dict[str, Any]] = []
    for name in ("direction", "bracket_outcome"):
        if name not in dataset.config.label_names:
            continue
        counts: dict[str, int] = {}
        for row in dataset.rows:
            value = row.labels.get(name)
            if value is not None:
                counts[str(value)] = counts.get(str(value), 0) + 1
        total = sum(counts.values())
        out.append(
            {
                "label": name,
                "counts": counts,
                "shares": {k: round(v / total, 6) for k, v in counts.items()} if total else {},
                "note": "reported, not corrected; rebalancing a holdout would leak",
            }
        )
    return out
