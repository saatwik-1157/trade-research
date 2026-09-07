"""The training engine: a dataset in, a *candidate* out, and nothing promoted.

    L23 dataset (READY, leakage-checked, fingerprinted)
        |
        v
    gates.evaluate            refuses on quality, shape, features, labels
        |
        v
    chronological split       from the dataset; nothing here re-splits
        |
        v
    Scaler.fit(rows, split)   the training segment only, by API shape
        |
        v
    fit_majority              the baseline every candidate must beat
        |
        v
    fit_weighted_logistic     deterministic; early stopping on VALIDATION
        |
        v
    metrics.classification    +  metrics.economic     (never merged)
        |
        v
    ai_service.register_version(status="draft")
        |
        v
    status = validation_pending    -> L26 validates, L28 promotes

**Nothing here promotes, deploys or trades.** Sections 25, 26 and 36. This
package imports no order manager, no broker adapter, no risk engine and no
strategy; a test parses every module to keep it so. A successful run means "a
candidate model was trained", which is not "a model is approved for trading".

**No new queue.** `TrainingService` uses the background-task-plus-semaphore
pattern `BacktestService` and `ReplayService` have used since L14 and L15.
Section 4 asks for exactly that, and a fourth way of running a background job
in one codebase would be three too many.
"""

from app.training.config import (
    MAX_CONCURRENT,
    MAX_QUEUED_PER_USER,
    TRAINING_ENGINE_VERSION,
    ModelFamily,
    SplitConfig,
    TrainingConfig,
    TrainingError,
    TrainingStage,
    environment,
    progress_of,
)
from app.training.gates import Gate, GateReport, evaluate
from app.training.service import (
    DuplicateTrainingJob,
    TrainingBusy,
    TrainingService,
    summarise,
)
from app.training.trainers import (
    FitReport,
    MajorityBaseline,
    class_weights,
    fit_majority,
    fit_weighted_logistic,
)

__all__ = [
    "MAX_CONCURRENT",
    "MAX_QUEUED_PER_USER",
    "TRAINING_ENGINE_VERSION",
    "DuplicateTrainingJob",
    "FitReport",
    "Gate",
    "GateReport",
    "MajorityBaseline",
    "ModelFamily",
    "SplitConfig",
    "TrainingBusy",
    "TrainingConfig",
    "TrainingError",
    "TrainingService",
    "TrainingStage",
    "class_weights",
    "environment",
    "evaluate",
    "fit_majority",
    "fit_weighted_logistic",
    "progress_of",
    "summarise",
]
