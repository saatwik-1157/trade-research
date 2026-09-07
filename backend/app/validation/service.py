"""The validation job: queue it, run it in the background, record a verdict.

**No new queue and no new engine.** Section 4. `BacktestService`, `ReplayService`
and `TrainingService` have run background `asyncio` tasks bounded by a semaphore
since L14; this is the fourth instance of that shape and deliberately not a
fifth pattern. The economics come from `tools/rule_backtest.simulate`, the
clustering from `tools/rule_search`, the calibration arithmetic from
`app/monitoring/stats.py`, and the leakage verdict from L23's own report.

**Validation has zero ability to trade.** This module imports no order manager,
no broker adapter, no risk engine, no position sizer and no strategy runner, and
it writes no column that any of them read. A test parses every module in the
package with `ast` and fails on such an import — the same test L25 has, because
"we would notice" is not a control.

**Validation does not activate anything.** Section 34. The only rows written are
in `validation_runs`. `model_versions.status` is never touched: a PASS makes a
candidate eligible for CONSIDERATION by the registry, and promotion is L28's
decision, taken by a human.

**Every figure is measured or the check is BLOCKED.** Section 41. There is no
default profit factor, no assumed AUC and no hardcoded verdict. When the inputs
cannot support a check it says so, and `verdict_from()` makes one BLOCKED check
block the report — because "we could not tell" reported as PASS or FAIL is a
claim about the model that the evidence does not support.

**The dataset is re-derived and the fingerprint re-checked**, exactly as
training does: if the data moved under the candidate, the run BLOCKS rather than
producing a report against provenance that is a guess.
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

from app.ai.loader import ArtifactError, model_from_version
from app.ai.probability import TradeProbabilityModel, _sigmoid
from app.core.events import Event
from app.datasets.builder import Dataset
from app.datasets.scaler import Scaler
from app.marketdata.types import Bar
from app.models.ai import ModelVersion
from app.models.validation import ValidationRun
from app.realtime.catalogue import EventType
from app.validation import checks, economics, statistics
from app.validation.config import (
    MAX_CONCURRENT,
    MAX_QUEUED_PER_USER,
    VALIDATION_ENGINE_VERSION,
    Severity,
    ValidationConfig,
    ValidationError,
    ValidationStage,
    Verdict,
    progress_of,
)
from app.validation.report import ValidationReport

log = logging.getLogger("app.validation")

QUEUED = "queued"
RUNNING = "running"
CANCELLING = "cancelling"
CANCELLED = "cancelled"
FAILED = "failed"
# Where a run that produced a report ends, whatever that report concluded.
# There is deliberately no `passed` status on the RUN: a run succeeded or it did
# not, and what it found is the `verdict` column. Collapsing the two is how
# "the validation passed" comes to mean "the model passed".
COMPLETED = "completed"

ACTIVE = (QUEUED, RUNNING, CANCELLING)


class ValidationBusy(ValidationError):
    """Too many runs in flight. A refusal, not a queue that grows forever."""


class DuplicateValidationJob(ValidationError):
    """The same candidate is already being validated under the same thresholds."""


@dataclass
class _Cancellation:
    """The stop flag, owned by the job rather than looked up by id.

    L25's fix, kept: the task's done-callback pops the handle, so a worker
    thread that looks the flag up by job id reads "not cancelled" and runs to
    completion long after the job was recorded as stopped.
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


class ValidationService:
    """Queue, run, cancel. Shaped like `TrainingService` on purpose."""

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
        config: ValidationConfig,
        *,
        loader: Any,
        user_id: str | None = None,
        dataset_id: str | None = None,
        training_run_id: str | None = None,
    ) -> ValidationRun:
        """Write a row, start a task, return immediately. Nothing runs here."""
        fingerprint = config.fingerprint()

        active = await db.scalar(
            select(func.count())
            .select_from(ValidationRun)
            .where(
                ValidationRun.config_fingerprint == fingerprint,
                ValidationRun.status.in_(ACTIVE),
            )
        )
        if active:
            raise DuplicateValidationJob(
                f"a validation of {config.model_version_id} under these exact thresholds "
                "is already running. Two identical runs would produce two identical "
                "reports and a question about which one is the answer."
            )

        if user_id is not None:
            queued = await db.scalar(
                select(func.count())
                .select_from(ValidationRun)
                .where(
                    ValidationRun.requested_by_user_id == user_id,
                    ValidationRun.status.in_(ACTIVE),
                )
            )
            if queued and queued >= MAX_QUEUED_PER_USER:
                raise ValidationBusy(
                    f"{queued} validation runs are already queued for this user, which is "
                    f"the limit of {MAX_QUEUED_PER_USER}. A queue that grows without a "
                    "bound is a denial of service with extra steps."
                )

        row = ValidationRun(
            model_version_id=config.model_version_id,
            training_run_id=training_run_id,
            dataset_id=dataset_id,
            status=QUEUED,
            config=config.as_dict(),
            config_fingerprint=fingerprint,
            validation_engine_version=VALIDATION_ENGINE_VERSION,
            current_stage=str(ValidationStage.queued),
            progress=Decimal(str(progress_of(ValidationStage.queued))),
            requested_by_user_id=user_id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        job_id = row.id
        handle = _Handle()
        self._running[job_id] = handle
        handle.task = asyncio.create_task(self._execute(job_id, config, loader, handle))
        handle.task.add_done_callback(lambda _t: self._running.pop(job_id, None))
        await self._publish("validation.job.queued", row.id, {})
        return row

    async def cancel(self, job_id: str) -> bool:
        handle = self._running.get(job_id)
        if handle is None:
            return False
        handle.stop()
        async with self.sessions() as db:
            row = await db.get(ValidationRun, job_id)
            if row is not None and row.status in (QUEUED, RUNNING):
                row.status = CANCELLING
                row.cancelled_at = _now()
                await db.commit()
        return True

    def is_cancelled(self, job_id: str) -> bool:
        handle = self._running.get(job_id)
        return handle.cancelled if handle else False

    def running(self) -> list[str]:
        return sorted(self._running)

    async def wait_for(self, job_id: str, *, timeout: float = 120.0) -> bool:
        """Await one job. Used by tests instead of polling the database."""
        handle = self._running.get(job_id)
        if handle is None or handle.task is None:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(handle.task), timeout=timeout)
        except asyncio.CancelledError:
            return True
        return True

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        tasks = [h.task for h in self._running.values() if h.task is not None]
        for handle in self._running.values():
            handle.stop()
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------- running

    async def _execute(
        self, job_id: str, config: ValidationConfig, loader: Any, handle: _Handle
    ) -> None:
        async with self._semaphore:
            async with self.sessions() as db:
                row = await db.get(ValidationRun, job_id)
                if row is None or row.status in (CANCELLED, CANCELLING):
                    await self._finish_cancelled(job_id)
                    return
                row.status = RUNNING
                row.started_at = _now()
                await db.commit()
            await self._publish("validation.job.started", job_id, {})

            try:
                report = await self._run(job_id, config, loader, handle)
            except asyncio.CancelledError:
                await self._finish_cancelled(job_id)
                raise
            except Exception as exc:  # noqa: BLE001 - recorded as failed, never as passed
                log.warning(
                    "validation failed",
                    extra={
                        "event": "validation_failed",
                        "job_id": job_id,
                        "error": type(exc).__name__,
                    },
                )
                await self._fail(job_id, f"{type(exc).__name__}: {exc}"[:500])
                return

            if report is None:  # cancelled mid-run
                await self._finish_cancelled(job_id)
                await self._publish("validation.job.cancelled", job_id, {})
                return

            await self._record(job_id, report)
            await self._publish(
                "validation.job.completed",
                job_id,
                {"verdict": str(report.verdict), "summary": report.summary_line()},
            )

    async def _run(
        self, job_id: str, config: ValidationConfig, loader: Any, handle: _Handle
    ) -> ValidationReport | None:
        """The pipeline. Returns None if cancellation was observed."""
        thresholds = config.thresholds
        report = ValidationReport(config=config)

        async def stage(name: ValidationStage) -> bool:
            if handle.cancelled:
                return False
            async with self.sessions() as db:
                row = await db.get(ValidationRun, job_id)
                if row is not None:
                    row.current_stage = str(name)
                    row.progress = Decimal(str(progress_of(name)))
                    await db.commit()
            await self._publish(
                "validation.job.progress",
                job_id,
                {"stage": str(name), "progress": progress_of(name)},
            )
            return True

        # --------------------------------------------------------- loading
        if not await stage(ValidationStage.loading):
            return None
        async with self.sessions() as db:
            version = await db.get(ModelVersion, config.model_version_id)
            if version is None:
                raise ValidationError(
                    f"no model version {config.model_version_id}. A report cannot be "
                    "produced for a candidate that does not exist."
                )
            stored_metrics = dict(version.metrics or {})
            bars, dataset = await loader(db)

        locked_fingerprint = dataset.fingerprint
        report.context = {
            "model_version_id": version.id,
            "model_key": (version.artifact_ref or "").split(":", 1)[0] or None,
            "dataset_fingerprint": locked_fingerprint,
            "dataset_rows": len(dataset.rows),
            "training_metrics_present": sorted(stored_metrics),
        }

        # -------------------------------------------------- data integrity
        if not await stage(ValidationStage.data_integrity):
            return None
        report.add(checks.data_integrity(dataset, thresholds))

        # --------------------------------------------------- model artifact
        if not await stage(ValidationStage.artifact):
            return None
        try:
            model = model_from_version(version)
        except ArtifactError as exc:
            # A candidate whose artifact will not load is BLOCKED, not FAIL:
            # nothing about the model has been measured.
            report.add(checks.Finding("model_artifact", Severity.blocked, str(exc)))
            report.finished_at = _now()
            return report

        feature_version = str(
            (dataset.manifest().get("config") or {}).get("feature_set_version") or ""
        )
        report.add(checks.model_artifact(model, dataset, feature_version))
        report.add(checks.version_locking(model, dataset))

        # ---------------------------------------------------------- leakage
        if not await stage(ValidationStage.leakage):
            return None
        report.add(checks.leakage(dataset))

        if not await stage(ValidationStage.temporal):
            return None
        report.add(checks.temporal(dataset))

        split = dataset.split
        if split is None:
            report.finished_at = _now()
            return report

        # ------------------------------------------------ scoring the model
        # Everything below is measured on the TEST segment only -- the one the
        # training job never saw. Section 12.
        scored = await asyncio.to_thread(_score_segment, model, dataset, bars, version, split.test)
        # The training segment too, and measured here rather than read from the
        # training job's record: L25 stores an early-stopping figure from the
        # VALIDATION segment, which is not an in-sample one. Section 19 asks how
        # much worse the holdout is than the data the model was fitted on, and
        # the only honest way to answer is to score both with the same metric.
        in_sample = await asyncio.to_thread(
            _score_segment, model, dataset, bars, version, split.train
        )
        report.context["scored"] = {
            "rows": len(scored["probabilities"]),
            "segment": "test",
            "segment_rows": list(split.test),
            "why": (
                "the final test segment, which the training job never saw and which is "
                "used once. Section 12: a holdout consulted during development is not a "
                "holdout."
            ),
        }

        probabilities: list[float] = scored["probabilities"]
        outcomes: list[int] = scored["outcomes"]

        # ------------------------------------------------------- baselines
        if not await stage(ValidationStage.baseline):
            return None
        report.add(
            checks.baseline_comparison(
                dict(stored_metrics.get("classification") or {}),
                dict(stored_metrics.get("baseline") or {}),
                thresholds,
            )
        )

        auc = statistics.roc_auc(probabilities, outcomes) if probabilities else 0.5
        report.add(checks.discrimination(auc, len(probabilities), thresholds))

        # AUC on both segments, higher-is-better, so the gap is train minus test
        # and it is the classic overfitting signature: a model that ranks well
        # on what it was fitted on and no better than chance on what it was not.
        train_auc = (
            statistics.roc_auc(in_sample["probabilities"], in_sample["outcomes"])
            if len(in_sample["probabilities"]) >= 20
            else None
        )
        report.add(
            checks.overfitting(
                train_auc,
                auc if len(probabilities) >= 20 else None,
                thresholds,
            )
        )
        report.context["in_sample"] = {
            "rows": len(in_sample["probabilities"]),
            "auc": None if train_auc is None else round(train_auc, 6),
            "segment": "train",
            "why": (
                "measured here rather than read from the training record, which stores "
                "an early-stopping figure from the VALIDATION segment -- not an "
                "in-sample one."
            ),
        }

        # ----------------------------------------------------- calibration
        if not await stage(ValidationStage.calibration):
            return None
        report.add(checks.calibration(probabilities, outcomes, thresholds))

        # -------------------------------------------------------- economic
        if not await stage(ValidationStage.economic):
            return None
        summary = await asyncio.to_thread(
            economics.evaluate,
            scored["bars"],
            scored["by_index"],
            threshold=config.decision_threshold,
            spread=scored["spread"],
            stop_atr=scored["stop_atr"],
            take_profit_atr=scored["take_profit_atr"],
            atr_period=scored["atr_period"],
            max_hold=scored["max_hold"],
            symbol=dataset.config.symbol,
        )
        report.add(checks.economic(summary, thresholds))
        report.add(
            checks.sample_size(len(probabilities), int(summary.get("trades") or 0), thresholds)
        )
        report.context["economic"] = {k: v for k, v in summary.items() if k not in ("rows",)}
        report.context["clustered"] = statistics.clustered(summary.get("rows") or [])
        report.context["pooled"] = statistics.pooled_t(
            [row["net"] for row in (summary.get("rows") or [])]
        )
        report.context["bootstrap"] = statistics.bootstrap_interval(
            [row["net"] for row in (summary.get("rows") or [])]
        )

        # ---------------------------------------------------- walk-forward
        if not await stage(ValidationStage.walk_forward):
            return None
        report.add(checks.walk_forward(_walk_forward_windows(probabilities, outcomes), thresholds))

        # ------------------------------------------------------ robustness
        if not await stage(ValidationStage.robustness):
            return None
        probes = await asyncio.to_thread(
            _robustness_probes, scored, config, thresholds, dataset.config.symbol
        )
        report.add(checks.robustness(probes, thresholds))

        # ---------------------------------------------------------- regime
        if not await stage(ValidationStage.regime):
            return None
        report.add(checks.regime(_regime_breakdown(scored, summary), thresholds))

        # ---------------------------------------------------- significance
        if not await stage(ValidationStage.significance):
            return None
        permutation = None
        if len(probabilities) >= 20:
            permutation = await asyncio.to_thread(
                statistics.permutation_test,
                probabilities,
                outcomes,
                statistics.roc_auc,
                permutations=thresholds.permutations,
                alpha=thresholds.significance_alpha,
                candidates_tried=thresholds.candidates_tried,
            )
        report.add(checks.significance(permutation, thresholds))

        # --------------------------------------------------------- closing
        if not await stage(ValidationStage.reporting):
            return None

        # Section 6, checked rather than assumed, exactly as training does:
        # re-derive the dataset and refuse a report against data that moved.
        async with self.sessions() as db:
            _, again = await loader(db)
        if again.fingerprint != locked_fingerprint:
            report.add(
                checks.Finding(
                    "version_locking",
                    Severity.blocked,
                    f"the dataset changed while validation ran: locked "
                    f"{locked_fingerprint[:12]}, now {again.fingerprint[:12]}. The report "
                    "is blocked rather than published against provenance that is a guess.",
                )
            )

        await stage(ValidationStage.done)
        report.finished_at = _now()
        return report

    # ------------------------------------------------------------ recording

    async def _record(self, job_id: str, report: ValidationReport) -> None:
        payload = report.as_dict()
        counts = report.counts()
        async with self.sessions() as db:
            row = await db.get(ValidationRun, job_id)
            if row is None:
                return
            row.status = COMPLETED
            row.verdict = str(report.verdict)
            row.summary = report.summary_line()[:2000]
            row.report = payload
            row.dataset_fingerprint = report.context.get("dataset_fingerprint")
            row.checks_passed = counts[str(Severity.passed)]
            row.checks_failed = counts[str(Severity.failed)]
            row.checks_warning = counts[str(Severity.warning)]
            row.checks_blocked = counts[str(Severity.blocked)]
            row.current_stage = str(ValidationStage.done)
            row.progress = Decimal("1")
            row.finished_at = _now()
            await db.commit()

    async def _fail(self, job_id: str, error: str) -> None:
        async with self.sessions() as db:
            row = await db.get(ValidationRun, job_id)
            if row is not None:
                # A failed run must never look like a verdict. No report, no
                # verdict, and the CHECK constraint would refuse one anyway.
                row.status = FAILED
                row.error = error[:1000]
                row.finished_at = _now()
                await db.commit()
        await self._publish("validation.job.failed", job_id, {"error": error[:200]})

    async def _finish_cancelled(self, job_id: str) -> None:
        async with self.sessions() as db:
            row = await db.get(ValidationRun, job_id)
            if row is not None and row.status not in (COMPLETED, FAILED):
                row.status = CANCELLED
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
                    type=str(EventType.VALIDATION_RUN_UPDATED),
                    payload={"job_id": job_id, "event": event, **payload},
                    source="validation",
                    channel=f"model:{payload.get('model') or 'validation'}",
                    correlation_id=job_id,
                )
            )
        except Exception:  # noqa: BLE001 - a validation run does not fail because an event did
            log.warning(
                "validation event not published",
                extra={"event": "validation_event_failed", "job_id": job_id, "type": event},
            )


# ================================================================ scoring


def _score_segment(
    model: Any,
    dataset: Dataset,
    bars: list[Bar],
    version: ModelVersion,
    bounds: tuple[int, int],
) -> dict[str, Any]:
    """Every prediction the model makes over one segment of the dataset.

    Synchronous and CPU-bound, so it runs in a thread. L25's lesson: an
    `await`-less loop inside a background task starves every request in the
    process for as long as it runs.

    The preprocessing applied is the one the model was FITTED with, never a
    refit: refitting on a holdout would standardise it by its own statistics,
    which is the leak `Scaler.fit(rows, split)` is shaped to make unspellable.
    """
    start, stop = bounds
    rows = dataset.rows[start:stop]

    scaler = _scaler_of(version)
    label = dataset.config.label_config

    probabilities: list[float] = []
    outcomes: list[int] = []
    by_index: dict[int, float] = {}
    # Rows are aligned to bars by TIME, never by position. The builder drops a
    # warm-up prefix and a horizon suffix, so dataset index N is not bar index
    # N, and treating it as one would shift every simulated entry by the warm-up
    # length -- silently, and in a direction that looks like a result.
    bar_index = {bar.bar_time: index for index, bar in enumerate(bars)}

    features = list(model.contract.features)
    for row in rows:
        values = dict(row.features)
        if scaler is not None:
            values = scaler.transform([values])[0]
        if any(values.get(name) is None for name in features):
            continue
        if isinstance(model, TradeProbabilityModel) and model.coefficients is not None:
            coefficients = model.coefficients
            inputs = [values.get(name) for name in coefficients.features]
            if any(value is None for value in inputs):
                # The gate above covers `model.contract.features`; a coefficient
                # naming something outside it would otherwise be scored as zero,
                # which is a substituted value dressed as arithmetic.
                continue
            z = float(coefficients.bias)
            for weight, value in zip(coefficients.weights, inputs, strict=True):
                z += weight * float(value)  # type: ignore[arg-type]
            probability = _sigmoid(z)
        else:
            # A family with no probability output is not scored as if it had
            # one. The checks that need probabilities will BLOCK on the empty
            # list, which is the honest answer rather than a substituted 0.5.
            continue

        index = bar_index.get(row.at)
        if index is None:
            # A scored row whose bar is not in this window is skipped rather
            # than placed at a guessed index.
            continue

        outcome = row.labels.get("bracket_outcome")
        if outcome not in ("WIN", "LOSS"):
            # AMBIGUOUS and TIMEOUT bars are excluded from the ML scoring
            # because they are not a binary outcome. They stay in the economic
            # simulation, where `simulate()` decides what happened to them.
            by_index[index] = probability
            continue
        probabilities.append(probability)
        outcomes.append(1 if outcome == "WIN" else 0)
        by_index[index] = probability

    return {
        "probabilities": probabilities,
        "outcomes": outcomes,
        "by_index": by_index,
        "bars": bars,
        "spread": float(label.spread_points),
        "stop_atr": float(label.stop_atr),
        "take_profit_atr": float(label.take_profit_atr),
        "atr_period": int(label.atr_period),
        "max_hold": int(label.horizon),
        "rows": rows,
        "bar_index": bar_index,
    }


def _scaler_of(version: ModelVersion) -> Scaler | None:
    """The preprocessing the model was fitted with, rebuilt from the artifact.

    Applying the SAME parameters rather than refitting is the point: refitting
    on the test segment would standardise the holdout by its own statistics,
    which is the leak `Scaler.fit(rows, split)` is shaped to prevent.
    """
    stored = (version.params or {}).get("scaler")
    if not isinstance(stored, dict) or not stored.get("means"):
        return None
    fitted = stored.get("fitted_rows") or [0, 0]
    return Scaler(
        feature_version=str(stored.get("feature_version") or version.feature_version or ""),
        means={k: float(v) for k, v in dict(stored["means"]).items()},
        deviations={k: float(v) for k, v in dict(stored.get("deviations") or {}).items()},
        fitted_rows=(int(fitted[0]), int(fitted[1])),
        fitted_on=str(stored.get("fitted_on") or "train"),
        constant_features=tuple(stored.get("constant_features") or ()),
    )


# ============================================================ walk-forward


def _walk_forward_windows(
    probabilities: list[float], outcomes: list[int], windows: int = 4
) -> list[dict[str, Any]]:
    """AUC per sequential block of the test segment. Section 11.

    Sequential blocks, never shuffled: the whole reason to look is that
    performance concentrated in one era is an era rather than an edge, and
    shuffling would average exactly that away.
    """
    if len(probabilities) < windows * 10:
        return []
    size = len(probabilities) // windows
    out: list[dict[str, Any]] = []
    for index in range(windows):
        start = index * size
        stop = len(probabilities) if index == windows - 1 else (index + 1) * size
        block_p = probabilities[start:stop]
        block_o = outcomes[start:stop]
        positives = sum(block_o)
        metric = statistics.roc_auc(block_p, block_o) if 0 < positives < len(block_o) else None
        out.append(
            {
                "window": index + 1,
                "rows": len(block_p),
                "positives": positives,
                "metric": metric,
                "note": (
                    None
                    if metric is not None
                    else "one class only in this window; AUC is undefined rather than 0.5"
                ),
            }
        )
    return out


# ============================================================== robustness


def _robustness_probes(
    scored: dict[str, Any],
    config: ValidationConfig,
    thresholds: Any,
    symbol: str,
) -> list[dict[str, Any]]:
    """Move the threshold and the cost, and see whether the result survives.

    Section 22. Both probes matter for a measured reason: this repository's
    bracket sweep found 36 cells in which the best in-sample t was 0.83 while
    the RANDOM rule reached 1.76, so a result that exists at one setting and
    nowhere near it is the shape a fitted parameter takes.
    """
    base = config.decision_threshold
    probes: list[dict[str, Any]] = []

    settings: list[tuple[str, float, float]] = [
        ("baseline", base, scored["spread"]),
        ("threshold-", max(0.01, base - thresholds.threshold_probe), scored["spread"]),
        ("threshold+", min(0.99, base + thresholds.threshold_probe), scored["spread"]),
        (
            f"cost x{thresholds.cost_probe_multiplier:.2g}",
            base,
            scored["spread"] * thresholds.cost_probe_multiplier,
        ),
    ]

    for name, threshold, spread in settings:
        summary = economics.evaluate(
            scored["bars"],
            scored["by_index"],
            threshold=threshold,
            spread=spread,
            stop_atr=scored["stop_atr"],
            take_profit_atr=scored["take_profit_atr"],
            atr_period=scored["atr_period"],
            max_hold=scored["max_hold"],
            symbol=symbol,
        )
        trades = int(summary.get("trades") or 0)
        factor = summary.get("profit_factor")
        probes.append(
            {
                "probe": name,
                "threshold": round(threshold, 4),
                "spread": round(spread, 6),
                "trades": trades,
                "profit_factor": factor,
                # A probe with too few trades is not stable and not unstable;
                # it is unmeasured, and counting it either way would be a
                # claim the sample cannot support.
                "stable": bool(trades >= 5 and factor is not None and factor >= 1.0),
                "measured": trades >= 5,
            }
        )
    return [p for p in probes if p["measured"]]


# ================================================================== regime


def _regime_breakdown(scored: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Trades split by the volatility state of the bar they opened on.

    Uses the dataset's own `atr_pct_14` feature rather than asking a regime
    model, so the breakdown works for a deployment that has none. The cuts are
    terciles of the test segment itself and are reported with the counts, so a
    thin cell is visible as thin rather than quoted as a rate.
    """
    rows = summary.get("rows") or []
    if not rows:
        return []
    bar_index: dict[Any, int] = scored["bar_index"]

    volatility: dict[int, float] = {}
    for row in scored["rows"]:
        value = row.features.get("atr_pct_14")
        index = bar_index.get(row.at)
        if value is not None and index is not None:
            volatility[index] = float(value)
    if len(volatility) < 3:
        return []

    ordered = sorted(volatility.values())
    low = ordered[len(ordered) // 3]
    high = ordered[2 * len(ordered) // 3]

    buckets: dict[str, list[float]] = {
        "low_volatility": [],
        "mid_volatility": [],
        "high_volatility": [],
    }
    for trade in rows:
        value = volatility.get(int(trade["entry_idx"]))
        if value is None:
            continue
        name = (
            "low_volatility"
            if value <= low
            else ("high_volatility" if value > high else "mid_volatility")
        )
        buckets[name].append(float(trade["net"]))

    out: list[dict[str, Any]] = []
    for name, nets in buckets.items():
        wins = sum(n for n in nets if n > 0)
        losses = -sum(n for n in nets if n < 0)
        out.append(
            {
                "regime": name,
                "trades": len(nets),
                "net": round(sum(nets), 6),
                "profit_factor": (None if losses == 0 else round(wins / losses, 4)),
                "basis": "terciles of atr_pct_14 over the test segment",
            }
        )
    return out


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def summarise(row: ValidationRun) -> dict[str, Any]:
    """One run as an API row. The verdict, never a score."""
    return {
        "id": row.id,
        "model_version_id": row.model_version_id,
        "training_run_id": row.training_run_id,
        "dataset_id": row.dataset_id,
        "dataset_fingerprint": row.dataset_fingerprint,
        "status": row.status,
        "verdict": row.verdict,
        "summary": row.summary,
        "stage": row.current_stage,
        "progress": float(row.progress) if row.progress is not None else None,
        "checks": {
            "PASS": int(row.checks_passed) if row.checks_passed is not None else None,
            "WARNING": int(row.checks_warning) if row.checks_warning is not None else None,
            "FAIL": int(row.checks_failed) if row.checks_failed is not None else None,
            "BLOCKED": int(row.checks_blocked) if row.checks_blocked is not None else None,
        },
        "validation_engine_version": row.validation_engine_version,
        "config_fingerprint": row.config_fingerprint,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "error": row.error,
        "authority": (
            "advisory. A PASS makes a candidate eligible for CONSIDERATION by the model "
            "registry. It is not a promotion, not a deployment and not an instruction to "
            "trade."
        ),
    }


__all__ = [
    "ACTIVE",
    "CANCELLED",
    "CANCELLING",
    "COMPLETED",
    "FAILED",
    "QUEUED",
    "RUNNING",
    "DuplicateValidationJob",
    "ValidationBusy",
    "ValidationService",
    "Verdict",
    "summarise",
]
