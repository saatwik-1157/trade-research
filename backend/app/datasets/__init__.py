"""The AI data pipeline: raw bars to a versioned, leakage-checked dataset.

    market_bars (RAW, never rewritten)
        -> quality.assess          what this series can support
        -> resampling              only when a timeframe must be built
        -> features.compute        bars at or before T
        -> labels.compute          bars strictly after T
        -> builder.build           alignment, split, scaler, leakage
        -> READY, or blocked at CLEAN with the reason

**Nothing here can place an order, and nothing here trains a model.** Section
1 and section 46. The package imports no adapter, no OMS, no risk engine and
no model; a test parses every module to keep it that way. It produces data,
and levels 24 and 25 decide what to do with it.

**Nothing here writes to `market_bars`.** The raw layer is written by
`app.marketdata` and is the reproducible source this pipeline reads. Section
10's rule — raw data is never overwritten by cleaned values — holds because
the cleaning has no write path at all.
"""

from app.datasets.builder import (
    BUILDER_VERSION,
    BuildError,
    Dataset,
    DatasetConfig,
    DatasetRow,
    DatasetStatus,
    build,
)
from app.datasets.features import (
    DEFAULT_FEATURES,
    FEATURE_SET_VERSION,
    FeatureError,
    compute_features,
)
from app.datasets.labels import (
    DEFAULT_LABELS,
    LABEL_SET_VERSION,
    LabelConfig,
    LabelError,
    compute_labels,
)
from app.datasets.leakage import LeakageFinding, LeakageReport
from app.datasets.quality import QualityReport, assess
from app.datasets.resampling import ResampleError, resample
from app.datasets.scaler import Scaler, ScalerError
from app.datasets.splits import Fold, Split, SplitError, chronological, walk_forward

__all__ = [
    "BUILDER_VERSION",
    "BuildError",
    "DEFAULT_FEATURES",
    "DEFAULT_LABELS",
    "Dataset",
    "DatasetConfig",
    "DatasetRow",
    "DatasetStatus",
    "FEATURE_SET_VERSION",
    "FeatureError",
    "Fold",
    "LABEL_SET_VERSION",
    "LabelConfig",
    "LabelError",
    "LeakageFinding",
    "LeakageReport",
    "QualityReport",
    "ResampleError",
    "Scaler",
    "ScalerError",
    "Split",
    "SplitError",
    "assess",
    "build",
    "chronological",
    "compute_features",
    "compute_labels",
    "resample",
    "walk_forward",
]
