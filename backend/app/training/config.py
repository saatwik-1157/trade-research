"""What a training run is, stated completely enough to reproduce it.

**Everything that could change the result is a field, and every field is
hashed.** The same argument `BacktestConfig` makes, applied to a fit: a run
whose configuration was implicit produces a model nobody can rebuild, and a
model nobody can rebuild is one nobody can check.

**The dataset is locked by fingerprint, not by name.** Section 6 asks that a
job record the exact data it trained on. A version string only records what the
data was *called*; L23's fingerprint covers the configuration **and a digest of
the bars**, so "the dataset changed under the running job" is detectable rather
than merely forbidden. `TrainingService` re-derives it after the fit and fails
the job if it moved.

**The split is stored with the job.** Section 9. Fractions rather than dates
because a dataset's range is not known until it is built, and a caller who
wants dates can convert — but nothing is hardcoded, and the values that were
actually used travel in the manifest.

**There is no `shuffle` field.** Section 8's rule is not configurable here:
`app.datasets.splits` has no shuffling to enable, and a flag that could turn
temporal ordering off would be the one setting nobody should ever find.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Bumped when the training procedure itself changes -- the order of stages, what
# is fitted on what, how a baseline is chosen. Separate from the feature, label
# and dataset versions because a run can be wrong through none of their fault.
TRAINING_ENGINE_VERSION = "1.0.0"

# How many training jobs may run at once, and how many one user may have in
# flight. Section 19: training must not consume unlimited resources. The
# numbers are small because these fits are seconds long and the limit exists to
# stop a queue, not to schedule a cluster.
MAX_CONCURRENT = 2
MAX_QUEUED_PER_USER = 3


class TrainingError(Exception):
    """A run that cannot proceed. Refused rather than started and abandoned."""


class TrainingStage(StrEnum):
    """Where a job is. Reported, and used to make cancellation cooperative.

    Every stage is a real boundary in `TrainingService._run`, so `current_stage`
    is observed rather than announced -- the same distinction L22 draws between
    a heartbeat and a status column.
    """

    queued = "queued"
    loading_dataset = "loading_dataset"
    quality_gates = "quality_gates"
    splitting = "splitting"
    preprocessing = "preprocessing"
    fitting_baseline = "fitting_baseline"
    fitting_candidate = "fitting_candidate"
    evaluating = "evaluating"
    recording = "recording"
    done = "done"


ORDERED_STAGES: tuple[TrainingStage, ...] = (
    TrainingStage.queued,
    TrainingStage.loading_dataset,
    TrainingStage.quality_gates,
    TrainingStage.splitting,
    TrainingStage.preprocessing,
    TrainingStage.fitting_baseline,
    TrainingStage.fitting_candidate,
    TrainingStage.evaluating,
    TrainingStage.recording,
    TrainingStage.done,
)


def progress_of(stage: TrainingStage) -> float:
    """A fraction, derived from the stage rather than reported separately.

    Two numbers that can disagree is one number too many: a progress field a
    trainer updates by hand drifts from the stage it claims to describe.
    """
    return round(ORDERED_STAGES.index(stage) / (len(ORDERED_STAGES) - 1), 4)


class ModelFamily(StrEnum):
    """The three families §47 asks for, and no more.

    Expected return, volatility and exit models are absent deliberately: each
    needs a label that does not exist, and adding a family because an
    architecture diagram lists it is what §48 calls fake AI.
    """

    regime = "regime"
    trade_probability = "trade_probability"
    anomaly = "anomaly"


@dataclass(frozen=True)
class SplitConfig:
    """Section 9, configurable and never hardcoded at the call site."""

    train: float = 0.70
    validation: float = 0.15
    walk_forward_folds: int = 4

    def __post_init__(self) -> None:
        if not 0 < self.train < 1:
            raise TrainingError("the training fraction must be between 0 and 1")
        if not 0 <= self.validation < 1:
            raise TrainingError("the validation fraction must be between 0 and 1")
        if self.train + self.validation >= 1.0:
            raise TrainingError(
                f"train ({self.train}) + validation ({self.validation}) leaves no test "
                "segment. A run whose holdout is empty measures nothing, and the test "
                "segment is the one thing this pipeline protects."
            )
        if self.walk_forward_folds < 2:
            raise TrainingError("a single fold is a single split; ask for at least two")

    @property
    def test(self) -> float:
        return round(1.0 - self.train - self.validation, 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "train": self.train,
            "validation": self.validation,
            "test": self.test,
            "walk_forward_folds": self.walk_forward_folds,
            "ordering": "chronological. There is no shuffle option, here or downstream.",
        }


@dataclass(frozen=True)
class TrainingConfig:
    """One run, completely specified.

    `dataset_key` and `dataset_version` name a dataset the platform already
    built; `dataset_fingerprint` is what the job locks onto. A caller cannot
    ask to "train on whatever data is current" — there is no spelling for it.
    """

    family: ModelFamily
    dataset_key: str
    dataset_version: str
    model_version: str
    features: tuple[str, ...] = ()
    split: SplitConfig = field(default_factory=SplitConfig)
    random_seed: int = 42
    # Logistic regression only. Named rather than a free-form blob so a value
    # nobody validated cannot reach the fitter, which is §35's rule about not
    # accepting arbitrary configuration.
    iterations: int = 400
    learning_rate: float = 0.1
    l2: float = 0.01
    # Section 15. `balanced` weights the minority class up inside the TRAINING
    # rows only; nothing resamples a holdout, and there is no option that would.
    class_weight: str = "none"
    # Section 16. Iterative fits stop on the VALIDATION metric, never the test
    # one. `0` disables it rather than hiding it behind a boolean.
    early_stopping_patience: int = 0
    minimum_rows: int = 200
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.dataset_key:
            raise TrainingError("a dataset key is required; there is no 'latest' option")
        if not self.model_version:
            raise TrainingError("a candidate model version is required")
        if self.random_seed < 0:
            raise TrainingError("the random seed must not be negative")
        if not 1 <= self.iterations <= 100_000:
            raise TrainingError("iterations must be between 1 and 100000")
        if not 0 < self.learning_rate <= 10:
            raise TrainingError("the learning rate must be between 0 and 10")
        if self.l2 < 0:
            raise TrainingError("L2 regularisation cannot be negative")
        if self.class_weight not in ("none", "balanced"):
            raise TrainingError(
                f"unknown class_weight {self.class_weight!r}; this fitter supports "
                "'none' and 'balanced'. An unrecognised value is refused rather than "
                "ignored, because a caller who asked for weighting should not get none."
            )
        if self.early_stopping_patience < 0:
            raise TrainingError("early stopping patience cannot be negative")
        if self.minimum_rows < 50:
            raise TrainingError(
                "a minimum below 50 rows is not a guard. Section 7 asks for a sufficient "
                "sample check, and a threshold this low would pass anything."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "training_engine_version": TRAINING_ENGINE_VERSION,
            "family": str(self.family),
            "dataset_key": self.dataset_key,
            "dataset_version": self.dataset_version,
            "model_version": self.model_version,
            "features": list(self.features),
            "split": self.split.as_dict(),
            "random_seed": self.random_seed,
            "iterations": self.iterations,
            "learning_rate": self.learning_rate,
            "l2": self.l2,
            "class_weight": self.class_weight,
            "early_stopping_patience": self.early_stopping_patience,
            "minimum_rows": self.minimum_rows,
            "notes": self.notes,
        }

    def fingerprint(self) -> str:
        """The identity of this run's *recipe*, without the data.

        Section 38 uses it to recognise a duplicate job. The dataset fingerprint
        is deliberately NOT part of it: two runs of the same recipe over the
        same named dataset are the duplicate that section is about, and folding
        the data digest in would make them look different whenever a bar
        arrived.
        """
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]


def environment() -> dict[str, Any]:
    """Section 12: what would have to match for a rerun to reproduce this.

    Reported rather than asserted. The project does not control the machine a
    run happens on, and claiming determinism across environments it has not
    tested would be exactly the false claim §12 warns against.
    """
    import platform
    import sys

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "implementation": platform.python_implementation(),
        "training_engine_version": TRAINING_ENGINE_VERSION,
        "frameworks": "none. Every fit here is standard-library arithmetic (L24).",
        "determinism": (
            "the fits are deterministic for a given configuration and dataset: no RNG, "
            "no shuffling, no early stopping unless configured, weights initialised to "
            "zero. Reproducibility across DIFFERENT Python or platform versions is not "
            "claimed, because it has not been measured."
        ),
    }
