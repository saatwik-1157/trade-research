"""The automated execution engine (L20).

`outcome.py` is the vocabulary every pipeline in the platform reports in --
extracted from `app/paper/engine.py`, which re-exports it, so a paper bot and
the orchestrator produce counters that can be added together.

`pipeline.py` orchestrates an externally-arriving signal: validate, check the
strategy, ask the AI seat, ask Risk, ask Sizing, hand the approval to the OMS.
It computes none of those things itself and holds no broker adapter -- a test
parses every module in this package to prove it.

`worker.py` drives the pipeline from a supervised background loop, so
execution does not depend on a browser, an open request, or anything else a
user can close.
"""

from app.execution.ai import AiFilter, AiVerdict
from app.execution.outcome import NO_ORDER, ORDER_UNSETTLED, Outcome, created_an_order
from app.execution.pipeline import (
    DEFAULT_MAX_SIGNAL_AGE_SECONDS,
    ExecutionPipeline,
    ExecutionResult,
    IncomingSignal,
    StrategyState,
)
from app.execution.worker import ExecutionWorker, status_for

__all__ = [
    "DEFAULT_MAX_SIGNAL_AGE_SECONDS",
    "AiFilter",
    "AiVerdict",
    "NO_ORDER",
    "ORDER_UNSETTLED",
    "ExecutionPipeline",
    "ExecutionResult",
    "ExecutionWorker",
    "IncomingSignal",
    "Outcome",
    "StrategyState",
    "created_an_order",
    "status_for",
]
