"""feature_sets, label_sets, datasets, dataset_checks.

**No table here holds a training row.** A dataset is a recipe with a
fingerprint, and `app.datasets.builder` rebuilds its rows from `market_bars`
on demand. Storing them would be a second copy of data the platform already
has, and a second copy is a copy that can drift from the first — which is the
argument `market_bars` itself makes for keeping `provider` in its identity.

**Versions are immutable.** `feature_sets` and `label_sets` are unique on
`(key, version)` and nothing updates a row: a changed formula mints a new
version, so a dataset built six months ago still describes what it was built
from. Section 18 and section 21.

**A dataset's status is what validation concluded, not what a caller asked
for.** Only `app.datasets.builder` decides it, and it cannot reach `READY`
while the leakage report has a failure.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import CreatedMixin, IdMixin, JSONType

# Only the states this level can actually reach. Section 35 also names LOCKED,
# TRAINING and ARCHIVED; they belong to levels 25 and 28, and declaring a value
# nothing can write would turn the vocabulary into a wish list -- the same
# reasoning that kept `created` out of the bot run states at L22.
DATASET_STATUSES = ("RAW", "CLEAN", "READY")


class FeatureSet(IdMixin, CreatedMixin, Base):
    """An immutable feature-set version: which features, and how each is defined."""

    __tablename__ = "feature_sets"
    __table_args__ = (UniqueConstraint("key", "version", name="feature_set_identity"),)

    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The registry from `app.datasets.features`: name, formula, inputs,
    # lookback, unit and timestamp policy for every feature in the set.
    definition: Mapped[dict] = mapped_column(JSONType, nullable=False)
    warmup_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class LabelSet(IdMixin, CreatedMixin, Base):
    """An immutable label-set version: which labels, and the parameters behind them."""

    __tablename__ = "label_sets"
    __table_args__ = (UniqueConstraint("key", "version", name="label_set_identity"),)

    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # horizon, thresholds, bracket distances and the spread charged. A label
    # computed without a cost is a label for a market nobody trades in, so the
    # cost is part of the set's identity rather than a runtime argument.
    params: Mapped[dict] = mapped_column(JSONType, nullable=False)
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)


class DatasetRecord(IdMixin, CreatedMixin, Base):
    """One built dataset: the recipe, the verdict, and how to rebuild it.

    Named `DatasetRecord` rather than `Dataset` because `app.datasets.builder`
    already owns that name for the built thing itself. Two `Dataset` classes in
    one codebase is an import away from a bug nobody reads twice.
    """

    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("key", "version", name="dataset_identity"),
        CheckConstraint("status IN ('" + "','".join(DATASET_STATUSES) + "')", name="status"),
        CheckConstraint("row_count >= 0", name="row_count_not_negative"),
        # A dataset that says it is READY must carry the fingerprint that
        # reproduces it. Without one it is a claim nobody can check.
        CheckConstraint(
            "status <> 'READY' OR (fingerprint IS NOT NULL AND row_count > 0)",
            name="ready_is_reproducible",
        ),
        Index("ix_datasets_fingerprint", "fingerprint"),
    )

    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    feature_set_id: Mapped[str] = mapped_column(
        ForeignKey("feature_sets.id", ondelete="RESTRICT"), index=True
    )
    label_set_id: Mapped[str] = mapped_column(
        ForeignKey("label_sets.id", ondelete="RESTRICT"), index=True
    )
    symbol_id: Mapped[str | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The provider is part of the identity for the same reason it is part of
    # `market_bars`' key: two providers' bars are different measurements of
    # different books and must never be pooled by omission.
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RAW")
    # Null while a dataset is blocked; required once it claims READY.
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The full manifest: config, feature set, label set, quality, split,
    # walk-forward folds, scaler parameters and the leakage report.
    manifest: Mapped[dict] = mapped_column(JSONType, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class DatasetCheck(IdMixin, Base):
    """One validation finding against a dataset, kept whether it passed or not.

    A passing check is evidence too: a dataset whose leakage report is absent
    and one whose report passed look identical from a status column, and only
    one of them has been looked at.
    """

    __tablename__ = "dataset_checks"
    __table_args__ = (
        CheckConstraint("category IN ('quality','leakage','split')", name="category"),
        Index("ix_dataset_checks_dataset", "dataset_id", "category"),
    )

    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    # `check_name`, not `check`: CHECK is a reserved word in SQL, and a column
    # that only works because every dialect happens to quote it is a column
    # waiting for the one that does not.
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
