"""The AI validation engine (L26).

The ones that matter most, and they are the rules the brief calls critical:

  * `test_a_blocked_check_is_not_a_failing_one` — §26, the distinction most
    easily lost.
  * `test_the_verdict_is_not_a_score` — §27, checked by construction rather
    than by reading the code.
  * `test_a_passing_report_promotes_nothing` — §34, the whole point of the
    level.
  * `test_validation_cannot_reach_a_venue_or_promote_a_model` — §30 and §50,
    parsed with `ast`.
  * `test_nothing_is_measured_on_data_the_model_was_fitted_on` — §12.
  * `test_the_economic_figures_come_from_the_projects_own_engine` — §18.
"""

from __future__ import annotations

import ast
import asyncio
import math
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.ai.loader import ArtifactError, model_from_version
from app.ai.probability import LogisticCoefficients, TradeProbabilityModel
from app.datasets.builder import Dataset, DatasetConfig, DatasetStatus, build
from app.datasets.labels import LabelConfig
from app.db.base import Base
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.models.ai import ModelVersion, TrainingRun
from app.models.validation import VALIDATION_STATUSES, VALIDATION_VERDICTS, ValidationRun
from app.training.config import ModelFamily, TrainingConfig
from app.training.service import TrainingService
from app.validation import checks, economics, statistics
from app.validation.config import (
    VALIDATION_ENGINE_VERSION,
    Severity,
    Thresholds,
    ValidationConfig,
    ValidationError,
    ValidationStage,
    Verdict,
    progress_of,
    verdict_from,
)
from app.validation.report import MEANS, ValidationReport, markdown
from app.validation.service import (
    CANCELLED,
    COMPLETED,
    DuplicateValidationJob,
    ValidationService,
    summarise,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

START = datetime(2026, 1, 1, 0, 0)
NOW = datetime(2026, 6, 1, 12, 0)
SPREAD = Decimal("0.00002")

LOOSE = Thresholds(minimum_samples=50, minimum_trades=5, permutations=50)


# ================================================================= fixtures


def make_bars(n: int = 1200, *, seed: int = 11) -> list[Bar]:
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


def make_dataset(bars: list[Bar]) -> Dataset:
    return build(
        bars,
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


@pytest.fixture(scope="module")
def bars() -> list[Bar]:
    return make_bars()


@pytest.fixture(scope="module")
def dataset(bars: list[Bar]) -> Dataset:
    return make_dataset(bars)


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
        from app.models.ai import AIModel

        session.add(User(id="u1", email="a@b.io", password_hash="x", role="trader"))
        # Every ModelVersion below is a version OF this model; `model_id` is a
        # foreign key, so the parent has to exist.
        session.add(
            AIModel(id="m1", key="trade_probability", name="trade probability", kind="classifier")
        )
        await session.commit()
    yield factory
    await engine.dispose()


def loader_for(bars: list[Bar], dataset: Dataset):  # noqa: ANN201
    async def load(_db: AsyncSession) -> tuple[list[Bar], Dataset]:
        return bars, dataset

    return load


def gated_loader(bars: list[Bar], dataset: Dataset, gate: asyncio.Event):  # noqa: ANN201
    """Holds the job open until the test releases it. Deterministic."""

    async def load(_db: AsyncSession) -> tuple[list[Bar], Dataset]:
        await gate.wait()
        return bars, dataset

    return load


async def train_a_candidate(sessions, dataset: Dataset) -> str:  # noqa: ANN001
    """One real training run, so validation is exercised on a real artifact."""

    async def load(_db: AsyncSession) -> Dataset:
        return dataset

    service = TrainingService(sessions)
    config = TrainingConfig(
        family=ModelFamily.trade_probability,
        dataset_key="eurusd-h1",
        dataset_version="1",
        model_version="1.0",
        features=("rsi_14", "atr_pct_14", "ema_spread_10_50", "return_5"),
        iterations=40,
        minimum_rows=100,
    )
    async with sessions() as db:
        row = await service.queue(db, config, user_id="u1", loader=load)
    assert await service.wait_for(row.id)
    await service.shutdown()
    async with sessions() as db:
        job = await db.get(TrainingRun, row.id)
    assert job is not None and job.model_version_id, job.error
    return job.model_version_id


async def validate(  # noqa: ANN201
    sessions,
    bars: list[Bar],
    dataset: Dataset,
    version_id: str,
    *,
    thresholds: Thresholds | None = None,
    **over: object,
):
    service = ValidationService(sessions)
    config = ValidationConfig(
        model_version_id=version_id,
        thresholds=thresholds or LOOSE,
        **over,  # type: ignore[arg-type]
    )
    async with sessions() as db:
        row = await service.queue(db, config, loader=loader_for(bars, dataset), user_id="u1")
    assert await service.wait_for(row.id)
    await service.shutdown()
    async with sessions() as db:
        done = await db.get(ValidationRun, row.id)
    assert done is not None
    return done


# ============================================================ 1. the config


def test_every_threshold_is_configurable_and_none_is_hardcoded() -> None:
    """Section 25. A bar nobody chose is a bar nobody can argue with."""
    default = Thresholds()
    strict = Thresholds(minimum_profit_factor=1.4, minimum_auc=0.55, permutations=500)
    assert strict.minimum_profit_factor != default.minimum_profit_factor
    assert strict.minimum_auc != default.minimum_auc
    # And the report quotes what it used, not what the defaults are.
    assert strict.as_dict()["minimum_profit_factor"] == 1.4


def test_a_run_must_name_its_candidate() -> None:
    with pytest.raises(ValidationError) as exc:
        ValidationConfig(model_version_id="")
    assert "no 'latest model' option" in str(exc.value)


def test_a_decision_threshold_outside_zero_to_one_is_refused() -> None:
    for bad in (0.0, 1.0, -0.2, 1.5):
        with pytest.raises(ValidationError):
            ValidationConfig(model_version_id="v1", decision_threshold=bad)


def test_the_fingerprint_covers_the_thresholds() -> None:
    """Two runs at different bars are different runs, not a repeat."""
    a = ValidationConfig(model_version_id="v1", thresholds=Thresholds())
    b = ValidationConfig(model_version_id="v1", thresholds=Thresholds(minimum_profit_factor=1.4))
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == ValidationConfig(model_version_id="v1").fingerprint()


def test_every_stage_has_a_progress_and_they_increase() -> None:
    values = [progress_of(stage) for stage in ValidationStage]
    assert values == sorted(values)
    assert values[0] == 0.0
    assert values[-1] == 1.0


# ========================================================== 2. the verdict


def test_a_blocked_check_is_not_a_failing_one() -> None:
    """Section 26. FAIL says not good enough; BLOCKED says we could not tell."""
    assert verdict_from([Severity.blocked]) is Verdict.blocked
    assert verdict_from([Severity.failed]) is Verdict.failed
    # And BLOCKED outranks FAIL: a report containing an unevaluable check
    # cannot honestly say the candidate failed.
    assert verdict_from([Severity.failed, Severity.blocked]) is Verdict.blocked


def test_one_failure_cannot_be_averaged_away() -> None:
    many_passes = [Severity.passed] * 20 + [Severity.failed]
    assert verdict_from(many_passes) is Verdict.failed


def test_a_warning_alone_is_conditional() -> None:
    assert verdict_from([Severity.passed, Severity.warning]) is Verdict.conditional
    assert verdict_from([Severity.passed]) is Verdict.passed


def test_no_checks_at_all_is_blocked_not_passed() -> None:
    assert verdict_from([]) is Verdict.blocked


def test_the_verdict_is_not_a_score() -> None:
    """Section 27, checked by construction rather than by reading the code."""
    report = ValidationReport(config=ValidationConfig(model_version_id="v1"))
    for name in ("a", "b", "c"):
        report.add(checks.Finding(name, Severity.passed, "fine"))
    report.add(checks.Finding("economic", Severity.failed, "loses money"))
    payload = report.as_dict()
    assert payload["verdict"] == "FAIL"
    assert "score" not in payload
    assert not any(key.endswith("_score") for key in payload)
    assert "there is no score" in payload["method"]["scoring"]


def test_every_verdict_says_what_it_does_not_authorise() -> None:
    """Section 34. The words are in the payload, not only in a docstring."""
    assert set(MEANS) == set(Verdict)
    passed = MEANS[Verdict.passed]
    assert "does NOT mean" in passed
    assert "deploy to live trading" in passed
    assert "CONSIDERED" in passed
    assert "not FAIL" in MEANS[Verdict.blocked]


# =========================================================== 3. the checks


def test_a_dataset_that_is_not_ready_blocks_rather_than_fails(bars: list[Bar]) -> None:
    """Section 7. Validation on refused data describes the data, not the model."""
    thin = make_dataset(bars[:60])
    assert thin.status is not DatasetStatus.ready
    finding = checks.data_integrity(thin, LOOSE)
    assert finding.severity is Severity.blocked


def test_a_ready_dataset_reports_l23s_own_verdict(dataset: Dataset) -> None:
    finding = checks.data_integrity(dataset, LOOSE)
    assert finding.severity is Severity.passed
    assert finding.evidence["rows"] == len(dataset.rows)


def test_too_few_samples_blocks_rather_than_failing() -> None:
    """Section 24. A figure from 30 rows is unmeasured, not weak."""
    finding = checks.sample_size(30, 40, Thresholds())
    assert finding.severity is Severity.blocked
    assert "confidence interval wider" in finding.summary


def test_enough_rows_but_too_few_trades_warns_and_says_which_half_holds() -> None:
    finding = checks.sample_size(500, 4, Thresholds())
    assert finding.severity is Severity.warning
    assert "ML metrics are supported" in finding.summary


def test_a_missing_leakage_report_is_not_a_passing_one(dataset: Dataset) -> None:
    """Section 9. A check nobody ran is not a check that passed."""
    from app.datasets.leakage import LeakageReport

    empty = Dataset(
        config=dataset.config,
        rows=dataset.rows,
        split=dataset.split,
        folds=dataset.folds,
        scaler=dataset.scaler,
        quality=dataset.quality,
        leakage=LeakageReport(findings=[]),
        status=dataset.status,
        fingerprint=dataset.fingerprint,
    )
    assert checks.leakage(empty).severity is Severity.blocked


def test_the_leakage_check_reads_all_six_of_l23s(dataset: Dataset) -> None:
    finding = checks.leakage(dataset)
    assert finding.severity is Severity.passed
    assert len(finding.evidence["checks"]) == 6


def test_the_temporal_check_proves_the_holdout_comes_last(dataset: Dataset) -> None:
    """Sections 10 and 12."""
    finding = checks.temporal(dataset)
    assert finding.severity is Severity.passed
    assert finding.evidence["train"] > 0
    assert finding.evidence["test"] > 0


def test_a_candidate_with_no_baseline_blocks(dataset: Dataset) -> None:
    """Section 13. An unmeasured claim is not a passing one."""
    finding = checks.baseline_comparison({"log_loss": 0.5}, {}, LOOSE)
    assert finding.severity is Severity.blocked


def test_a_candidate_that_cannot_beat_its_own_prior_fails() -> None:
    finding = checks.baseline_comparison(
        {"log_loss": 0.70, "beats_majority": False, "accuracy": 0.5, "majority_share": 0.6},
        {"log_loss": 0.69},
        LOOSE,
    )
    assert finding.severity is Severity.failed
    assert "has not learned anything" in finding.summary


def test_poor_calibration_is_a_warning_and_says_how_to_read_the_number() -> None:
    """Section 16. Ranking can still be useful; the probability is not a frequency."""
    probabilities = [0.95] * 300
    outcomes = [0] * 300
    finding = checks.calibration(probabilities, outcomes, LOOSE)
    assert finding.severity is Severity.warning
    assert "0.80 does not mean" in finding.summary


def test_calibration_on_too_few_predictions_blocks() -> None:
    finding = checks.calibration([0.5] * 10, [1] * 10, Thresholds())
    assert finding.severity is Severity.blocked


def test_an_economic_result_with_no_trades_blocks_rather_than_fails() -> None:
    """Section 17. Zero trades is not a losing model; nothing was measured."""
    finding = checks.economic({"trades": 0, "entries_offered": 0}, LOOSE)
    assert finding.severity is Severity.blocked


def test_a_profit_factor_below_the_bar_fails_and_names_the_spread() -> None:
    finding = checks.economic(
        {"trades": 40, "profit_factor": 0.8, "rows": [{"net": -1.0}] * 40}, Thresholds()
    )
    assert finding.severity is Severity.failed
    assert "spread this broker actually charges" in finding.summary


def test_a_model_that_does_not_beat_its_shuffled_self_fails() -> None:
    """Section 23, and the gate this project actually trusts."""
    scores = [i / 100 for i in range(100)]
    outcomes = [i % 2 for i in range(100)]  # no relationship at all
    result = statistics.permutation_test(scores, outcomes, statistics.roc_auc, permutations=50)
    finding = checks.significance(result, LOOSE)
    assert finding.severity is Severity.failed
    assert "shuffled self" in finding.summary


def test_a_missing_null_is_not_a_passing_one() -> None:
    assert checks.significance(None, LOOSE).severity is Severity.blocked


def test_significance_that_clears_alpha_but_not_bonferroni_warns() -> None:
    """A model picked from twenty runs and quoted uncorrected was selected."""
    result = statistics.PermutationResult(
        observed=0.7,
        null_mean=0.5,
        null_std=0.05,
        null_best=0.62,
        permutations=200,
        p_value=0.02,
        alpha=0.05,
        corrected_alpha=0.0025,
        candidates_tried=20,
        beats_null=True,
        beats_corrected=False,
    )
    finding = checks.significance(result, LOOSE)
    assert finding.severity is Severity.warning
    assert "selected, not" in finding.summary


def test_a_result_that_lives_in_one_window_is_an_era_not_an_edge() -> None:
    """Section 11, and the project's own donchian_fade_55 lesson."""
    windows = [
        {"window": 1, "metric": 0.40},
        {"window": 2, "metric": 0.42},
        {"window": 3, "metric": 0.44},
        {"window": 4, "metric": 0.95},
    ]
    finding = checks.walk_forward(windows, LOOSE)
    assert finding.severity is Severity.warning
    assert "an era, not an edge" in finding.summary
    assert "1 fold of 4" in finding.evidence["why"]


def test_too_few_windows_blocks_rather_than_passing() -> None:
    assert checks.walk_forward([{"metric": 0.9}], Thresholds()).severity is Severity.blocked


def test_a_model_fitted_to_its_threshold_is_flagged() -> None:
    """Section 22."""
    probes = [
        {"probe": "baseline", "stable": True},
        {"probe": "threshold-", "stable": False},
        {"probe": "threshold+", "stable": False},
        {"probe": "cost", "stable": False},
    ]
    finding = checks.robustness(probes, LOOSE)
    assert finding.severity is Severity.warning
    assert "fitted to the threshold" in finding.summary


def test_a_regime_with_too_few_trades_gets_a_count_and_no_verdict() -> None:
    """Section 21. Printing a win rate beside a count of nine invites the claim."""
    breakdown = [
        {"regime": "low_volatility", "trades": 3, "profit_factor": 0.2},
        {"regime": "high_volatility", "trades": 4, "profit_factor": 0.1},
    ]
    finding = checks.regime(breakdown, Thresholds())
    assert finding.severity is Severity.blocked
    assert "low_volatility" in finding.evidence["too_thin"]


def test_no_regime_model_is_not_the_candidates_fault() -> None:
    finding = checks.regime([], LOOSE)
    assert finding.severity is Severity.blocked
    assert "Not a failure of the candidate" in finding.summary


def test_a_model_evaluated_on_the_wrong_feature_set_fails() -> None:
    model = TradeProbabilityModel(
        feature_version="9.9",
        coefficients=LogisticCoefficients(
            features=("rsi_14",), weights=(0.4,), bias=0.0, fitted_rows=100
        ),
    )
    dataset = None  # not reached: the version check comes first

    class _Stub:
        rows: list = []

    finding = checks.model_artifact(model, _Stub(), "1.0")  # type: ignore[arg-type]
    assert finding.severity is Severity.failed
    assert "different quantity" in finding.summary
    assert dataset is None


def test_an_unfitted_model_blocks_rather_than_failing() -> None:
    model = TradeProbabilityModel(feature_version="1.0")

    class _Stub:
        rows: list = []

    finding = checks.model_artifact(model, _Stub(), "1.0")  # type: ignore[arg-type]
    assert finding.severity is Severity.blocked
    assert "nothing to validate" in finding.summary


def test_a_model_fitted_on_different_data_blocks(dataset: Dataset) -> None:
    """Section 6. The comparison would be meaningless, so it is not made."""
    model = TradeProbabilityModel(
        feature_version="1.0",
        dataset_version="1",
        dataset_fingerprint="deadbeef" * 8,
        coefficients=LogisticCoefficients(
            features=("rsi_14",), weights=(0.4,), bias=0.0, fitted_rows=100
        ),
    )
    finding = checks.version_locking(model, dataset)
    assert finding.severity is Severity.blocked
    assert "different data" in finding.summary


# ======================================================= 4. the statistics


def test_auc_is_a_rank_sum_with_ties_averaged() -> None:
    assert statistics.roc_auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0
    assert statistics.roc_auc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0
    # Every score identical: no ranking information at all.
    assert statistics.roc_auc([0.5] * 4, [1, 1, 0, 0]) == 0.5


def test_auc_of_one_class_is_undefined_and_reported_as_chance() -> None:
    assert statistics.roc_auc([0.9, 0.1], [1, 1]) == 0.5


def test_a_p_value_of_exactly_zero_is_never_claimed() -> None:
    """Phipson-Smyth. 200 shuffles cannot support p = 0."""
    scores = list(range(100))
    outcomes = [1 if i >= 50 else 0 for i in range(100)]
    result = statistics.permutation_test(
        [float(s) for s in scores], outcomes, statistics.roc_auc, permutations=50
    )
    assert result.p_value > 0
    assert result.p_value == pytest.approx(1 / 51)


def test_the_permutation_test_is_deterministic() -> None:
    scores = [i / 100 for i in range(100)]
    outcomes = [1 if i % 3 == 0 else 0 for i in range(100)]
    a = statistics.permutation_test(scores, outcomes, statistics.roc_auc, permutations=50)
    b = statistics.permutation_test(scores, outcomes, statistics.roc_auc, permutations=50)
    assert a.p_value == b.p_value


def test_too_few_permutations_are_refused_rather_than_run() -> None:
    with pytest.raises(ValueError) as exc:
        statistics.permutation_test([0.5] * 30, [1] * 30, statistics.roc_auc, permutations=5)
    assert "cannot resolve a p-value" in str(exc.value)


def test_bonferroni_is_over_the_candidates_actually_tried() -> None:
    scores = [i / 100 for i in range(100)]
    outcomes = [i % 2 for i in range(100)]
    one = statistics.permutation_test(
        scores, outcomes, statistics.roc_auc, permutations=50, candidates_tried=1
    )
    twenty = statistics.permutation_test(
        scores, outcomes, statistics.roc_auc, permutations=50, candidates_tried=20
    )
    assert twenty.corrected_alpha == pytest.approx(one.corrected_alpha / 20)


def test_the_clustering_is_the_toolkits_own() -> None:
    """Not a second copy: the correction every recorded figure was measured under."""
    rows = [
        {
            "symbol": "EURUSD" if i % 2 else "GBPUSD",
            "net": 1.0,
            "entry_time": 1700000000 + i * 86400,
        }
        for i in range(20)
    ]
    out = statistics.clustered(rows)
    assert "t_stat_clustered_by_date" in out
    assert "t_stat_clustered_by_symbol" in out
    assert "USD on one side" in out["why"]


def test_the_bootstrap_says_when_its_interval_includes_zero() -> None:
    out = statistics.bootstrap_interval([1.0, -1.0] * 30)
    assert out["includes_zero"] is True
    assert "optimistic" in out["note"]


def test_the_pooled_t_never_stands_alone() -> None:
    out = statistics.pooled_t([1.0, 2.0, 3.0, 4.0])
    assert "date-clustered figure is the one to quote" in out["note"]


# ======================================================== 5. the economics


def test_the_economic_figures_come_from_the_projects_own_engine(bars: list[Bar]) -> None:
    """Section 18. One simulator, so results stay comparable with CLAUDE.md."""
    probabilities = {i: 0.9 for i in range(30, len(bars) - 30, 20)}
    summary = economics.evaluate(
        bars, probabilities, threshold=0.5, spread=float(SPREAD), symbol="EURUSD"
    )
    assert summary["trades"] > 0
    assert "rule_backtest.simulate" in summary["engine"]
    assert "NEXT bar's open" in summary["friction"]
    assert summary["spread_charged"] == float(SPREAD)


def test_a_prediction_below_the_threshold_is_not_a_trade(bars: list[Bar]) -> None:
    probabilities = {i: 0.30 for i in range(30, 200)}
    summary = economics.evaluate(
        bars, probabilities, threshold=0.60, spread=float(SPREAD), symbol="EURUSD"
    )
    assert summary["trades"] == 0
    assert "a result, not a failure to evaluate" in summary["note"]


def test_charging_more_spread_never_improves_the_result(bars: list[Bar]) -> None:
    """Cost drag is the one effect this repository has measured to significance."""
    probabilities = {i: 0.9 for i in range(30, len(bars) - 30, 15)}
    cheap = economics.evaluate(
        bars, probabilities, threshold=0.5, spread=float(SPREAD), symbol="EURUSD"
    )
    dear = economics.evaluate(
        bars, probabilities, threshold=0.5, spread=float(SPREAD) * 20, symbol="EURUSD"
    )
    assert dear["expectancy_points_net"] <= cheap["expectancy_points_net"]


def test_the_signal_is_long_only_because_the_label_is() -> None:
    signal = economics.build_signal({0: 0.9, 1: 0.1, 2: 0.7}, 4, threshold=0.5)
    assert list(signal) == [1.0, 0.0, 1.0, 0.0]


def test_a_drawdown_is_a_ratio_not_a_point_count() -> None:
    """The metals-points lesson: absolute points are not comparable across symbols."""
    rows = [{"net": 10.0}, {"net": -4.0}, {"net": 6.0}]
    assert economics.drawdown_ratio(rows) == pytest.approx(4.0 / 16.0)
    assert economics.drawdown_ratio([]) is None
    assert economics.drawdown_ratio([{"net": -1.0}]) is None


# ====================================================== 6. artifact loading


def test_a_stored_model_loads_back_into_one_that_predicts(dataset: Dataset) -> None:
    """Section 8. L24 wrote the artifact; L26 is the first to read it back."""
    version = ModelVersion(
        id="v1",
        model_id="m1",
        version=1,
        artifact_ref="trade_probability:1.0",
        feature_version="1.0",
        status="draft",
        params={
            "artifact": {
                "kind": "logistic",
                "coefficients": {
                    "features": ["rsi_14"],
                    "weights": [0.5],
                    "bias": -0.1,
                    "fitted_rows": 100,
                },
            }
        },
    )
    model = model_from_version(version)
    assert model.fitted
    assert model.identity.version == "1.0"
    prediction = model.predict(
        {"rsi_14": 0.4},
        at=datetime.now(UTC),
        symbol="EURUSD",
        timeframe="H1",
        feature_version="1.0",
    )
    assert prediction.ok


def test_a_version_with_no_artifact_is_refused_not_approximated() -> None:
    version = ModelVersion(id="v1", model_id="m1", version=1, feature_version="1.0", params={})
    with pytest.raises(ArtifactError) as exc:
        model_from_version(version)
    assert "no fitted parameters" in str(exc.value)


def test_an_unknown_artifact_kind_is_refused() -> None:
    version = ModelVersion(
        id="v1",
        model_id="m1",
        version=1,
        feature_version="1.0",
        params={"artifact": {"kind": "pickle", "path": "/etc/passwd"}},
    )
    with pytest.raises(ArtifactError) as exc:
        model_from_version(version)
    assert "cannot load" in str(exc.value)


def test_coefficients_that_do_not_line_up_are_refused() -> None:
    version = ModelVersion(
        id="v1",
        model_id="m1",
        version=1,
        feature_version="1.0",
        artifact_ref="trade_probability:1.0",
        params={
            "artifact": {
                "kind": "logistic",
                "coefficients": {"features": ["a", "b"], "weights": [0.5], "bias": 0.0},
            }
        },
    )
    with pytest.raises(ArtifactError):
        model_from_version(version)


# ========================================================= 7. the pipeline


async def test_a_real_candidate_validates_end_to_end(sessions, bars, dataset) -> None:  # noqa: ANN001
    version_id = await train_a_candidate(sessions, dataset)
    row = await validate(sessions, bars, dataset, version_id)

    assert row.status == COMPLETED
    assert row.verdict in VALIDATION_VERDICTS
    assert row.report is not None
    names = {check["check"] for check in row.report["checks"]}
    # Every check group the brief names is present in the report.
    assert {
        "data_integrity",
        "model_artifact",
        "leakage",
        "temporal",
        "baseline_comparison",
        "calibration",
        "economic",
        "robustness",
        "sample_size",
        "significance",
        "walk_forward",
        "regime",
        "overfitting",
        "discrimination",
        "version_locking",
    } <= names


async def test_a_report_is_named_verdicts_and_not_a_number(sessions, bars, dataset) -> None:  # noqa: ANN001
    """Section 27's worked example, on a real run."""
    version_id = await train_a_candidate(sessions, dataset)
    row = await validate(sessions, bars, dataset, version_id)
    payload = row.report
    assert payload is not None
    for check in payload["checks"]:
        assert check["severity"] in {"PASS", "WARNING", "FAIL", "BLOCKED"}
    assert payload["verdict"] == row.verdict
    assert "score" not in payload


async def test_a_passing_report_promotes_nothing(sessions, bars, dataset) -> None:  # noqa: ANN001
    """Section 34, and the whole point of the level."""
    version_id = await train_a_candidate(sessions, dataset)
    async with sessions() as db:
        before = (await db.get(ModelVersion, version_id)).status
    await validate(sessions, bars, dataset, version_id)
    async with sessions() as db:
        after = (await db.get(ModelVersion, version_id)).status
        promoted = await db.scalar(select(ModelVersion).where(ModelVersion.status == "promoted"))
    assert before == after == "draft"
    assert promoted is None


async def test_the_run_status_never_says_passed(sessions, bars, dataset) -> None:  # noqa: ANN001
    """A run status of `passed` is one keystroke from a passed MODEL."""
    assert "passed" not in VALIDATION_STATUSES
    version_id = await train_a_candidate(sessions, dataset)
    row = await validate(sessions, bars, dataset, version_id)
    assert row.status == COMPLETED
    assert row.verdict is not None


async def test_nothing_is_measured_on_data_the_model_was_fitted_on(  # noqa: ANN001
    sessions, bars, dataset
) -> None:
    """Section 12. The scored segment is the test segment, and it is named."""
    version_id = await train_a_candidate(sessions, dataset)
    row = await validate(sessions, bars, dataset, version_id)
    scored = row.report["context"]["scored"]
    assert scored["segment"] == "test"
    assert scored["segment_rows"] == list(dataset.split.test)
    assert scored["rows"] <= dataset.split.test[1] - dataset.split.test[0]


async def test_the_in_sample_figure_is_measured_not_borrowed(  # noqa: ANN001
    sessions, bars, dataset
) -> None:
    """L25 records an early-stopping figure from the VALIDATION segment."""
    version_id = await train_a_candidate(sessions, dataset)
    row = await validate(sessions, bars, dataset, version_id)
    in_sample = row.report["context"]["in_sample"]
    assert in_sample["segment"] == "train"
    assert in_sample["rows"] > 0


async def test_a_dataset_that_moves_under_a_running_job_blocks_the_report(  # noqa: ANN001
    sessions, bars, dataset
) -> None:
    """Section 6, checked rather than merely forbidden."""
    version_id = await train_a_candidate(sessions, dataset)
    other = make_dataset(make_bars(1100, seed=77))
    calls = {"n": 0}

    async def shifting(_db: AsyncSession):  # noqa: ANN202
        calls["n"] += 1
        return (bars, dataset) if calls["n"] == 1 else (bars, other)

    service = ValidationService(sessions)
    config = ValidationConfig(model_version_id=version_id, thresholds=LOOSE)
    async with sessions() as db:
        row = await service.queue(db, config, loader=shifting, user_id="u1")
    assert await service.wait_for(row.id)
    await service.shutdown()
    async with sessions() as db:
        done = await db.get(ValidationRun, row.id)
    assert done.verdict == "BLOCKED"
    assert any(
        "changed while validation ran" in check["summary"] for check in done.report["checks"]
    )


async def test_a_missing_candidate_is_refused_and_no_run_is_recorded(  # noqa: ANN001
    sessions, bars, dataset
) -> None:
    """A run cannot name a candidate that does not exist.

    This asserted a persisted FAILED run until the suite began enforcing
    foreign keys. It could never have happened: `model_version_id` is NOT NULL
    and a foreign key, so the row was unwritable against the real schema and
    against PostgreSQL -- SQLite was simply not checking. The refusal belongs
    at the door, where the caller learns which id was not found.
    """
    from app.validation.service import UnknownCandidate

    service = ValidationService(sessions)
    config = ValidationConfig(model_version_id="does-not-exist")
    async with sessions() as db:
        with pytest.raises(UnknownCandidate) as refused:
            await service.queue(db, config, loader=loader_for(bars, dataset), user_id="u1")
    assert "does-not-exist" in str(refused.value)
    await service.shutdown()

    # And nothing was left behind to be read as a real attempt.
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ValidationRun)) == 0


async def test_an_unloadable_artifact_blocks_and_reports_nothing_else(  # noqa: ANN001
    sessions, bars, dataset
) -> None:
    async with sessions() as db:
        version = ModelVersion(
            id="broken",
            model_id="m1",
            version=1,
            feature_version="1.0",
            status="draft",
            params={"artifact": {"kind": "logistic"}},
        )
        db.add(version)
        await db.commit()
    row = await validate(sessions, bars, dataset, "broken")
    assert row.verdict == "BLOCKED"
    names = {c["check"]: c["severity"] for c in row.report["checks"]}
    assert names["model_artifact"] == "BLOCKED"
    # And nothing downstream was reported as passing on an artifact that
    # never loaded.
    assert "economic" not in names


async def test_a_duplicate_run_under_the_same_thresholds_is_refused(  # noqa: ANN001
    sessions, bars, dataset
) -> None:
    version_id = await train_a_candidate(sessions, dataset)
    gate = asyncio.Event()
    service = ValidationService(sessions)
    config = ValidationConfig(model_version_id=version_id, thresholds=LOOSE)
    async with sessions() as db:
        await service.queue(db, config, loader=gated_loader(bars, dataset, gate), user_id="u1")
        with pytest.raises(DuplicateValidationJob):
            await service.queue(db, config, loader=gated_loader(bars, dataset, gate), user_id="u1")
    gate.set()
    await service.shutdown()


async def test_a_different_threshold_is_a_different_run(sessions, bars, dataset) -> None:  # noqa: ANN001
    version_id = await train_a_candidate(sessions, dataset)
    gate = asyncio.Event()
    service = ValidationService(sessions)
    async with sessions() as db:
        await service.queue(
            db,
            ValidationConfig(model_version_id=version_id, thresholds=LOOSE),
            loader=gated_loader(bars, dataset, gate),
            user_id="u1",
        )
        second = await service.queue(
            db,
            ValidationConfig(
                model_version_id=version_id,
                thresholds=Thresholds(minimum_samples=50, minimum_trades=5, permutations=60),
            ),
            loader=gated_loader(bars, dataset, gate),
            user_id="u1",
        )
    assert second is not None
    gate.set()
    await service.shutdown()


async def test_a_cancelled_run_is_recorded_as_cancelled_never_as_a_verdict(  # noqa: ANN001
    sessions, bars, dataset
) -> None:
    version_id = await train_a_candidate(sessions, dataset)
    gate = asyncio.Event()
    service = ValidationService(sessions)
    config = ValidationConfig(model_version_id=version_id, thresholds=LOOSE)
    async with sessions() as db:
        row = await service.queue(
            db, config, loader=gated_loader(bars, dataset, gate), user_id="u1"
        )
    assert await service.cancel(row.id)
    gate.set()
    await service.wait_for(row.id)
    await service.shutdown()
    async with sessions() as db:
        done = await db.get(ValidationRun, row.id)
    assert done.status == CANCELLED
    assert done.verdict is None
    assert done.report is None


async def test_the_report_records_the_thresholds_it_used(sessions, bars, dataset) -> None:  # noqa: ANN001
    """Section 25. A report read against today's defaults is read at the wrong bar."""
    version_id = await train_a_candidate(sessions, dataset)
    row = await validate(
        sessions,
        bars,
        dataset,
        version_id,
        thresholds=Thresholds(minimum_samples=50, minimum_trades=5, minimum_profit_factor=1.9),
    )
    used = row.report["config"]["thresholds"]
    assert used["minimum_profit_factor"] == 1.9
    assert row.report["validation_engine_version"] == VALIDATION_ENGINE_VERSION


async def test_two_identical_runs_reach_the_same_verdict(sessions, bars, dataset) -> None:  # noqa: ANN001
    """Every random draw is seeded; a verdict that moves is not a verdict."""
    version_id = await train_a_candidate(sessions, dataset)
    first = await validate(sessions, bars, dataset, version_id)
    second = await validate(sessions, bars, dataset, version_id)
    assert first.verdict == second.verdict
    assert first.summary == second.summary


# ========================================================== 8. the summary


async def test_the_api_row_says_what_a_pass_authorises(sessions, bars, dataset) -> None:  # noqa: ANN001
    version_id = await train_a_candidate(sessions, dataset)
    row = await validate(sessions, bars, dataset, version_id)
    payload = summarise(row)
    assert "CONSIDERATION" in payload["authority"]
    assert "not a promotion" in payload["authority"]
    assert sum(v for v in payload["checks"].values() if v) > 0


def test_the_markdown_renders_the_table_the_brief_asks_for() -> None:
    report = ValidationReport(config=ValidationConfig(model_version_id="v1"))
    report.add(checks.Finding("data_integrity", Severity.passed, "READY"))
    report.add(checks.Finding("calibration", Severity.warning, "ECE 0.14"))
    text = markdown(report)
    assert "| data_integrity | PASS |" in text
    assert "| calibration | WARNING |" in text
    assert "CONDITIONAL" in text
    assert "There is no score" in text


def test_recommendations_come_from_the_findings_not_from_advice() -> None:
    report = ValidationReport(config=ValidationConfig(model_version_id="v1"))
    report.add(checks.Finding("economic", Severity.failed, "profit factor 0.7"))
    report.add(checks.Finding("calibration", Severity.warning, "ECE 0.2"))
    lines = report.recommendations()
    assert any("economic must improve" in line for line in lines)
    assert any("calibration is a caveat" in line for line in lines)


# ============================================================= 9. safety


VALIDATION_MODULES = sorted((Path(__file__).parent.parent / "app" / "validation").glob("*.py"))

FORBIDDEN_IMPORTS = (
    "app.execution",
    "app.oms",
    "app.risk",
    "app.sizing",
    "app.brokers",
    "app.orders",
    "app.strategies",
    "app.bots",
    "app.paper",
)


def _imported_modules(path: Path) -> set[str]:
    """Imports parsed with `ast`, not grepped.

    A grep matches the docstring that explains the rule, which is how L24's
    version of this test failed on prose.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_validation_cannot_reach_a_venue_or_an_order() -> None:
    """Sections 30 and 50. Parsed, because 'we would notice' is not a control."""
    assert VALIDATION_MODULES, "no validation modules were found to parse"
    for path in VALIDATION_MODULES:
        for imported in _imported_modules(path):
            for banned in FORBIDDEN_IMPORTS:
                assert not imported.startswith(banned), f"{path.name} imports {imported}"


def test_validation_never_writes_a_model_version_status() -> None:
    """Section 34. A PASS is not a promotion, and there is no code path to one."""
    for path in VALIDATION_MODULES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "status"
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "version"
                    ):  # pragma: no cover - the assertion is the point
                        raise AssertionError(f"{path.name} writes a model version status")
        assert "ModelVersion.status" not in source


def test_validation_cannot_enable_live_trading() -> None:
    for path in VALIDATION_MODULES:
        source = path.read_text(encoding="utf-8")
        assert "LIVE_TRADING" not in source
        assert "live_trading" not in source.replace("enable live trading", "")


def test_the_engine_is_versioned_so_two_reports_are_comparable() -> None:
    """A report stores the version it was produced under."""
    assert VALIDATION_ENGINE_VERSION
    report = ValidationReport(config=ValidationConfig(model_version_id="v1"))
    report.add(checks.Finding("x", Severity.passed, "fine"))
    assert report.as_dict()["validation_engine_version"] == VALIDATION_ENGINE_VERSION


def test_a_report_carries_no_credential(dataset: Dataset) -> None:
    """Section 46. A report is the most-read artefact this level produces."""
    report = ValidationReport(config=ValidationConfig(model_version_id="v1", notes="hello"))
    report.add(checks.data_integrity(dataset, LOOSE))
    text = str(report.as_dict()).lower()
    for banned in ("password", "secret", "token", "api_key", "credential"):
        assert banned not in text
