"""The AI training engine (L25).

The ones that matter most, and they are the rules the brief calls critical:

  * `test_a_dataset_that_is_not_ready_cannot_be_trained_on` — §7 and §8.
  * `test_preprocessing_is_fitted_on_the_training_segment_only` — §10.
  * `test_training_produces_a_draft_and_promotes_nothing` — §25 and §26.
  * `test_a_dataset_that_moves_under_a_running_job_fails_it` — §6, checked
    rather than merely forbidden.
  * `test_the_training_engine_cannot_reach_a_venue_or_promote_a_model` — §36
    and §50, parsed.
"""

from __future__ import annotations

import ast
import asyncio
import math
import random
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.datasets.builder import Dataset, DatasetConfig, DatasetStatus, build
from app.datasets.labels import LabelConfig
from app.db.base import Base
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.models.ai import TRAINING_STATUSES, ModelVersion, TrainingRun
from app.training import (
    MAX_QUEUED_PER_USER,
    DuplicateTrainingJob,
    ModelFamily,
    SplitConfig,
    TrainingBusy,
    TrainingConfig,
    TrainingError,
    TrainingService,
    TrainingStage,
    class_weights,
    evaluate,
    fit_majority,
    fit_weighted_logistic,
    progress_of,
    summarise,
)
from app.training import metrics as training_metrics
from app.training.service import (
    CANCELLED,
    FAILED,
    VALIDATION_PENDING,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

START = datetime(2026, 1, 1, 0, 0)
NOW = datetime(2026, 3, 1, 12, 0)
SPREAD = Decimal("0.00002")


# ================================================================= fixtures


def make_bars(n: int = 600, *, seed: int = 11) -> list[Bar]:
    rng = random.Random(seed)
    price = 1.10
    out: list[Bar] = []
    for i in range(n):
        price = max(0.5, price * (1 + 0.00002 * math.sin(i / 19.0) + rng.gauss(0, 0.0008)))
        high = price * (1 + abs(rng.gauss(0, 0.0006)))
        low = price * (1 - abs(rng.gauss(0, 0.0006)))
        open_ = min(max(price * (1 + rng.gauss(0, 0.0004)), low), high)
        out.append(
            Bar(
                symbol="EURUSD",
                provider=Provider.mt5,
                timeframe=Timeframe.H1,
                bar_time=START + timedelta(hours=i),
                open=Decimal(f"{open_:.5f}"),
                high=Decimal(f"{high:.5f}"),
                low=Decimal(f"{low:.5f}"),
                close=Decimal(f"{price:.5f}"),
                volume=Decimal(rng.randint(100, 900)),
                spread=SPREAD,
                spread_availability=Availability.available,
            )
        )
    return out


def make_dataset(bars: list[Bar] | None = None) -> Dataset:
    return build(
        bars or make_bars(),
        DatasetConfig(
            key="eurusd-h1",
            symbol="EURUSD",
            provider_symbol="EURUSD.R",
            timeframe=Timeframe.H1,
            provider=Provider.mt5,
            label_config=LabelConfig(spread_points=SPREAD, horizon=12),
        ),
        now=NOW,
    )


def make_config(**over: object) -> TrainingConfig:
    fields: dict[str, object] = {
        "family": ModelFamily.trade_probability,
        "dataset_key": "eurusd-h1",
        "dataset_version": "1",
        "model_version": "1.0",
        "features": ("rsi_14", "atr_pct_14", "ema_spread_10_50", "return_5"),
        "iterations": 40,
        "minimum_rows": 100,
    }
    fields.update(over)
    return TrainingConfig(**fields)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return make_dataset()


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        from app.auth.models import User

        session.add(User(id="u1", email="a@b.io", password_hash="x", role="trader"))
        await session.commit()
    yield factory
    await engine.dispose()


def loader_for(dataset: Dataset):  # noqa: ANN201
    async def load(_db: AsyncSession) -> Dataset:
        return dataset

    return load


def gated_loader(dataset: Dataset, gate: asyncio.Event):  # noqa: ANN201
    """A loader that holds the job open until the test releases it.

    Deterministic, and it costs nothing: holding a job open with a very large
    iteration count would burn CPU on a worker thread and make every later test
    in the file depend on how fast this machine is.
    """

    async def load(_db: AsyncSession) -> Dataset:
        await gate.wait()
        return dataset

    return load


async def run_to_completion(
    service: TrainingService, sessions, config: TrainingConfig, loader
) -> TrainingRun:  # noqa: ANN001
    async with sessions() as db:
        row = await service.queue(db, config, user_id="u1", loader=loader)
    job_id = row.id
    # Await the TASK rather than polling the status column. A poll makes the
    # test depend on how fast this machine is, and the row says what the last
    # writer believed while the task says what actually happened.
    assert await service.wait_for(job_id), "the training task was not registered"
    await service.shutdown()
    async with sessions() as db:
        current = await db.get(TrainingRun, job_id)
    assert current is not None
    assert current.status in (VALIDATION_PENDING, FAILED, CANCELLED), current.status
    return current


# ============================================================ 1. the config


def test_a_split_with_no_holdout_is_refused() -> None:
    with pytest.raises(TrainingError) as exc:
        SplitConfig(train=0.9, validation=0.1)
    assert "leaves no test segment" in str(exc.value)


def test_the_split_is_configurable_and_not_hardcoded() -> None:
    split = SplitConfig(train=0.6, validation=0.25)
    assert split.test == pytest.approx(0.15)
    assert "chronological" in split.as_dict()["ordering"]


def test_there_is_no_shuffle_option_anywhere_in_the_config() -> None:
    """§8: temporal ordering is not a setting somebody can turn off."""
    assert "shuffle" not in TrainingConfig.__dataclass_fields__
    assert "shuffle" not in SplitConfig.__dataclass_fields__
    # The rendered dict DOES contain the word, in the sentence saying there is
    # no such option. A field name is the thing that would matter.
    assert not any("shuffle" in name for name in TrainingConfig.__dataclass_fields__)
    assert "no shuffle option" in make_config().split.as_dict()["ordering"]


def test_an_unknown_class_weight_is_refused_rather_than_ignored() -> None:
    with pytest.raises(TrainingError) as exc:
        make_config(class_weight="magic")
    assert "refused rather than ignored" in str(exc.value)


def test_a_dataset_cannot_be_named_implicitly() -> None:
    with pytest.raises(TrainingError):
        make_config(dataset_key="")


def test_the_same_recipe_fingerprints_the_same_and_a_different_one_does_not() -> None:
    assert make_config().fingerprint() == make_config().fingerprint()
    assert make_config().fingerprint() != make_config(random_seed=7).fingerprint()
    assert make_config().fingerprint() != make_config(iterations=41).fingerprint()


def test_progress_is_derived_from_the_stage() -> None:
    """Two numbers that can disagree is one number too many."""
    assert progress_of(TrainingStage.queued) == 0.0
    assert progress_of(TrainingStage.done) == 1.0
    assert progress_of(TrainingStage.fitting_candidate) > progress_of(TrainingStage.splitting)


def test_the_environment_does_not_claim_determinism_it_cannot_guarantee() -> None:
    from app.training import environment

    assert "not claimed" in environment()["determinism"]


# ====================================================== 2. the quality gates


def test_a_clean_dataset_passes_every_gate(dataset: Dataset) -> None:
    report = evaluate(dataset, make_config(), targets=[0, 1] * (len(dataset.rows) // 2))
    assert report.passed, [g.detail for g in report.failures()]


def test_a_dataset_that_is_not_ready_cannot_be_trained_on() -> None:
    """§7 and §8 in one gate: L23 marks READY only when leakage checks pass."""
    blocked = make_dataset(make_bars(60))
    assert blocked.status is not DatasetStatus.ready
    report = evaluate(blocked, make_config())
    assert not report.passed
    assert any(g.check == "dataset_is_ready" for g in report.failures())


def test_too_few_rows_refuses_the_run(dataset: Dataset) -> None:
    report = evaluate(dataset, make_config(minimum_rows=10_000))
    assert not report.passed
    assert any("below the configured minimum" in g.detail for g in report.failures())


def test_a_missing_feature_refuses_the_run(dataset: Dataset) -> None:
    report = evaluate(dataset, make_config(features=("moon_phase_14",)))
    assert not report.passed
    assert any("moon_phase_14" in g.detail for g in report.failures())


def test_one_class_absent_refuses_the_run(dataset: Dataset) -> None:
    report = evaluate(dataset, make_config(), targets=[1] * len(dataset.rows))
    assert not report.passed
    assert any(g.check == "both_classes_present" for g in report.failures())


def test_class_imbalance_is_a_warning_and_not_a_block(dataset: Dataset) -> None:
    """§15: document the distribution first. It does not stop the run."""
    targets = [1] * (len(dataset.rows) - 3) + [0, 0, 0]
    report = evaluate(dataset, make_config(), targets=targets)
    assert report.passed
    assert any(g.check == "class_balance" for g in report.warnings())


def test_the_gate_report_says_it_cleans_nothing(dataset: Dataset) -> None:
    assert "has no write path" in evaluate(dataset, make_config()).as_dict()["consequence"]


# ========================================================= 3. the baseline


def test_the_baseline_is_the_training_prior_not_a_coin_flip() -> None:
    baseline = fit_majority([1, 1, 1, 0])
    assert baseline.prior == pytest.approx(0.75)
    assert "Stronger than a coin flip" in baseline.as_dict()["why"]


def test_a_baseline_cannot_be_fitted_on_nothing() -> None:
    with pytest.raises(TrainingError):
        fit_majority([])


def test_class_weights_are_one_when_weighting_is_off() -> None:
    assert class_weights([1, 0, 1], "none") == (1.0, 1.0)


def test_balanced_weights_make_the_classes_contribute_equally() -> None:
    negative, positive = class_weights([1] * 90 + [0] * 10, "balanced")
    assert positive < 1.0 < negative
    assert 90 * positive == pytest.approx(10 * negative)


def test_balanced_weighting_needs_both_classes() -> None:
    with pytest.raises(TrainingError) as exc:
        class_weights([1] * 50, "balanced")
    assert "division by zero dressed as a hyperparameter" in str(exc.value)


# ========================================================== 4. the fit


def _xy(dataset: Dataset, features: tuple[str, ...]) -> tuple[list[list[float]], list[int]]:
    x: list[list[float]] = []
    y: list[int] = []
    for row in dataset.rows:
        values = [row.features.get(f) for f in features]
        if any(v is None for v in values):
            continue
        x.append([float(v) for v in values if v is not None])
        y.append(1 if row.labels["bracket_outcome"] == "WIN" else 0)
    return x, y


def test_the_fit_is_deterministic(dataset: Dataset) -> None:
    features = ("rsi_14", "atr_pct_14")
    x, y = _xy(dataset, features)
    config = make_config(iterations=30)
    first, _ = fit_weighted_logistic(x, y, features=features, config=config)
    second, _ = fit_weighted_logistic(x, y, features=features, config=config)
    assert first.weights == second.weights
    assert first.bias == second.bias


def test_unweighted_training_agrees_with_the_level_24_fitter(dataset: Dataset) -> None:
    """The L25 fitter is a widened form of L24's, not a second implementation."""
    from app.ai import fit_logistic

    features = ("rsi_14", "atr_pct_14")
    x, y = _xy(dataset, features)
    rows: list[dict[str, float | None]] = [dict(zip(features, values, strict=True)) for values in x]
    theirs = fit_logistic(rows, y, features=features, iterations=25, learning_rate=0.1, l2=0.01)
    mine, _ = fit_weighted_logistic(
        x, y, features=features, config=make_config(iterations=25, class_weight="none")
    )
    for a, b in zip(theirs.weights, mine.weights, strict=True):
        assert a == pytest.approx(b, abs=1e-12)
    assert theirs.bias == pytest.approx(mine.bias, abs=1e-12)


def test_early_stopping_reads_validation_and_keeps_the_best_parameters(
    dataset: Dataset,
) -> None:
    """§16: never the test set, and the best epoch is the one kept."""
    features = ("rsi_14", "atr_pct_14")
    x, y = _xy(dataset, features)
    cut = int(len(x) * 0.7)
    _, report = fit_weighted_logistic(
        x[:cut],
        y[:cut],
        features=features,
        config=make_config(iterations=200, early_stopping_patience=3),
        validation_rows=x[cut:],
        validation_targets=y[cut:],
    )
    assert report.best_iteration is not None
    assert "VALIDATION" in report.as_dict()["early_stopping"]


def test_a_fit_stops_when_asked_to(dataset: Dataset) -> None:
    features = ("rsi_14", "atr_pct_14")
    x, y = _xy(dataset, features)
    _, report = fit_weighted_logistic(
        x, y, features=features, config=make_config(iterations=500), should_stop=lambda: True
    )
    assert report.stopped_because == "cancelled"
    assert report.iterations_run == 0


def test_a_model_with_no_features_cannot_be_fitted() -> None:
    with pytest.raises(TrainingError):
        fit_weighted_logistic([], [], features=(), config=make_config())


# =========================================================== 5. the metrics


def test_accuracy_is_never_reported_without_the_majority_share() -> None:
    """A set that is 70% WIN gives 70% accuracy to a constant predictor."""
    report = training_metrics.classification([0.9] * 70 + [0.9] * 30, [1] * 70 + [0] * 30)
    assert report["accuracy"] == pytest.approx(0.7)
    assert report["majority_share"] == pytest.approx(0.7)
    assert report["beats_majority"] is False


def test_ml_metrics_say_they_are_not_trading_performance() -> None:
    report = training_metrics.classification([0.6, 0.4], [1, 0])
    assert "not trading performance" in report["note"]


def test_economic_metrics_say_they_are_not_a_backtest() -> None:
    report = training_metrics.economic([0.01, -0.005, 0.002])
    assert report["trades"] == 3
    assert "NOT a backtest" in report["note"]
    assert report["max_drawdown"] <= 0


def test_the_two_kinds_of_metric_are_never_merged() -> None:
    ml = training_metrics.classification([0.6, 0.4], [1, 0])
    economic = training_metrics.economic([0.01, -0.005])
    assert set(ml) & set(economic) <= {"note", "warning"}


def test_a_small_sample_carries_a_warning() -> None:
    assert training_metrics.classification([0.6] * 5, [1] * 5)["warning"]


def test_comparison_needs_more_than_one_metric_to_move() -> None:
    """§24: one metric moving is not a result."""
    candidate = {"accuracy": 0.6, "log_loss": 0.9, "beats_majority": True}
    baseline = {"accuracy": 0.5, "log_loss": 0.8}
    assert training_metrics.compare(candidate, baseline)["meaningfully_better"] is False

    better = {"accuracy": 0.6, "log_loss": 0.7, "beats_majority": True}
    assert training_metrics.compare(better, baseline)["meaningfully_better"] is True


def test_the_comparison_says_it_is_not_the_validation_gate() -> None:
    result = training_metrics.compare({"accuracy": 0.6}, {"accuracy": 0.5})
    assert "not the L26 gate" in result["caveat"]
    assert "permutation null" in result["caveat"]


def test_calibration_reports_a_measurement_and_not_a_verdict() -> None:
    report = training_metrics.calibration([0.1, 0.9, 0.5, 0.8], [0, 1, 1, 1])
    assert "is_calibrated" not in report
    assert "does not certify one" in report["note"]


# ====================================================== 6. the job lifecycle


async def test_training_produces_a_draft_and_promotes_nothing(sessions, dataset: Dataset) -> None:
    """§25 and §26: a successful run means a candidate exists, nothing more."""
    service = TrainingService(sessions)
    row = await run_to_completion(service, sessions, make_config(), loader_for(dataset))

    assert row.status == VALIDATION_PENDING
    assert row.model_version_id is not None
    assert row.progress == 1
    assert row.current_stage == str(TrainingStage.done)

    async with sessions() as db:
        version = await db.get(ModelVersion, row.model_version_id)
        assert version is not None
        # DRAFT. Never promoted, and the database would refuse a promotion that
        # could not state its provenance anyway.
        assert version.status == "draft"
        assert version.feature_version
        assert version.params["artifact"]["kind"] == "logistic"
        assert version.training_start is not None and version.test_end is not None

    assert "not mean the model is approved" in summarise(row)["candidate_only"]


async def test_the_run_records_both_the_baseline_and_the_candidate(
    sessions, dataset: Dataset
) -> None:
    """§13 and §24: a candidate is always reported beside a baseline."""
    service = TrainingService(sessions)
    row = await run_to_completion(service, sessions, make_config(), loader_for(dataset))
    assert row.metrics is not None
    assert row.metrics["baseline"]["model"]["kind"] == "majority_class_prior"
    assert "meaningfully_better" in row.metrics["comparison"]
    assert row.metrics["classification"]["samples"] > 0


async def test_ml_and_economic_metrics_are_stored_separately(sessions, dataset: Dataset) -> None:
    service = TrainingService(sessions)
    row = await run_to_completion(service, sessions, make_config(), loader_for(dataset))
    assert row.metrics is not None
    assert "classification" in row.metrics
    assert "economic" in row.metrics
    assert "never merged" in row.metrics["separation"]


async def test_a_refused_gate_fails_the_job_rather_than_training_anyway(
    sessions,
) -> None:
    service = TrainingService(sessions)
    row = await run_to_completion(
        service, sessions, make_config(), loader_for(make_dataset(make_bars(60)))
    )
    assert row.status == FAILED
    assert "data quality gates refused" in (row.error or "")
    assert row.model_version_id is None


async def test_a_dataset_that_moves_under_a_running_job_fails_it(
    sessions, dataset: Dataset
) -> None:
    """§6, checked rather than merely forbidden.

    The job locks the dataset's fingerprint before fitting and re-derives it
    afterwards. A loader that returns different data the second time is what a
    dataset changing mid-run looks like.
    """
    service = TrainingService(sessions)
    calls = {"n": 0}
    other = make_dataset(make_bars(600, seed=99))

    async def shifting(_db: AsyncSession) -> Dataset:
        calls["n"] += 1
        return dataset if calls["n"] == 1 else other

    row = await run_to_completion(service, sessions, make_config(), shifting)
    assert row.status == FAILED
    assert "changed while the job ran" in (row.error or "")
    assert row.model_version_id is None


async def test_a_second_identical_job_is_refused_while_the_first_runs(
    sessions, dataset: Dataset
) -> None:
    """§38: two identical fits produce two identical candidates."""
    service = TrainingService(sessions)
    config = make_config()
    gate = asyncio.Event()
    async with sessions() as db:
        await service.queue(db, config, user_id="u1", loader=gated_loader(dataset, gate))
    try:
        async with sessions() as db:
            with pytest.raises(DuplicateTrainingJob) as exc:
                await service.queue(db, config, user_id="u1", loader=gated_loader(dataset, gate))
        assert "already running the same model" in str(exc.value)
    finally:
        gate.set()
        await service.shutdown()


async def test_a_user_cannot_queue_without_limit(sessions, dataset: Dataset) -> None:
    """§19: training is bounded so one caller cannot exhaust the machine."""
    service = TrainingService(sessions)
    gate = asyncio.Event()
    try:
        for i in range(MAX_QUEUED_PER_USER):
            async with sessions() as db:
                await service.queue(
                    db,
                    make_config(model_version=f"1.{i}"),
                    user_id="u1",
                    loader=gated_loader(dataset, gate),
                )
        async with sessions() as db:
            with pytest.raises(TrainingBusy) as exc:
                await service.queue(
                    db,
                    make_config(model_version="9.9"),
                    user_id="u1",
                    loader=gated_loader(dataset, gate),
                )
        assert "already queued or running" in str(exc.value)
    finally:
        gate.set()
        await service.shutdown()


async def test_a_cancelled_job_is_never_recorded_as_trained(sessions, dataset: Dataset) -> None:
    """§18: do not mark a cancelled job as successfully trained."""
    service = TrainingService(sessions)
    gate = asyncio.Event()
    async with sessions() as db:
        row = await service.queue(
            db, make_config(), user_id="u1", loader=gated_loader(dataset, gate)
        )
    await asyncio.sleep(0.02)
    assert await service.cancel(row.id) is True
    gate.set()
    assert await service.wait_for(row.id)
    await service.shutdown()

    async with sessions() as db:
        current = await db.get(TrainingRun, row.id)
    assert current is not None
    assert current.status == CANCELLED
    assert current.model_version_id is None


async def test_cancelling_a_job_that_is_not_running_says_so(sessions) -> None:
    assert await TrainingService(sessions).cancel("no-such-job") is False


async def test_a_queued_job_can_be_recorded_before_it_has_a_model(
    sessions, dataset: Dataset
) -> None:
    """The column had to become nullable: a queued job has no candidate yet."""
    service = TrainingService(sessions)
    gate = asyncio.Event()
    async with sessions() as db:
        row = await service.queue(
            db, make_config(), user_id="u1", loader=gated_loader(dataset, gate)
        )
    assert row.model_version_id is None
    assert row.status in ("queued", "running")
    gate.set()
    await service.shutdown()


def test_the_status_vocabulary_declares_only_reachable_states() -> None:
    """`paused`, `validation_passed` and `validation_failed` are absent."""
    assert "paused" not in TRAINING_STATUSES
    assert "validation_passed" not in TRAINING_STATUSES
    assert "validation_failed" not in TRAINING_STATUSES
    assert "validation_pending" in TRAINING_STATUSES
    assert "cancelling" in TRAINING_STATUSES


# =============================================== 7. preprocessing and leakage


async def test_preprocessing_is_fitted_on_the_training_segment_only(
    sessions, dataset: Dataset
) -> None:
    """§10, checked on the stored artifact rather than trusted."""
    service = TrainingService(sessions)
    row = await run_to_completion(service, sessions, make_config(), loader_for(dataset))
    async with sessions() as db:
        version = await db.get(ModelVersion, row.model_version_id)
        assert version is not None
    scaler = version.params["scaler"]
    assert scaler["fitted_on"] == "train"
    assert dataset.split is not None
    assert scaler["fitted_rows"] == list(dataset.split.train)
    assert "never contribute" in scaler["guarantee"]


async def test_the_stored_artifact_carries_everything_needed_to_reproduce_it(
    sessions, dataset: Dataset
) -> None:
    service = TrainingService(sessions)
    row = await run_to_completion(service, sessions, make_config(), loader_for(dataset))
    async with sessions() as db:
        version = await db.get(ModelVersion, row.model_version_id)
        assert version is not None
    for key in ("artifact", "training_config", "environment", "scaler", "feature_schema"):
        assert key in version.params, key
    assert version.dataset_fingerprint == dataset.fingerprint
    assert version.params["environment"]["frameworks"].startswith("none")


async def test_the_job_locks_the_dataset_fingerprint(sessions, dataset: Dataset) -> None:
    service = TrainingService(sessions)
    row = await run_to_completion(service, sessions, make_config(), loader_for(dataset))
    assert row.dataset_fingerprint == dataset.fingerprint


# ================================================ 8. what training may not do


def test_the_training_engine_cannot_reach_a_venue_or_promote_a_model() -> None:
    """§36 and §50, parsed rather than asserted by convention."""
    forbidden = (
        "app.brokers",
        "app.oms",
        "app.risk",
        "app.sizing",
        "app.positions",
        "app.execution",
        "app.strategies",
        "MetaTrader5",
    )
    package = Path(__file__).resolve().parents[1] / "app" / "training"
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for banned in forbidden:
                    assert not name.startswith(banned), f"{path.name} imports {name}"


def test_nothing_in_the_package_writes_a_promoted_model() -> None:
    """§26: promotion is level 28's, and this package has no spelling for it."""
    package = Path(__file__).resolve().parents[1] / "app" / "training"
    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert '"promoted"' not in source, path.name
        assert "'promoted'" not in source, path.name


def test_live_trading_is_still_disabled_by_default() -> None:
    """§50. Training enables nothing."""
    from app.core.settings import LIVE_GATES, Settings, TradingMode

    settings = Settings(_env_file=None)
    assert settings.live_trading is False
    assert settings.trading_mode is TradingMode.paper
    assert all(value is False for value in LIVE_GATES.values())


async def test_a_failed_job_leaves_no_candidate_behind(sessions) -> None:
    """§27 and §37: a failure must never corrupt or replace anything."""
    service = TrainingService(sessions)

    async def explode(_db: AsyncSession) -> Dataset:
        raise RuntimeError("the dataset store is down")

    row = await run_to_completion(service, sessions, make_config(), explode)
    assert row.status == FAILED
    assert "dataset store is down" in (row.error or "")
    async with sessions() as db:
        assert list((await db.scalars(select(ModelVersion))).all()) == []
