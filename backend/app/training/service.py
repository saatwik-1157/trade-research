"""The training job: queue it, run it in the background, and hand off a candidate.

**No new queue.** Section 4 says reuse the background job system if one exists,
and one does: `BacktestService` and `ReplayService` have run background
`asyncio` tasks bounded by a semaphore since L14 and L15, on the same
`async_sessionmaker` pattern. This service is shaped like them deliberately —
same queue/cancel/execute skeleton, same "a failed run must never look
completed" rule — because a fourth way of running a background job in one
codebase is three too many.

**The job never runs in the request.** `queue()` writes a row, starts a task
and returns immediately.

**Training produces a candidate and nothing else.** Sections 25 and 26. The
model version it writes is a `draft`, and the job ends at `validation_pending`
— a status that means "a candidate exists", not "a model is approved". There is
no code path from this module to the registry's `promoted`, to a strategy, to
the risk engine or to an order; a test parses every module to keep it that way.

**The dataset is locked, and the lock is checked.** The job records the
dataset's fingerprint before fitting and re-derives it after. If the data moved
under a running job, the job FAILS rather than recording a model whose
provenance is a guess.

**Cancellation is cooperative.** A flag is checked between stages and between
gradient iterations, so a cancelled job stops at a boundary with its logs
intact and is recorded as `cancelled` — never as trained.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import service as ai_service
from app.ai.probability import _sigmoid
from app.core.events import Event
from app.datasets.builder import Dataset
from app.datasets.scaler import fit as fit_scaler
from app.models.ai import TrainingRun
from app.realtime.catalogue import EventType
from app.training import gates, metrics, trainers
from app.training.config import (
    MAX_CONCURRENT,
    MAX_QUEUED_PER_USER,
    ModelFamily,
    TrainingConfig,
    TrainingError,
    TrainingStage,
    environment,
    progress_of,
)

log = logging.getLogger("app.training")

# The status vocabulary. Five existed on `training_runs` since L05; two are
# added. `validation_pending` is the terminus of a successful run -- section 25:
# "candidate model successfully trained" is not "model approved for trading".
#
# Deliberately NOT added: `paused` (nothing here can be paused -- these fits are
# seconds long and there is no checkpointing to resume from, so the state would
# be unreachable), and `validation_passed` / `validation_failed`, which are
# level 26's to write. Declaring a state nothing can enter makes the vocabulary
# a wish list, which is the reasoning L22 used on bot states and L23 on dataset
# ones.
QUEUED = "queued"
RUNNING = "running"
CANCELLING = "cancelling"
CANCELLED = "cancelled"
FAILED = "failed"
VALIDATION_PENDING = "validation_pending"

ACTIVE = (QUEUED, RUNNING, CANCELLING)


class TrainingBusy(TrainingError):
    """Too many runs in flight. A refusal, not a queue that grows forever."""


class DuplicateTrainingJob(TrainingError):
    """The same recipe is already running. Section 38."""


@dataclass
class _Cancellation:
    """The stop flag, owned by the job rather than looked up by id.

    Held directly by the running job and by the worker thread doing the fit.
    Looking it up in `self._running` instead was a real bug: the task's
    done-callback pops that entry, so a cancelled task's thread would find no
    handle, read "not cancelled" and run the fit to completion -- burning CPU
    long after the job was recorded as cancelled.
    """

    cancelled: bool = False


@dataclass
class _Handle:
    task: asyncio.Task | None = None
    flag: _Cancellation = field(default_factory=_Cancellation)

    @property
    def cancelled(self) -> bool:
        return self.flag.cancelled

    def stop(self) -> None:
        self.flag.cancelled = True


class TrainingService:
    """Queue, run, cancel. Shaped like `BacktestService` on purpose."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        hub: object | None = None,
    ) -> None:
        self.sessions = sessions
        self.hub = hub
        self._running: dict[str, _Handle] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    # ------------------------------------------------------------ queueing

    async def queue(
        self,
        db: AsyncSession,
        config: TrainingConfig,
        *,
        user_id: str,
        loader: Any,
    ) -> TrainingRun:
        """Record a queued job and start it. Returns the row immediately.

        `loader` is an awaitable taking a session and returning the built
        `Dataset`. Injected rather than imported so a caller can supply a
        prepared dataset in a test and the service never has to know how one is
        produced — which is also what keeps this module free of a second
        dataset-building path.
        """
        recipe = config.fingerprint()

        in_flight = await db.scalar(
            select(func.count())
            .select_from(TrainingRun)
            .where(TrainingRun.requested_by_user_id == user_id, TrainingRun.status.in_(ACTIVE))
        )
        if int(in_flight or 0) >= MAX_QUEUED_PER_USER:
            raise TrainingBusy(
                f"{in_flight} of your training jobs are already queued or running; the "
                f"limit is {MAX_QUEUED_PER_USER}. Training is bounded so one caller "
                "cannot exhaust the machine."
            )

        duplicate = await db.scalar(
            select(TrainingRun).where(
                TrainingRun.config_fingerprint == recipe, TrainingRun.status.in_(ACTIVE)
            )
        )
        if duplicate is not None:
            raise DuplicateTrainingJob(
                f"training job {duplicate.id} is already running the same model, dataset, "
                "feature version, label version and configuration. Refused rather than "
                "started: two identical fits produce two identical candidates and one "
                "wasted machine."
            )

        row = TrainingRun(
            model_key=str(config.family),
            model_version_label=config.model_version,
            dataset_ref=f"{config.dataset_key}:{config.dataset_version}",
            config_fingerprint=recipe,
            params={"config": config.as_dict(), "environment": environment()},
            status=QUEUED,
            current_stage=str(TrainingStage.queued),
            progress=Decimal("0"),
            random_seed=config.random_seed,
            requested_by_user_id=user_id,
        )
        db.add(row)
        await db.flush()
        job_id = row.id
        await db.commit()

        await self._publish("training.job.created", job_id, {"family": str(config.family)})

        handle = _Handle()
        self._running[job_id] = handle
        task = asyncio.create_task(
            self._execute(job_id, config, loader, handle), name=f"training:{job_id}"
        )
        handle.task = task
        task.add_done_callback(lambda _t: self._running.pop(job_id, None))
        return row

    async def cancel(self, job_id: str) -> bool:
        """Ask a running job to stop at the next boundary.

        Cooperative rather than a killed task: the flag is checked between
        stages and between gradient iterations, so the row is written by the
        job itself and a cancelled run is never left looking half-trained.
        """
        handle = self._running.get(job_id)
        if handle is None or handle.task is None or handle.task.done():
            return False
        handle.stop()
        async with self.sessions() as db:
            row = await db.get(TrainingRun, job_id)
            if row is not None and row.status in (QUEUED, RUNNING):
                row.status = CANCELLING
                await db.commit()
        await self._publish("training.job.cancelling", job_id, {})
        return True

    def is_cancelled(self, job_id: str) -> bool:
        handle = self._running.get(job_id)
        return bool(handle and handle.cancelled)

    # The running job reads its OWN flag rather than looking itself up, so a
    # popped registry entry cannot make a cancelled fit look uncancelled.

    def running(self) -> list[str]:
        return sorted(self._running)

    async def wait_for(self, job_id: str, *, timeout: float = 120.0) -> bool:
        """Await one job's task. True if it finished, False if it is not here.

        Public because polling a status column to find out whether a background
        job is done is exactly the pattern L22 spent a level arguing against: a
        row says what the last writer believed, and the task says what actually
        happened.
        """
        handle = self._running.get(job_id)
        if handle is None or handle.task is None:
            return False
        await asyncio.wait([handle.task], timeout=timeout)
        return handle.task.done()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop every running job and wait for it, like replay and paper do.

        Without this a process could exit with fits still running on worker
        threads, and a half-written job row would be the only trace. The
        application's lifespan calls it beside `replay.shutdown()` and
        `paper.shutdown()`, which have done the same since L15 and L16.
        """
        for handle in list(self._running.values()):
            handle.stop()
        tasks = [
            h.task for h in list(self._running.values()) if h.task is not None and not h.task.done()
        ]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------- running

    async def _execute(
        self, job_id: str, config: TrainingConfig, loader: Any, handle: _Handle
    ) -> None:
        async with self._semaphore:
            async with self.sessions() as db:
                row = await db.get(TrainingRun, job_id)
                if row is None or row.status in (CANCELLED, CANCELLING):
                    await self._finish(job_id, CANCELLED, stage=TrainingStage.queued)
                    return
                row.status = RUNNING
                row.started_at = _now()
                await db.commit()
            await self._publish("training.job.started", job_id, {})

            try:
                result = await self._run(job_id, config, loader, handle)
            except asyncio.CancelledError:
                await self._finish(job_id, CANCELLED, stage=TrainingStage.queued)
                raise
            except TrainingError as exc:
                await self._fail(job_id, f"{type(exc).__name__}: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - recorded as failed, never as done
                log.warning(
                    "training failed",
                    extra={
                        "event": "training_failed",
                        "job_id": job_id,
                        "error": type(exc).__name__,
                    },
                )
                await self._fail(job_id, f"{type(exc).__name__}: {exc}"[:500])
                return

            if result is None:  # cancelled mid-run
                await self._finish(job_id, CANCELLED, stage=TrainingStage.queued)
                await self._publish("training.job.cancelled", job_id, {})
                return

            await self._publish(
                "training.job.completed",
                job_id,
                {"candidate": result.get("model_version_id")},
            )
            await self._publish("training.validation.pending", job_id, {})

    async def _run(
        self, job_id: str, config: TrainingConfig, loader: Any, handle: _Handle
    ) -> dict[str, Any] | None:
        """The pipeline. Returns None if cancellation was observed."""

        async def stage(name: TrainingStage) -> bool:
            """Advance the stage; return False if the job should stop."""
            if handle.cancelled:
                return False
            async with self.sessions() as db:
                row = await db.get(TrainingRun, job_id)
                if row is not None:
                    row.current_stage = str(name)
                    row.progress = Decimal(str(progress_of(name)))
                    await db.commit()
            await self._publish(
                "training.job.progress", job_id, {"stage": str(name), "progress": progress_of(name)}
            )
            return True

        if not await stage(TrainingStage.loading_dataset):
            return None
        async with self.sessions() as db:
            dataset: Dataset = await loader(db)
        locked_fingerprint = dataset.fingerprint

        if not await stage(TrainingStage.quality_gates):
            return None
        rows = [row.features for row in dataset.rows]
        targets = _targets_for(config.family, dataset)
        report = gates.evaluate(dataset, config, targets=targets)
        if not report.passed:
            raise TrainingError(
                "data quality gates refused this run: "
                + "; ".join(f"{g.check}: {g.detail}" for g in report.failures())
            )

        if not await stage(TrainingStage.splitting):
            return None
        split = dataset.split
        assert split is not None  # the gate above required it
        features = config.features or tuple(sorted(rows[0]))

        if not await stage(TrainingStage.preprocessing):
            return None
        # Fitted on the training segment only. `fit()` takes the split and reads
        # `split.train`; there is no spelling for fitting on everything.
        scaler = fit_scaler(
            rows,
            split,
            feature_version=str(
                (dataset.manifest().get("config") or {}).get("feature_set_version")
            ),
            features=features,
        )
        scaled = scaler.transform(rows)

        train_slice = slice(*split.train)
        validation_slice = slice(*split.validation)
        test_slice = slice(*split.test)

        if not await stage(TrainingStage.fitting_baseline):
            return None
        baseline_metrics: dict[str, Any] = {}
        candidate_metrics: dict[str, Any] = {}
        comparison: dict[str, Any] = {}
        fit_report: dict[str, Any] = {}
        coefficients = None

        if config.family is ModelFamily.trade_probability:
            assert targets is not None
            baseline = trainers.fit_majority(targets[train_slice])
            test_targets = targets[test_slice]
            baseline_probabilities = [baseline.probability()] * len(test_targets)
            baseline_metrics = metrics.classification(baseline_probabilities, test_targets)
            baseline_metrics["model"] = baseline.as_dict()

            if not await stage(TrainingStage.fitting_candidate):
                return None
            train_x, train_kept = trainers.vectorise(scaled[train_slice], features)
            validation_x, validation_kept = trainers.vectorise(scaled[validation_slice], features)
            train_y = [targets[train_slice][i] for i in train_kept]
            validation_y = [targets[validation_slice][i] for i in validation_kept]
            # OFF THE EVENT LOOP. The fit is a synchronous CPU-bound loop, and
            # a background asyncio task is not enough on its own: an `await`-less
            # loop inside one starves every other request in the process for as
            # long as it runs. Section 4 asks that training not run inside an
            # HTTP request, and running it on the same thread as every HTTP
            # request is the same problem wearing a different hat. Found when a
            # 4000-iteration fit froze the suite's other jobs.
            coefficients, report_obj = await asyncio.to_thread(
                trainers.fit_weighted_logistic,
                train_x,
                train_y,
                features=features,
                config=config,
                validation_rows=validation_x,
                validation_targets=validation_y,
                should_stop=lambda: handle.cancelled,
            )
            if report_obj.stopped_because == "cancelled":
                return None
            fit_report = report_obj.as_dict()
        else:
            if not await stage(TrainingStage.fitting_candidate):
                return None

        if not await stage(TrainingStage.evaluating):
            return None
        model = await asyncio.to_thread(
            trainers.build_model,
            config.family,
            config=config,
            feature_version=scaler.feature_version,
            dataset_version=config.dataset_version,
            dataset_fingerprint=locked_fingerprint,
            coefficients=coefficients,
            train_rows=rows[train_slice],
            scaler=scaler,
        )

        calibration_report: dict[str, Any] = {}
        economics: dict[str, Any] = {}
        if config.family is ModelFamily.trade_probability and coefficients is not None:
            assert targets is not None
            test_x, test_kept = trainers.vectorise(scaled[test_slice], features)
            test_y = [targets[test_slice][i] for i in test_kept]
            probabilities = [
                _sigmoid(
                    coefficients.bias
                    + sum(w * v for w, v in zip(coefficients.weights, values, strict=True))
                )
                for values in test_x
            ]
            candidate_metrics = metrics.classification(probabilities, test_y)
            calibration_report = metrics.calibration(probabilities, test_y)
            comparison = metrics.compare(candidate_metrics, baseline_metrics)
            economics = metrics.economic(
                [
                    float(dataset.rows[split.test[0] + i].labels["forward_return"])
                    for i, p in zip(test_kept, probabilities, strict=True)
                    if p >= 0.5
                    and dataset.rows[split.test[0] + i].labels.get("forward_return") is not None
                ]
            )

        # Section 6, checked rather than assumed: re-derive the dataset's
        # identity and fail if it moved under the running job.
        async with self.sessions() as db:
            again: Dataset = await loader(db)
        if again.fingerprint != locked_fingerprint:
            raise TrainingError(
                f"the dataset changed while the job ran: locked {locked_fingerprint[:12]}, "
                f"now {again.fingerprint[:12]}. The candidate is discarded rather than "
                "recorded against provenance that is a guess."
            )

        if not await stage(TrainingStage.recording):
            return None
        artifact = trainers.artifact_of(model)
        async with self.sessions() as db:
            version = await ai_service.register_version(
                db,
                model,
                params={
                    "artifact": artifact,
                    "training_config": config.as_dict(),
                    "environment": environment(),
                    "scaler": scaler.as_dict(),
                    "feature_schema": list(features),
                },
                metrics={
                    "classification": candidate_metrics,
                    "baseline": baseline_metrics,
                    "comparison": comparison,
                    "calibration": calibration_report,
                    "economic": economics,
                    "fit": fit_report,
                    "separation": (
                        "ML metrics and economic metrics are reported separately and are "
                        "never merged. A model can score well on the first and lose money "
                        "on the second; this repository has measured exactly that."
                    ),
                },
                code_version=environment()["training_engine_version"],
                training_period=_period(dataset, split.train),
                validation_period=_period(dataset, split.validation),
                test_period=_period(dataset, split.test),
            )
            row = await db.get(TrainingRun, job_id)
            if row is not None:
                row.model_version_id = version.id
                row.dataset_fingerprint = locked_fingerprint
                row.metrics = {
                    "classification": candidate_metrics,
                    "baseline": baseline_metrics,
                    "comparison": comparison,
                    "calibration": calibration_report,
                    "economic": economics,
                    "fit": fit_report,
                    "gates": report.as_dict(),
                    "separation": (
                        "ML metrics and economic metrics are reported separately and are "
                        "never merged. A model can score well on the first and lose money "
                        "on the second; this repository has measured exactly that."
                    ),
                }
                row.status = VALIDATION_PENDING
                row.current_stage = str(TrainingStage.done)
                row.progress = Decimal("1")
                row.finished_at = _now()
                await db.commit()

        return {"model_version_id": version.id}

    # ------------------------------------------------------------ recording

    async def _fail(self, job_id: str, error: str) -> None:
        async with self.sessions() as db:
            row = await db.get(TrainingRun, job_id)
            if row is not None:
                # A failed run must never look completed, and must never leave a
                # candidate behind that something could mistake for a result.
                row.status = FAILED
                row.error = error[:1000]
                row.finished_at = _now()
                await db.commit()
        await self._publish("training.job.failed", job_id, {"error": error[:200]})

    async def _finish(self, job_id: str, status: str, *, stage: TrainingStage) -> None:
        async with self.sessions() as db:
            row = await db.get(TrainingRun, job_id)
            if row is not None and row.status not in (VALIDATION_PENDING, FAILED):
                row.status = status
                row.finished_at = _now()
                await db.commit()

    async def _publish(self, event: str, job_id: str, payload: dict[str, Any]) -> None:
        """Through L07's hub, never a second realtime system (§30)."""
        if self.hub is None:
            return
        # ONE argument. `Hub.publish` takes an `Event`, and this call passed two
        # positional arguments from L25 until L28 -- a TypeError raised on every
        # publish, caught by the `except` below and logged as a warning, so the
        # events never reached the bus and the failure looked like silence. The
        # event type is catalogued now too; an uncatalogued one is refused.
        try:
            await self.hub.publish(  # type: ignore[attr-defined]
                Event(
                    type=str(EventType.TRAINING_JOB_UPDATED),
                    payload={"job_id": job_id, "event": event, **payload},
                    source="training",
                    channel=f"model:{payload.get('model') or 'training'}",
                    correlation_id=job_id,
                )
            )
        except Exception:  # noqa: BLE001 - a training run does not fail because an event did
            log.warning(
                "training event not published",
                extra={"event": "training_event_failed", "job_id": job_id, "type": event},
            )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _period(dataset: Dataset, bounds: tuple[int, int]) -> tuple[datetime, datetime] | None:
    start, stop = bounds
    if stop <= start or stop > len(dataset.rows):
        return None
    return (dataset.rows[start].at, dataset.rows[stop - 1].at)


def _targets_for(family: ModelFamily, dataset: Dataset) -> list[int] | None:
    """Binary targets for the families that have one.

    Only the probability model is supervised. The regime and anomaly models fit
    unsupervised statistics of the training segment, so they have no target and
    the class-balance gates do not apply to them -- returning None says that,
    rather than inventing a label to keep a code path uniform.
    """
    if family is not ModelFamily.trade_probability:
        return None
    return [1 if row.labels.get("bracket_outcome") == "WIN" else 0 for row in dataset.rows]


def summarise(row: TrainingRun) -> dict[str, Any]:
    """One job as an API row."""
    return {
        "id": row.id,
        "model": row.model_key,
        "model_version": row.model_version_label,
        "dataset": row.dataset_ref,
        "dataset_fingerprint": row.dataset_fingerprint,
        "config_fingerprint": row.config_fingerprint,
        "status": row.status,
        "stage": row.current_stage,
        "progress": float(row.progress) if row.progress is not None else None,
        "random_seed": row.random_seed,
        "model_version_id": row.model_version_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "error": row.error,
        "candidate_only": (
            "validation_pending means a candidate exists. It does not mean the model is "
            "approved for trading: L26 validates and L28 promotes."
        ),
    }
