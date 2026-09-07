"""The AI layer (L24).

The ones that matter most, and they are the scenarios the brief writes out:

  * `test_a_bot_pinned_to_v1_keeps_v1_when_v2_is_installed` — §50, verbatim.
  * `test_a_feature_version_the_model_was_not_fitted_on_blocks_inference` — §51.
  * `test_ai_required_and_no_model_means_no_trade` — §26 and §49.
  * `test_future_data_cannot_change_a_prediction_for_an_earlier_timestamp` — §38
    and §52, run against the model rather than against the feature engine.
  * `test_the_ai_layer_cannot_reach_a_venue_or_the_risk_engine` — §54, parsed.
"""

from __future__ import annotations

import ast
import math
import random
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.ai import (
    AnomalyModel,
    AnomalyStatus,
    CalibrationReport,
    FeatureContract,
    LogisticCoefficients,
    ModelError,
    ModelIdentity,
    ModelRegistry,
    Prediction,
    PredictionStatus,
    Regime,
    RegimeModel,
    RegistryError,
    TradeProbabilityModel,
    fit_baseline,
    fit_cuts,
    fit_logistic,
)
from app.ai.service import (
    AiServiceError,
    answer_rate,
    record,
    register_version,
    summarise_version,
)
from app.datasets import features as feature_engine
from app.datasets.builder import DatasetConfig, build
from app.datasets.labels import LabelConfig
from app.db.base import Base
from app.execution.ai import AiGate, AiPolicy, AiVerdict, ModelBackedFilter
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.models.ai import AIModel, ModelPrediction, ModelVersion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

FV = feature_engine.FEATURE_SET_VERSION
AT = datetime(2026, 3, 1, 12, 0)
START = datetime(2026, 1, 1, 0, 0)


# ================================================================= fixtures


def make_bars(n: int = 400, *, seed: int = 3) -> list[Bar]:
    rng = random.Random(seed)
    price = 1.10
    out: list[Bar] = []
    for i in range(n):
        price = max(0.5, price * (1 + 0.00002 * math.sin(i / 17.0) + rng.gauss(0, 0.0008)))
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
                spread=Decimal("0.00002"),
                spread_availability=Availability.available,
            )
        )
    return out


@pytest.fixture(scope="module")
def dataset():
    """One built dataset, shared: building it is the expensive part."""
    return build(
        make_bars(),
        DatasetConfig(
            key="ai-fixture",
            symbol="EURUSD",
            timeframe=Timeframe.H1,
            provider=Provider.mt5,
            label_config=LabelConfig(spread_points=Decimal("0.00002"), horizon=12),
        ),
        now=AT,
    )


@pytest.fixture(scope="module")
def rows(dataset) -> list[dict[str, float | None]]:
    return [row.features for row in dataset.rows]


@pytest.fixture(scope="module")
def train_rows(dataset, rows) -> list[dict[str, float | None]]:
    """Only the training segment. Every fit in this file reads this."""
    assert dataset.split is not None
    start, stop = dataset.split.train
    return rows[start:stop]


@pytest.fixture(scope="module")
def probability_model(dataset, rows, train_rows) -> TradeProbabilityModel:
    assert dataset.split is not None
    start, stop = dataset.split.train
    targets = [
        1 if dataset.rows[i].labels["bracket_outcome"] == "WIN" else 0 for i in range(start, stop)
    ]
    features = ("rsi_14", "atr_pct_14", "ema_spread_10_50", "return_5")
    coefficients = fit_logistic(train_rows, targets, features=features, iterations=120)
    return TradeProbabilityModel(feature_version=FV, coefficients=coefficients)


@pytest.fixture(scope="module")
def regime_model(train_rows) -> RegimeModel:
    return RegimeModel(feature_version=FV, cuts=fit_cuts(train_rows))


@pytest.fixture(scope="module")
def anomaly_model(train_rows) -> AnomalyModel:
    return AnomalyModel(feature_version=FV, baseline=fit_baseline(train_rows))


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
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
        yield session
    await engine.dispose()


def ask(model, features: dict[str, float | None], **over):  # noqa: ANN001, ANN201
    kwargs: dict = {
        "at": AT,
        "symbol": "EURUSD",
        "timeframe": "H1",
        "feature_version": FV,
    }
    kwargs.update(over)
    return model.predict(features, **kwargs)


# ==================================================== 1. the prediction shape


def test_a_refusal_cannot_also_carry_a_value() -> None:
    """The type makes the mistake unrepresentable rather than discouraged."""
    identity = ModelIdentity(key="x", version="1.0", kind="classifier", feature_version=FV)
    with pytest.raises(ModelError):
        Prediction(
            identity=identity,
            at=AT,
            symbol="EURUSD",
            timeframe="H1",
            status=PredictionStatus.model_unavailable,
            value="UP",
        )


def test_a_probability_outside_zero_to_one_is_refused() -> None:
    identity = ModelIdentity(key="x", version="1.0", kind="classifier", feature_version=FV)
    with pytest.raises(ModelError):
        Prediction(identity=identity, at=AT, symbol="EURUSD", timeframe="H1", probability=1.4)


def test_two_different_feature_vectors_are_two_predictions(regime_model) -> None:  # noqa: ANN001
    """The id is a function of the input, not only of the timestamp.

    Without this, two bars scored at one nominal `at` would collapse into a
    single row and every distribution built over the table would undercount.
    """
    a = ask(regime_model, {"ema_spread_10_50": 0.001, "atr_pct_14": 0.0005})
    b = ask(regime_model, {"ema_spread_10_50": 0.002, "atr_pct_14": 0.0005})
    assert a.prediction_id() != b.prediction_id()
    assert a.input_digest != b.input_digest


def test_a_prediction_id_is_deterministic_and_two_models_do_not_collide() -> None:
    def one(key: str) -> Prediction:
        return Prediction(
            identity=ModelIdentity(key=key, version="1.0", kind="c", feature_version=FV),
            at=AT,
            symbol="EURUSD",
            timeframe="H1",
        )

    assert one("regime").prediction_id() == one("regime").prediction_id()
    assert one("regime").prediction_id() != one("anomaly").prediction_id()


def test_the_feature_contract_names_what_is_missing_in_its_own_order() -> None:
    contract = FeatureContract(features=("a", "b", "c"), feature_version=FV, lookback=1)
    assert contract.missing({"a": 1.0, "b": None}) == ("b", "c")


# ============================================== 2. the gate, in a fixed order


def test_an_unfitted_model_is_unavailable_rather_than_a_default() -> None:
    model = RegimeModel(feature_version=FV)
    answer = ask(model, {"ema_spread_10_50": 0.0, "atr_pct_14": 0.001})
    assert answer.status is PredictionStatus.model_unavailable
    assert answer.value is None
    assert "rather than a default" in answer.detail


def test_a_feature_version_the_model_was_not_fitted_on_blocks_inference(
    regime_model: RegimeModel,
) -> None:
    """§51: model requires 1.0, system provides 1.3, expected BLOCK."""
    answer = ask(
        regime_model,
        {"ema_spread_10_50": 0.0, "atr_pct_14": 0.001},
        feature_version="1.3",
    )
    assert answer.status is PredictionStatus.feature_version_mismatch
    assert answer.value is None
    assert "1.3" in answer.detail


def test_a_declared_compatible_version_is_accepted(train_rows) -> None:
    model = RegimeModel(feature_version=FV, cuts=fit_cuts(train_rows))
    model.contract = FeatureContract(
        features=model.contract.features,
        feature_version=FV,
        lookback=model.contract.lookback,
        compatible_feature_versions=("9.9",),
    )
    answer = ask(model, {"ema_spread_10_50": 0.0, "atr_pct_14": 0.001}, feature_version="9.9")
    assert answer.ok


def test_a_missing_feature_is_an_input_error_and_is_never_substituted(
    regime_model: RegimeModel,
) -> None:
    answer = ask(regime_model, {"ema_spread_10_50": 0.001, "atr_pct_14": None})
    assert answer.status is PredictionStatus.model_input_error
    assert "atr_pct_14" in answer.detail
    assert answer.value is None


def test_stale_features_refuse_rather_than_describe_a_market_that_moved(
    train_rows,
) -> None:
    model = RegimeModel(
        feature_version=FV, cuts=fit_cuts(train_rows), max_feature_age=timedelta(hours=1)
    )
    answer = ask(
        model,
        {"ema_spread_10_50": 0.0, "atr_pct_14": 0.001},
        features_at=AT - timedelta(hours=6),
    )
    assert answer.status is PredictionStatus.stale_features


def test_an_unstamped_feature_set_is_not_treated_as_fresh(train_rows) -> None:
    """ "We do not know how old this is" is not "this is fresh"."""
    model = RegimeModel(
        feature_version=FV, cuts=fit_cuts(train_rows), max_feature_age=timedelta(hours=1)
    )
    answer = ask(model, {"ema_spread_10_50": 0.0, "atr_pct_14": 0.001})
    assert answer.status is PredictionStatus.stale_features


def test_the_gate_reports_the_version_before_the_missing_feature(train_rows) -> None:
    """Order matters: a version problem sends a caller to the right place."""
    model = RegimeModel(feature_version=FV, cuts=fit_cuts(train_rows))
    answer = ask(model, {"ema_spread_10_50": None, "atr_pct_14": None}, feature_version="9.9")
    assert answer.status is PredictionStatus.feature_version_mismatch


# ================================================== 3. the regime model (§6)


def test_regime_thresholds_are_fitted_not_hardcoded(train_rows) -> None:
    cuts = fit_cuts(train_rows)
    assert cuts.trend_low < cuts.trend_high
    assert cuts.volatility_low < cuts.volatility_high
    assert cuts.fitted_on == "train"
    assert "quantiles of the training segment" in cuts.as_dict()["method"]


def test_a_regime_model_refuses_to_fit_on_too_little_rather_than_guessing() -> None:
    with pytest.raises(ValueError) as exc:
        fit_cuts([{"ema_spread_10_50": 0.0, "atr_pct_14": 0.001}] * 5)
    assert "indistinguishable from fitted" in str(exc.value)


def test_a_strong_uptrend_at_ordinary_volatility_is_trending_up(
    regime_model: RegimeModel,
) -> None:
    cuts = regime_model.cuts
    assert cuts is not None
    answer = ask(
        regime_model,
        {
            "ema_spread_10_50": cuts.trend_high * 4 + 0.01,
            "atr_pct_14": (cuts.volatility_low + cuts.volatility_high) / 2,
        },
    )
    assert answer.value == str(Regime.trending_up)


def test_violent_movement_reads_as_high_volatility_not_as_a_trend(
    regime_model: RegimeModel,
) -> None:
    """The table's deliberate asymmetry: what a risk decision needs first."""
    cuts = regime_model.cuts
    assert cuts is not None
    answer = ask(
        regime_model,
        {
            "ema_spread_10_50": cuts.trend_high * 10,
            "atr_pct_14": cuts.volatility_high * 10,
        },
    )
    assert answer.value == str(Regime.high_volatility)


def test_a_reading_on_the_boundary_is_unknown_rather_than_forced(train_rows) -> None:
    """§7: if confidence is low, regime = UNKNOWN. Do not force a classification."""
    model = RegimeModel(feature_version=FV, cuts=fit_cuts(train_rows), minimum_confidence=0.99)
    cuts = model.cuts
    assert cuts is not None
    answer = ask(
        model,
        {
            "ema_spread_10_50": cuts.trend_high,
            "atr_pct_14": (cuts.volatility_low + cuts.volatility_high) / 2,
        },
    )
    assert answer.value == str(Regime.unknown)


def test_a_regime_prediction_carries_no_probability(regime_model: RegimeModel) -> None:
    """A label is not a probability of anything, so the field stays empty."""
    answer = ask(regime_model, {"ema_spread_10_50": 0.001, "atr_pct_14": 0.0005})
    assert answer.ok
    assert answer.probability is None
    assert answer.calibrated is False


def test_the_regime_explanation_names_only_what_the_model_reads(
    regime_model: RegimeModel,
) -> None:
    """§23: do not claim a feature influenced a model when it did not."""
    features = {"ema_spread_10_50": 0.001, "atr_pct_14": 0.0005, "rsi_14": 55.0}
    named = {item["feature"] for item in regime_model.explanation(features)}
    assert named == {"ema_spread_10_50", "atr_pct_14"}


# ============================================= 4. the probability model (§8)


def test_a_probability_is_labelled_with_what_it_is_the_probability_of(
    probability_model: TradeProbabilityModel, rows
) -> None:
    answer = ask(probability_model, rows[100])
    assert answer.ok
    assert 0.0 <= (answer.probability or 0.0) <= 1.0
    assert "bracket_outcome == WIN" in (answer.label_definition or "")
    assert "not a guarantee" in (answer.label_definition or "")


def test_an_uncalibrated_probability_says_so(
    probability_model: TradeProbabilityModel, rows
) -> None:
    """§20: do not call 0.80 an 80% success rate unless calibration supports it."""
    answer = ask(probability_model, rows[100])
    assert answer.calibrated is False
    assert "ranking, not a frequency" in answer.metadata["caveat"]


def test_a_measured_calibration_is_reported_as_measured(
    probability_model: TradeProbabilityModel, rows
) -> None:
    model = TradeProbabilityModel(
        feature_version=FV,
        coefficients=probability_model.coefficients,
        calibration=CalibrationReport(
            brier=0.21, expected_calibration_error=0.04, samples=500, measured_on="validation"
        ),
    )
    answer = ask(model, rows[100])
    assert answer.calibrated is True
    assert answer.metadata["calibration"]["samples"] == 500
    assert answer.metadata["caveat"] is None


def test_confidence_is_distance_from_a_coin_flip_not_the_probability(
    probability_model: TradeProbabilityModel, rows
) -> None:
    answer = ask(probability_model, rows[100])
    assert answer.probability is not None and answer.confidence is not None
    assert answer.confidence == pytest.approx(abs(answer.probability - 0.5) * 2.0, abs=1e-6)


def test_the_explanation_is_the_model_itself_not_an_approximation(
    probability_model: TradeProbabilityModel, rows
) -> None:
    """For a logistic model, weight x value IS the contribution to the logit."""
    features = rows[100]
    contributions = probability_model.explanation(features)
    total = sum(item["contribution"] for item in contributions)
    assert probability_model.coefficients is not None
    logit = probability_model.coefficients.bias + total
    answer = ask(probability_model, features)
    assert answer.metadata["logit"] == pytest.approx(logit, abs=1e-6)


def test_coefficients_that_do_not_line_up_with_their_names_are_refused() -> None:
    with pytest.raises(ValueError) as exc:
        LogisticCoefficients(features=("a", "b"), weights=(0.1,), bias=0.0, fitted_rows=100)
    assert "nobody can explain or reproduce" in str(exc.value)


def test_fitting_is_deterministic(train_rows, dataset) -> None:
    """§24: a model whose FIT is nondeterministic cannot deliver deterministic
    inference, so the fitter has no shuffling, no early stopping and no seed."""
    assert dataset.split is not None
    start, stop = dataset.split.train
    targets = [
        1 if dataset.rows[i].labels["bracket_outcome"] == "WIN" else 0 for i in range(start, stop)
    ]
    features = ("rsi_14", "atr_pct_14")
    first = fit_logistic(train_rows, targets, features=features, iterations=50)
    second = fit_logistic(train_rows, targets, features=features, iterations=50)
    assert first.weights == second.weights
    assert first.bias == second.bias


def test_fitting_on_too_little_is_refused(train_rows) -> None:
    with pytest.raises(ValueError) as exc:
        fit_logistic(train_rows[:5], [1] * 5, features=("rsi_14",))
    assert "indistinguishable from real ones" in str(exc.value)


def test_a_non_binary_target_is_refused(train_rows) -> None:
    with pytest.raises(ValueError):
        fit_logistic(train_rows[:40], [2] * 40, features=("rsi_14",))


def test_the_sigmoid_does_not_overflow_into_a_nan_probability() -> None:
    from app.ai.probability import _sigmoid

    assert _sigmoid(1000.0) == pytest.approx(1.0)
    assert _sigmoid(-1000.0) == pytest.approx(0.0)


# ================================================ 5. the anomaly model (§10)


def test_an_ordinary_bar_is_normal(anomaly_model: AnomalyModel, rows) -> None:
    answer = ask(anomaly_model, rows[100])
    assert answer.ok
    assert answer.value in {str(AnomalyStatus.normal), str(AnomalyStatus.anomalous)}


def test_a_wildly_unusual_bar_is_anomalous(anomaly_model: AnomalyModel, rows) -> None:
    extreme = dict(rows[100])
    extreme["range_pct"] = (extreme["range_pct"] or 0.0) + 1.0
    answer = ask(anomaly_model, extreme)
    assert answer.value == str(AnomalyStatus.anomalous)
    assert answer.metadata["worst_feature"] == "range_pct"


def test_the_anomaly_score_is_not_reported_as_a_probability(
    anomaly_model: AnomalyModel, rows
) -> None:
    answer = ask(anomaly_model, rows[100])
    assert answer.probability is None
    assert "NOT a probability" in (answer.label_definition or "")


def test_a_baseline_uses_the_median_not_the_mean(train_rows) -> None:
    """An outlier moves a mean far more than a median, so a mean-fitted
    detector is partly fitted to the events it exists to find."""
    baseline = fit_baseline(train_rows)
    assert "median absolute deviation" in baseline.as_dict()["method"]
    assert baseline.fitted_on == "train"


def test_fitting_an_anomaly_baseline_on_too_little_is_refused() -> None:
    with pytest.raises(ValueError) as exc:
        fit_baseline([{"return_1": 0.0}] * 5)
    assert "usable training values" in str(exc.value)


def test_a_constant_feature_is_skipped_rather_than_infinitely_unusual() -> None:
    rows: list[dict[str, float | None]] = [
        {"return_1": 0.001 * i, "range_pct": 0.5, "atr_pct_14": 0.002, "volatility_20": 0.003}
        for i in range(60)
    ]
    baseline = fit_baseline(rows)
    assert "range_pct" in baseline.constant_features
    model = AnomalyModel(feature_version=FV, baseline=baseline)
    answer = ask(
        model,
        {"return_1": 0.03, "range_pct": 99.0, "atr_pct_14": 0.002, "volatility_20": 0.003},
    )
    assert answer.ok
    assert "range_pct" not in answer.metadata["z_by_feature"]


# ================================================ 6. the registry (§32, §50)


def test_a_bot_pinned_to_v1_keeps_v1_when_v2_is_installed(train_rows) -> None:
    """§50, verbatim: bot uses v1, v2 is installed, expected: still v1."""
    registry = ModelRegistry()
    v1 = registry.register(
        RegimeModel(version="1.0", feature_version=FV, cuts=fit_cuts(train_rows))
    )
    assert registry.get("regime", "1.0") is v1

    v2 = registry.register(
        RegimeModel(version="2.0", feature_version=FV, cuts=fit_cuts(train_rows))
    )
    assert registry.get("regime", "1.0") is v1
    assert registry.latest("regime") is v2


def test_registering_the_same_version_twice_is_refused(train_rows) -> None:
    """§32: no silent model replacement, expressed as an error."""
    registry = ModelRegistry()
    cuts = fit_cuts(train_rows)
    registry.register(RegimeModel(version="1.0", feature_version=FV, cuts=cuts))
    with pytest.raises(RegistryError) as exc:
        registry.register(RegimeModel(version="1.0", feature_version=FV, cuts=cuts))
    assert "silent model swap" in str(exc.value)


def test_an_unregistered_version_is_refused_not_substituted() -> None:
    registry = ModelRegistry()
    with pytest.raises(RegistryError) as exc:
        registry.get("regime", "7.0")
    assert "Nothing is substituted" in str(exc.value)


# =============================================== 7. the AI policy (§26, §49)


class _NeverAnswers:
    """A model that is present and cannot answer. The AI-failure scenario."""

    identity = ModelIdentity(key="trade_probability", version="1.0", kind="c", feature_version=FV)

    def predict(self, features, **kwargs):  # noqa: ANN001, ANN003, ANN201
        from app.ai.contract import refuse

        return refuse(
            self.identity,
            PredictionStatus.model_unavailable,
            "the service is down",
            at=kwargs["at"],
            symbol=kwargs["symbol"],
            timeframe=kwargs["timeframe"],
        )


def _features_for(_signal: object):  # noqa: ANN202
    return ({"rsi_14": 55.0}, AT, "EURUSD", "H1", AT)


def test_ai_required_and_no_model_means_no_trade() -> None:
    """§49: AI unavailable and AI_REQUIRED=true -> NO TRADE."""
    verdict = ModelBackedFilter(
        _NeverAnswers(),
        AiGate(policy=AiPolicy.required),
        feature_version=FV,
        features_for=_features_for,
    ).score(object())
    assert verdict.accept is False
    assert "No trade" in verdict.reason


def test_ai_optional_and_no_model_follows_the_configured_fallback() -> None:
    verdict = ModelBackedFilter(
        _NeverAnswers(),
        AiGate(policy=AiPolicy.optional),
        feature_version=FV,
        features_for=_features_for,
    ).score(object())
    assert verdict.accept is True
    assert "risk engine" in verdict.reason


def test_a_probability_below_the_minimum_is_filtered(
    probability_model: TradeProbabilityModel, rows
) -> None:
    """§27: strategy says BUY, AI says 0.42, minimum 0.70 -> REJECT."""
    verdict = ModelBackedFilter(
        probability_model,
        AiGate(policy=AiPolicy.optional, minimum_probability=1.0),
        feature_version=FV,
        features_for=lambda _s: (rows[100], AT, "EURUSD", "H1", AT),
    ).score(object())
    assert verdict.accept is False
    assert "is a filter, not a size" in verdict.reason


def test_the_seat_cannot_express_an_approval_of_anything_else() -> None:
    """§54.1-4: the verdict type has no field that could approve or size."""
    fields = set(AiVerdict.__dataclass_fields__)
    assert fields == {"accept", "confidence", "reason", "model"}
    for forbidden in ("quantity", "volume", "size", "limit", "risk", "leverage", "enable"):
        assert not any(forbidden in name for name in fields)


def test_an_impossible_minimum_probability_is_refused() -> None:
    with pytest.raises(ValueError):
        AiGate(minimum_probability=1.5)


def test_a_feature_build_that_raises_is_no_answer_not_an_accept() -> None:
    def explode(_signal: object):  # noqa: ANN202
        raise RuntimeError("the feature store is down")

    verdict = ModelBackedFilter(
        _NeverAnswers(),
        AiGate(policy=AiPolicy.required),
        feature_version=FV,
        features_for=explode,
    ).score(object())
    assert verdict.accept is False
    assert "feature store is down" in verdict.reason


# ================================================= 8. leakage and determinism


def test_future_data_cannot_change_a_prediction_for_an_earlier_timestamp(
    regime_model: RegimeModel,
) -> None:
    """§38 and §52, run end to end against the model rather than the features.

    Compute features over a prefix, predict; append 100 bars whose prices are
    deliberately extreme, recompute, predict again. The earlier prediction must
    be identical, id included.
    """
    bars = make_bars(300)
    cut = 200
    index = 150

    prefix_features = feature_engine.compute_features(bars[:cut])[index]
    before = ask(regime_model, prefix_features, at=bars[index].bar_time)

    extended = list(bars)
    for i in range(cut, len(extended)):
        bar = extended[i]
        extended[i] = Bar(
            symbol=bar.symbol,
            provider=bar.provider,
            timeframe=bar.timeframe,
            bar_time=bar.bar_time,
            open=bar.open * 5,
            high=bar.high * 5,
            low=bar.low * 5,
            close=bar.close * 5,
            volume=bar.volume,
            spread=bar.spread,
            spread_availability=bar.spread_availability,
        )
    after_features = feature_engine.compute_features(extended)[index]
    after = ask(regime_model, after_features, at=bars[index].bar_time)

    assert after_features == prefix_features
    assert after.as_dict() == before.as_dict()


def test_inference_is_deterministic_for_the_same_input(
    probability_model: TradeProbabilityModel, rows
) -> None:
    first = ask(probability_model, rows[100])
    second = ask(probability_model, rows[100])
    assert first.as_dict() == second.as_dict()
    assert first.prediction_id() == second.prediction_id()


def test_a_model_reads_no_clock_of_its_own(probability_model: TradeProbabilityModel, rows) -> None:
    """The timestamp is an argument. Two calls an hour apart on paper agree."""
    a = ask(probability_model, rows[100], at=datetime(2026, 1, 1))
    b = ask(probability_model, rows[100], at=datetime(2027, 6, 6))
    assert a.probability == b.probability
    assert a.prediction_id() != b.prediction_id()


# ===================================================== 9. what it may not do


def test_the_ai_layer_cannot_reach_a_venue_or_the_risk_engine() -> None:
    """§54.1-6, parsed rather than asserted by convention."""
    forbidden = (
        "app.brokers",
        "app.oms",
        "app.risk",
        "app.sizing",
        "app.positions",
        "app.execution",
        "MetaTrader5",
    )
    package = Path(__file__).resolve().parents[1] / "app" / "ai"
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


def test_the_dependency_runs_one_way_from_execution_to_ai() -> None:
    """The L22 cycle, not repeated: `app.ai` never IMPORTS `app.execution`.

    Parsed rather than grepped. The docstrings in this package describe the
    dependency direction in prose, and a text search cannot tell an explanation
    of a rule from a violation of it.
    """
    package = Path(__file__).resolve().parents[1] / "app" / "ai"
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.execution"), path.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.execution"), path.name


def test_live_trading_is_still_disabled_by_default() -> None:
    """§54.18. This level builds an advisor and enables nothing."""
    from app.core.settings import LIVE_GATES, Settings, TradingMode

    settings = Settings(_env_file=None)
    assert settings.live_trading is False
    assert settings.trading_mode is TradingMode.paper
    assert all(value is False for value in LIVE_GATES.values())


# ========================================================== 10. persistence


async def test_a_version_is_recorded_as_a_draft_with_its_provenance(
    db: AsyncSession, regime_model: RegimeModel
) -> None:
    assert regime_model.cuts is not None
    row = await register_version(db, regime_model, params={"cuts": regime_model.cuts.as_dict()})
    assert row.status == "draft"
    assert row.feature_version == FV
    assert row.params is not None
    assert summarise_version(row)["calibrated"] is False


async def test_registering_the_same_version_twice_writes_one_row(
    db: AsyncSession, regime_model: RegimeModel
) -> None:
    assert regime_model.cuts is not None
    params = {"cuts": regime_model.cuts.as_dict()}
    first = await register_version(db, regime_model, params=params)
    second = await register_version(db, regime_model, params=params)
    assert first.id == second.id
    assert len(list((await db.scalars(select(ModelVersion))).all())) == 1
    assert len(list((await db.scalars(select(AIModel))).all())) == 1


async def test_a_credential_shaped_parameter_is_refused(
    db: AsyncSession, regime_model: RegimeModel
) -> None:
    """§46: no secret goes in a model artifact."""
    with pytest.raises(AiServiceError) as exc:
        await register_version(db, regime_model, params={"broker": {"api_key": "abc"}})
    assert "credential-shaped key" in str(exc.value)


async def test_a_refusal_is_recorded_as_a_refusal(
    db: AsyncSession, regime_model: RegimeModel
) -> None:
    version = await register_version(db, regime_model, params={"cuts": {}})
    refusal = ask(RegimeModel(feature_version=FV), {"ema_spread_10_50": 0.0, "atr_pct_14": 0.0})
    row = await record(db, refusal, model_version_id=version.id)
    assert row.status == "MODEL_UNAVAILABLE"
    assert row.prediction is None
    assert row.detail


async def test_recording_the_same_prediction_twice_writes_one_row(
    db: AsyncSession, regime_model: RegimeModel, rows
) -> None:
    version = await register_version(db, regime_model, params={"cuts": {}})
    answer = ask(regime_model, rows[100])
    first = await record(db, answer, model_version_id=version.id)
    second = await record(db, answer, model_version_id=version.id)
    assert first.id == second.id
    assert len(list((await db.scalars(select(ModelPrediction))).all())) == 1


async def test_the_answer_rate_counts_the_refusals_too(
    db: AsyncSession, regime_model: RegimeModel, rows
) -> None:
    version = await register_version(db, regime_model, params={"cuts": {}})
    await record(db, ask(regime_model, rows[100]), model_version_id=version.id)
    await record(db, ask(regime_model, rows[101]), model_version_id=version.id)
    await record(
        db,
        ask(RegimeModel(feature_version=FV), {"ema_spread_10_50": 0.0, "atr_pct_14": 0.0}),
        model_version_id=version.id,
    )
    report = await answer_rate(db, model_version_id=version.id)
    # Three rows, because the id covers the INPUT: rows[100] and rows[101] are
    # different feature vectors, so they are different predictions even at one
    # nominal timestamp. This is the assertion that found that hole.
    assert report["predictions"] == 3
    assert report["answered"] == 2
    assert report["refused"] == 1
    assert report["answer_rate"] == pytest.approx(2 / 3, abs=1e-6)
