"""The AI data pipeline (L23).

The three that matter most, and they are the three the brief calls critical:

  * `test_a_feature_at_T_does_not_change_when_the_future_arrives` — section 56,
    run literally. Compute through T, append bars after T, recompute, and
    require the row at T to be identical.
  * `test_a_scaler_cannot_be_fitted_on_anything_but_the_training_segment` —
    section 58. The API has no spelling for the mistake, and the check re-fits
    rather than trusting the claim.
  * `test_the_same_bars_and_configuration_rebuild_the_same_dataset` — section
    59. Reproducibility is why the rows are not stored.

Everything else here is section 55's list.
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
from app.datasets import features as feature_engine
from app.datasets import labels as label_engine
from app.datasets import leakage, quality, resampling, scaler, splits
from app.datasets.builder import (
    BuildError,
    DatasetConfig,
    DatasetStatus,
    build,
    class_balances,
)
from app.datasets.service import (
    DatasetServiceError,
    build_and_register,
    load_bars,
    register,
    summarise,
)
from app.db.base import Base
from app.marketdata.types import Availability, Bar, Provider, Timeframe, seconds_of
from app.models.datasets import DatasetCheck, DatasetRecord, FeatureSet, LabelSet
from app.models.market import MarketBar, Symbol
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

START = datetime(2026, 1, 1, 0, 0)
SPREAD = Decimal("0.00002")


# ================================================================= fixtures


def make_bars(
    n: int = 400,
    *,
    seed: int = 7,
    timeframe: Timeframe = Timeframe.H1,
    volume: bool = True,
    spread: bool = True,
    start: datetime = START,
) -> list[Bar]:
    """A deterministic synthetic series. Seeded, so every test sees one series."""
    rng = random.Random(seed)
    price = 1.10
    step = timedelta(seconds=seconds_of(timeframe))
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
                timeframe=timeframe,
                bar_time=start + i * step,
                open=Decimal(f"{open_:.5f}"),
                high=Decimal(f"{high:.5f}"),
                low=Decimal(f"{low:.5f}"),
                close=Decimal(f"{price:.5f}"),
                volume=Decimal(rng.randint(100, 900)) if volume else None,
                spread=SPREAD if spread else None,
                spread_availability=(
                    Availability.available if spread else Availability.not_available
                ),
            )
        )
    return out


def make_config(**over: object) -> DatasetConfig:
    fields: dict[str, object] = {
        "key": "eurusd-h1",
        "symbol": "EURUSD",
        "timeframe": Timeframe.H1,
        "provider": Provider.mt5,
        "label_config": label_engine.LabelConfig(spread_points=SPREAD, horizon=12),
    }
    fields.update(over)
    return DatasetConfig(**fields)  # type: ignore[arg-type]


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
        session.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        await session.flush()
        yield session
    await engine.dispose()


# ============================================= 1. the registry and its units


def test_no_feature_is_a_raw_price_level() -> None:
    """The metals-points error, refused at the catalogue.

    Pooling price-scaled quantities across symbols is what turned a +4,236
    headline into an arithmetic error. A model trained on `sma_20` learns the
    price of the instrument, so no such feature is offered at all.
    """
    for spec in feature_engine.CATALOGUE.values():
        assert spec.unit in {"ratio", "oscillator", "category"}, spec.name
        assert "price" != spec.unit


def test_every_feature_declares_its_lookback_and_timestamp_policy() -> None:
    for spec in feature_engine.CATALOGUE.values():
        assert spec.lookback >= 0
        assert spec.formula
        assert spec.inputs
        assert spec.timestamp_policy == "at_or_before_T"


def test_an_unknown_feature_is_refused_rather_than_skipped() -> None:
    with pytest.raises(feature_engine.FeatureError) as exc:
        feature_engine.spec_for("moon_phase_14")
    assert "moon_phase_14" in str(exc.value)


def test_the_warmup_is_the_longest_lookback_asked_for() -> None:
    assert feature_engine.warmup_for(("return_1",)) == 1
    assert feature_engine.warmup_for(("return_1", "ema_spread_10_50")) == 50


# ================================================== 2. causal feature timing


def test_a_feature_at_T_does_not_change_when_the_future_arrives() -> None:
    """Section 56, run literally.

    Compute features over the first two thirds of a series, append the rest,
    recompute, and require every row in the overlap to be identical. A rolling
    window that reached forward, a normalisation over the whole array or a
    centred moving average would all move a value here, and nothing else in the
    system would notice.
    """
    bars = make_bars(300)
    cut = 200
    prefix = feature_engine.compute_features(bars[:cut])
    whole = feature_engine.compute_features(bars)
    for index in range(cut):
        assert prefix[index] == whole[index], f"row {index} moved when the future arrived"


def test_the_leakage_check_reports_a_feature_that_reads_forward() -> None:
    """A deliberately non-causal feature must FAIL the check.

    Without this, a passing `no_future_influence` would only prove that the
    check runs — not that it can tell the difference.
    """
    bars = make_bars(60)

    def peeking(window: list[Bar]) -> list[dict[str, float | None]]:
        # The classic mistake: the "current" value taken from the next bar.
        closes = [float(b.close) for b in window]
        return [
            {"next_close": closes[i + 1] if i + 1 < len(closes) else None}
            for i in range(len(window))
        ]

    finding = leakage.no_future_influence(bars, peeking)
    assert finding.passed is False
    assert "read the future" in finding.detail


def test_a_series_too_short_to_test_fails_rather_than_passes() -> None:
    finding = leakage.no_future_influence(make_bars(3), feature_engine.compute_features)
    assert finding.passed is False
    assert "too few" in finding.detail


def test_an_unwarmed_feature_is_none_and_never_zero() -> None:
    rows = feature_engine.compute_features(make_bars(60), ("ema_spread_10_50",))
    assert rows[0]["ema_spread_10_50"] is None
    assert rows[10]["ema_spread_10_50"] is None
    assert rows[59]["ema_spread_10_50"] is not None


def test_a_missing_volume_makes_a_volume_feature_absent_not_zero() -> None:
    """An unrecorded volume is not a quiet market."""
    rows = feature_engine.compute_features(
        make_bars(60, volume=False), ("relative_volume_20", "volume_change_1")
    )
    assert all(row["relative_volume_20"] is None for row in rows)
    assert all(row["volume_change_1"] is None for row in rows)


def test_the_rollover_hour_feature_marks_the_hour_the_project_measured() -> None:
    """21:00 UTC is server 00:00 at UTC+3 — the one hour whose quote bands
    measurably do not fit inside a bracket."""
    bars = make_bars(30, start=datetime(2026, 1, 1, 19, 0))
    rows = feature_engine.compute_features(bars, ("is_rollover_hour", "hour_utc"))
    flagged = [r for r in rows if r["is_rollover_hour"] == 1.0]
    assert flagged
    assert all(r["hour_utc"] == 21.0 for r in flagged)


def test_features_and_bars_are_the_same_length_so_alignment_is_by_index() -> None:
    bars = make_bars(120)
    assert len(feature_engine.compute_features(bars)) == len(bars)
    assert len(label_engine.compute_labels(bars, label_engine.LabelConfig(SPREAD, 5))) == len(bars)


# ============================================================ 3. the labels


def test_a_label_at_the_end_of_the_series_is_absent_not_fabricated() -> None:
    bars = make_bars(60)
    rows = label_engine.compute_labels(bars, label_engine.LabelConfig(SPREAD, horizon=10))
    for row in rows[-10:]:
        assert row.forward_return is None
        assert row.bracket_outcome is None


def test_a_label_reads_the_future_and_a_feature_does_not() -> None:
    """Section 57: the separation, checked on one timestamp.

    The label at T changes when the bars after T change. The feature at T does
    not. That asymmetry is the whole design.
    """
    bars = make_bars(80)
    config = label_engine.LabelConfig(SPREAD, horizon=5)
    index = 40

    before_feature = feature_engine.compute_features(bars)[index]
    before_label = label_engine.compute_labels(bars, config)[index]

    moved = list(bars)
    for j in range(index + 1, index + 6):
        moved[j] = Bar(
            symbol=moved[j].symbol,
            provider=moved[j].provider,
            timeframe=moved[j].timeframe,
            bar_time=moved[j].bar_time,
            open=moved[j].open,
            high=moved[j].high * Decimal("1.05"),
            low=moved[j].low,
            close=moved[j].close * Decimal("1.05"),
            volume=moved[j].volume,
            spread=moved[j].spread,
            spread_availability=moved[j].spread_availability,
        )

    after_feature = feature_engine.compute_features(moved)[index]
    after_label = label_engine.compute_labels(moved, config)[index]

    assert after_feature == before_feature, "a feature moved when the FUTURE changed"
    assert after_label.forward_return != before_label.forward_return


def test_a_bar_containing_both_barriers_is_ambiguous_rather_than_guessed() -> None:
    """Bar data cannot order two prices inside one bar.

    Position 10200315596 in the live log is what guessing looks like: a
    282-point M1 range that put both exits on the wrong side of the fill.
    """
    outcome, offset = label_engine._walk(
        highs=[0.0, 200.0], lows=[0.0, 1.0], start=1, end=1, stop=50.0, target=150.0
    )
    assert outcome == str(label_engine.Outcome.ambiguous)
    assert offset == 1


def test_the_first_barrier_reached_wins_not_the_nearer_one() -> None:
    # Bar 1 hits only the stop; bar 2 would have hit the target.
    outcome, offset = label_engine._walk(
        highs=[0.0, 120.0, 400.0], lows=[0.0, 40.0, 90.0], start=1, end=2, stop=50.0, target=150.0
    )
    assert outcome == str(label_engine.Outcome.loss)
    assert offset == 1


def test_a_trade_still_open_at_the_horizon_is_a_timeout_not_a_loss() -> None:
    outcome, offset = label_engine._walk(
        highs=[0.0, 100.0], lows=[0.0, 90.0], start=1, end=1, stop=50.0, target=150.0
    )
    assert outcome == str(label_engine.Outcome.timeout)
    assert offset is None


def test_the_label_config_has_no_zero_cost_default() -> None:
    """A WIN computed without the spread is a label for a market nobody trades in."""
    with pytest.raises(TypeError):
        label_engine.LabelConfig()  # type: ignore[call-arg]


def test_an_impossible_label_configuration_is_refused() -> None:
    for kwargs in (
        {"horizon": 0},
        {"stop_atr": 0.0},
        {"take_profit_atr": -1.0},
        {"atr_period": 1},
        {"flat_threshold": -0.1},
    ):
        with pytest.raises(label_engine.LabelError):
            label_engine.LabelConfig(spread_points=SPREAD, **kwargs)


def test_a_negative_spread_is_refused() -> None:
    with pytest.raises(label_engine.LabelError):
        label_engine.LabelConfig(spread_points=Decimal("-1"))


def test_the_class_balance_is_reported_and_not_corrected() -> None:
    bars = make_bars(200)
    rows = label_engine.compute_labels(bars, label_engine.LabelConfig(SPREAD, horizon=8))
    report = label_engine.class_balance(rows, "direction")
    assert report["labelled"] + report["unlabelled"] == len(rows)
    assert "not corrected" in report["note"]
    assert abs(sum(report["shares"].values()) - 1.0) < 1e-9


# =========================================================== 4. resampling


def test_a_bucket_is_a_function_of_the_timestamp_not_the_position() -> None:
    """Two overlapping windows must produce the same H4 candle for the same hour."""
    bars = make_bars(48, timeframe=Timeframe.H1)
    whole = resampling.resample(bars, Timeframe.H4)
    offset = resampling.resample(bars[8:], Timeframe.H4)
    shared = {b.bar_time: b for b in whole}
    for candle in offset:
        if candle.bar_time in shared and candle.complete and shared[candle.bar_time].complete:
            assert candle.close == shared[candle.bar_time].close
            assert candle.high == shared[candle.bar_time].high


def test_ohlcv_aggregation_is_first_max_min_last_sum() -> None:
    bars = make_bars(8, timeframe=Timeframe.H1, start=datetime(2026, 1, 1, 0, 0))
    candles = resampling.resample(bars, Timeframe.H4)
    first = candles[0]
    group = bars[:4]
    assert first.open == group[0].open
    assert first.high == max(b.high for b in group)
    assert first.low == min(b.low for b in group)
    assert first.close == group[-1].close
    assert first.volume == sum(b.volume for b in group)  # type: ignore[misc]


def test_a_short_bucket_is_marked_incomplete_rather_than_padded() -> None:
    bars = make_bars(6, timeframe=Timeframe.H1, start=datetime(2026, 1, 1, 0, 0))
    candles = resampling.resample(bars, Timeframe.H4)
    assert candles[0].complete is True
    assert candles[1].complete is False


def test_a_missing_volume_makes_the_aggregate_volume_absent() -> None:
    bars = make_bars(4, timeframe=Timeframe.H1, volume=False)
    assert resampling.resample(bars, Timeframe.H4)[0].volume is None


def test_mixed_providers_are_refused_rather_than_merged() -> None:
    bars = make_bars(8)
    other = bars[4]
    bars[4] = Bar(
        symbol=other.symbol,
        provider=Provider.yfinance,
        timeframe=other.timeframe,
        bar_time=other.bar_time,
        open=other.open,
        high=other.high,
        low=other.low,
        close=other.close,
    )
    with pytest.raises(resampling.ResampleError) as exc:
        resampling.resample(bars, Timeframe.H4)
    assert "different books" in str(exc.value)


def test_an_impossible_aggregation_is_refused() -> None:
    with pytest.raises(resampling.ResampleError) as exc:
        resampling.resample(make_bars(4, timeframe=Timeframe.H1), Timeframe.M5)
    assert "cannot be aggregated" in str(exc.value)


# ======================================================= 5. quality and gaps


def test_an_invalid_candle_blocks_the_series() -> None:
    bars = make_bars(40)
    broken = bars[10]
    bars[10] = Bar(
        symbol=broken.symbol,
        provider=broken.provider,
        timeframe=broken.timeframe,
        bar_time=broken.bar_time,
        open=broken.open,
        high=broken.low,  # high below low: not a candle at all
        low=broken.high,
        close=broken.close,
    )
    report = quality.assess(bars, Timeframe.H1)
    assert report.usable is False
    assert any("not candles at all" in b for b in report.blocking)


def test_out_of_order_bars_are_refused_rather_than_sorted() -> None:
    bars = make_bars(40)
    bars[5], bars[6] = bars[6], bars[5]
    report = quality.assess(bars, Timeframe.H1)
    assert report.usable is False
    assert any("out of order" in b for b in report.blocking)


def test_a_gap_is_a_warning_and_not_a_block() -> None:
    """A closed market is a fact about the series, not corruption."""
    bars = make_bars(40)
    del bars[20:23]
    report = quality.assess(bars, Timeframe.H1)
    assert report.usable is True
    assert any("missing slot" in w for w in report.warnings)
    assert report.completeness < 1.0


def test_an_empty_series_is_not_a_dataset() -> None:
    report = quality.assess([], Timeframe.H1)
    assert report.usable is False
    assert report.score == 0.0


def test_the_quality_score_is_reported_and_does_not_block() -> None:
    report = quality.assess(make_bars(60, spread=False), Timeframe.H1)
    assert report.usable is True
    assert report.score < 1.0
    assert "does not block" in report.as_dict()["policy"]


# ================================================ 6. splits and walk-forward


def test_a_chronological_split_never_mixes_past_and_future() -> None:
    times = [START + timedelta(hours=i) for i in range(100)]
    split = splits.chronological(times, train=0.6, validation=0.2)
    assert split.sizes() == {"train": 60, "validation": 20, "test": 20}
    assert times[split.train[1] - 1] < times[split.validation[0]]
    assert times[split.validation[1] - 1] < times[split.test[0]]


def test_a_split_with_no_holdout_is_refused() -> None:
    times = [START + timedelta(hours=i) for i in range(100)]
    with pytest.raises(splits.SplitError) as exc:
        splits.chronological(times, train=0.9, validation=0.1)
    assert "no test segment" in str(exc.value)


def test_unordered_rows_are_refused_rather_than_sorted() -> None:
    """A chronological split of unordered rows is a random split."""
    times = [START + timedelta(hours=i) for i in range(100)]
    times[10], times[11] = times[11], times[10]
    with pytest.raises(splits.SplitError) as exc:
        splits.chronological(times)
    assert "random split" in str(exc.value)


def test_the_split_output_warns_that_one_split_is_one_observation() -> None:
    times = [START + timedelta(hours=i) for i in range(100)]
    warning = splits.chronological(times).as_dict()["single_split_warning"]
    assert "walk-forward" in warning


def test_walk_forward_folds_expand_and_never_test_on_the_past() -> None:
    times = [START + timedelta(hours=i) for i in range(200)]
    folds = splits.walk_forward(times, folds=4, initial_train=0.4)
    assert len(folds) == 4
    for fold in folds:
        assert fold.train[0] == 0
        assert fold.train[1] == fold.test[0]
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert later.train[1] > earlier.train[1]
    assert folds[-1].test[1] == 200


def test_a_single_fold_is_refused_because_it_is_a_single_split() -> None:
    times = [START + timedelta(hours=i) for i in range(200)]
    with pytest.raises(splits.SplitError):
        splits.walk_forward(times, folds=1)


# ============================================================== 7. the scaler


def _rows(n: int = 100) -> list[dict[str, float | None]]:
    return [{"a": float(i), "b": float(i % 7)} for i in range(n)]


def test_a_scaler_cannot_be_fitted_on_anything_but_the_training_segment() -> None:
    """Section 58. The API has no spelling for the mistake.

    `fit` takes a split and reads `split.train`. Changing the rows AFTER the
    training boundary must not move a single parameter — if it does, something
    fitted on more than the training segment.
    """
    rows = _rows(100)
    split = splits.chronological([START + timedelta(hours=i) for i in range(100)])
    fitted = scaler.fit(rows, split, feature_version="1.0", features=("a", "b"))

    tampered = list(rows)
    for i in range(split.train[1], 100):
        tampered[i] = {"a": 1e9, "b": -1e9}
    again = scaler.fit(tampered, split, feature_version="1.0", features=("a", "b"))

    assert again.means == fitted.means
    assert again.deviations == fitted.deviations


def test_the_leakage_check_catches_a_scaler_fitted_on_the_whole_series() -> None:
    rows = _rows(100)
    split = splits.chronological([START + timedelta(hours=i) for i in range(100)])
    whole = splits.Split(train=(0, 100), validation=(0, 0), test=(0, 0), boundaries=(None, None))
    bad = scaler.fit(rows, whole, feature_version="1.0", features=("a", "b"))
    finding = leakage.scaler_fitted_on_train_only(bad, rows, split, features=("a", "b"))
    assert finding.passed is False
    assert "fitted on rows" in finding.detail


def test_a_constant_feature_is_passed_through_rather_than_divided_by_zero() -> None:
    rows: list[dict[str, float | None]] = [{"flat": 5.0} for _ in range(100)]
    split = splits.chronological([START + timedelta(hours=i) for i in range(100)])
    fitted = scaler.fit(rows, split, feature_version="1.0", features=("flat",))
    assert "flat" in fitted.constant_features
    assert fitted.transform(rows)[0]["flat"] == 5.0


def test_a_feature_with_no_usable_training_values_is_refused() -> None:
    rows: list[dict[str, float | None]] = [{"a": None} for _ in range(100)]
    split = splits.chronological([START + timedelta(hours=i) for i in range(100)])
    with pytest.raises(scaler.ScalerError) as exc:
        scaler.fit(rows, split, feature_version="1.0", features=("a",))
    assert "standard deviation needs at least two" in str(exc.value)


def test_transform_leaves_a_missing_value_missing() -> None:
    rows = _rows(100)
    split = splits.chronological([START + timedelta(hours=i) for i in range(100)])
    fitted = scaler.fit(rows, split, feature_version="1.0", features=("a", "b"))
    assert fitted.transform([{"a": None, "b": 3.0}])[0]["a"] is None


# ================================================== 8. leakage report wiring


def test_a_label_name_used_as_a_feature_fails_validation() -> None:
    finding = leakage.labels_are_not_features(("rsi_14", "forward_return"), ("forward_return",))
    assert finding.passed is False
    assert "forward_return" in finding.detail


def test_overlapping_splits_fail_validation() -> None:
    times = [START + timedelta(hours=i) for i in range(30)]
    overlapping = splits.Split(
        train=(0, 20), validation=(10, 25), test=(25, 30), boundaries=(None, None)
    )
    finding = leakage.splits_are_ordered(times, overlapping)
    assert finding.passed is False
    assert "holdout that is not held out" in finding.detail


def test_a_labelled_row_without_a_full_horizon_fails_validation() -> None:
    finding = leakage.labels_have_a_future(total_rows=100, labelled_indices=[97], horizon=10)
    assert finding.passed is False
    assert "do not exist" in finding.detail


def test_a_report_with_one_failure_does_not_pass() -> None:
    report = leakage.LeakageReport(
        [
            leakage.LeakageFinding("a", True, "fine"),
            leakage.LeakageFinding("b", False, "not fine"),
        ]
    )
    assert report.passed is False
    assert len(report.failures()) == 1
    assert "blocks the dataset at CLEAN" in report.as_dict()["consequence"]


# ============================================================ 9. the builder


def test_a_clean_series_builds_a_ready_dataset() -> None:
    dataset = build(make_bars(400), make_config(), now=datetime(2026, 3, 1))
    assert dataset.status is DatasetStatus.ready
    assert dataset.ready is True
    assert dataset.leakage.passed is True
    assert dataset.blocked_reason is None
    assert dataset.rows


def test_every_row_carries_features_from_before_it_and_labels_from_after() -> None:
    """Section 25's alignment, checked on the row rather than in the docstring."""
    bars = make_bars(200)
    config = make_config()
    dataset = build(bars, config, now=datetime(2026, 3, 1))
    by_time = {b.bar_time: i for i, b in enumerate(bars)}
    for row in dataset.rows[:5]:
        index = by_time[row.at]
        expected_features = feature_engine.compute_features(bars)[index]
        expected_labels = label_engine.compute_labels(bars, config.label_config)[index]
        assert row.features == expected_features
        assert row.labels == expected_labels.as_dict()


def test_cold_start_and_horizon_rows_are_dropped_and_counted() -> None:
    bars = make_bars(300)
    config = make_config()
    dataset = build(bars, config, now=datetime(2026, 3, 1))
    warmup = feature_engine.warmup_for(config.feature_names)
    assert dataset.dropped["cold_start"] == warmup
    assert dataset.dropped["no_future"] == config.label_config.horizon
    assert len(dataset.rows) + sum(dataset.dropped.values()) == len(bars)


def test_a_corrupt_series_is_blocked_at_raw_and_never_reaches_ready() -> None:
    bars = make_bars(200)
    broken = bars[10]
    bars[10] = Bar(
        symbol=broken.symbol,
        provider=broken.provider,
        timeframe=broken.timeframe,
        bar_time=broken.bar_time,
        open=broken.open,
        high=broken.low,
        low=broken.high,
        close=broken.close,
    )
    dataset = build(bars, make_config(), now=datetime(2026, 3, 1))
    assert dataset.status is DatasetStatus.raw
    assert dataset.ready is False
    assert dataset.rows == []
    assert dataset.blocked_reason


def test_too_few_rows_is_blocked_rather_than_padded() -> None:
    dataset = build(make_bars(60), make_config(), now=datetime(2026, 3, 1))
    assert dataset.ready is False
    assert "inventing rows" in (dataset.blocked_reason or "")


def test_a_dataset_with_no_features_or_no_labels_is_refused() -> None:
    with pytest.raises(BuildError):
        make_config(feature_names=())
    with pytest.raises(BuildError):
        make_config(label_names=())


def test_the_same_bars_and_configuration_rebuild_the_same_dataset() -> None:
    """Section 59. Reproducibility is why the rows are not stored."""
    bars = make_bars(300)
    config = make_config()
    first = build(bars, config, now=datetime(2026, 3, 1))
    second = build(bars, config, now=datetime(2026, 3, 1))
    assert first.fingerprint == second.fingerprint
    assert [r.as_dict() for r in first.rows] == [r.as_dict() for r in second.rows]


def test_different_bars_under_the_same_recipe_are_a_different_dataset() -> None:
    config = make_config()
    a = build(make_bars(300, seed=1), config, now=datetime(2026, 3, 1))
    b = build(make_bars(300, seed=2), config, now=datetime(2026, 3, 1))
    assert a.fingerprint != b.fingerprint


def test_a_different_horizon_is_a_different_dataset() -> None:
    bars = make_bars(300)
    a = build(bars, make_config(), now=datetime(2026, 3, 1))
    b = build(
        bars,
        make_config(label_config=label_engine.LabelConfig(SPREAD, horizon=6)),
        now=datetime(2026, 3, 1),
    )
    assert a.fingerprint != b.fingerprint


def test_the_manifest_carries_everything_needed_to_explain_the_dataset() -> None:
    manifest = build(make_bars(300), make_config(), now=datetime(2026, 3, 1)).manifest()
    for key in (
        "fingerprint",
        "config",
        "feature_set",
        "label_set",
        "quality",
        "split",
        "walk_forward",
        "scaler",
        "leakage",
        "alignment",
        "dropped",
    ):
        assert key in manifest, key
    assert manifest["feature_set"]["feature_set_version"] == feature_engine.FEATURE_SET_VERSION
    assert manifest["label_set"]["label_set_version"] == label_engine.LABEL_SET_VERSION


def test_the_class_balance_is_documented_and_not_rebalanced() -> None:
    dataset = build(make_bars(400), make_config(), now=datetime(2026, 3, 1))
    balances = class_balances(dataset)
    assert {b["label"] for b in balances} == {"direction", "bracket_outcome"}
    for balance in balances:
        assert "rebalancing a holdout would leak" in balance["note"]


# ============================================== 10. what the package may not do


def test_the_data_pipeline_cannot_reach_a_venue_or_a_model() -> None:
    """Sections 1 and 46: it produces data, and decides nothing.

    Parsed rather than asserted by convention, so an import added later has to
    delete this test to pass.
    """
    forbidden = {
        "app.brokers",
        "app.oms",
        "app.risk",
        "app.sizing",
        "app.positions",
        "MetaTrader5",
        "sklearn",
        "torch",
    }
    package = Path(__file__).resolve().parents[1] / "app" / "datasets"
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


def test_nothing_in_the_package_writes_to_the_raw_bar_table() -> None:
    """Section 10: raw data is never overwritten by cleaned values."""
    package = Path(__file__).resolve().parents[1] / "app" / "datasets"
    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for verb in ("db.add(MarketBar", "update(MarketBar", "delete(MarketBar"):
            assert verb not in source, f"{path.name} writes to market_bars"


# ============================================================ 11. persistence


async def test_a_built_dataset_is_recorded_with_its_verdict(db: AsyncSession) -> None:
    dataset = build(make_bars(400), make_config(), now=datetime(2026, 3, 1))
    record = await register(db, dataset)

    assert record.status == str(DatasetStatus.ready)
    assert record.fingerprint == dataset.fingerprint
    assert record.row_count == len(dataset.rows)
    assert record.symbol_id == "sym-eur"

    checks = list(
        (await db.scalars(select(DatasetCheck).where(DatasetCheck.dataset_id == record.id))).all()
    )
    assert {c.category for c in checks} == {"leakage", "quality", "split"}
    assert all(c.passed for c in checks)


async def test_registering_twice_updates_one_row_rather_than_making_two(
    db: AsyncSession,
) -> None:
    bars = make_bars(400)
    first = await register(db, build(bars, make_config(), now=datetime(2026, 3, 1)))
    second = await register(db, build(bars, make_config(), now=datetime(2026, 3, 1)))
    assert first.id == second.id
    rows = list((await db.scalars(select(DatasetRecord))).all())
    assert len(rows) == 1
    checks = list((await db.scalars(select(DatasetCheck))).all())
    assert len(checks) == len(second.manifest["leakage"]["findings"]) + 2


async def test_a_feature_set_version_is_written_once(db: AsyncSession) -> None:
    bars = make_bars(400)
    await register(db, build(bars, make_config(), now=datetime(2026, 3, 1)))
    await register(
        db, build(bars, make_config(key="second"), now=datetime(2026, 3, 1)), version="1"
    )
    assert len(list((await db.scalars(select(FeatureSet))).all())) == 1


async def test_two_horizons_are_two_label_sets(db: AsyncSession) -> None:
    bars = make_bars(400)
    await register(db, build(bars, make_config(), now=datetime(2026, 3, 1)))
    await register(
        db,
        build(
            bars,
            make_config(key="short", label_config=label_engine.LabelConfig(SPREAD, horizon=6)),
            now=datetime(2026, 3, 1),
        ),
    )
    rows = list((await db.scalars(select(LabelSet))).all())
    assert len(rows) == 2
    assert {r.horizon for r in rows} == {6, 12}


async def test_a_blocked_dataset_carries_no_fingerprint(db: AsyncSession) -> None:
    """A fingerprint on a blocked row would read as "this reproduces"."""
    dataset = build(make_bars(60), make_config(), now=datetime(2026, 3, 1))
    record = await register(db, dataset)
    assert record.status == str(DatasetStatus.clean)
    assert record.fingerprint is None
    assert record.row_count == 0
    assert summarise(record)["ready"] is False


async def test_building_from_an_empty_raw_layer_is_refused(db: AsyncSession) -> None:
    with pytest.raises(DatasetServiceError) as exc:
        await build_and_register(
            db,
            key="nothing",
            symbol="EURUSD",
            provider_symbol="EURUSD.R",
            provider=Provider.mt5,
            timeframe=Timeframe.H1,
            horizon=12,
            spread_points=SPREAD,
        )
    assert "no stored bars" in str(exc.value)


# ================================================ 12. end to end, from storage


async def test_the_whole_pipeline_runs_from_the_stored_raw_layer(db: AsyncSession) -> None:
    """Section 60: bars -> normalisation -> features -> labels -> dataset ->
    leakage validation -> READY, from what the platform actually stores."""
    for bar in make_bars(400):
        db.add(
            MarketBar(
                provider=str(bar.provider),
                provider_symbol="EURUSD.R",
                symbol_id="sym-eur",
                timeframe=str(bar.timeframe),
                bar_time=bar.bar_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                spread=bar.spread,
                spread_availability=str(bar.spread_availability),
                ingested_at=datetime(2026, 3, 1),
            )
        )
    await db.flush()

    loaded = await load_bars(
        db,
        provider=Provider.mt5,
        provider_symbol="EURUSD.R",
        timeframe=Timeframe.H1,
        symbol="EURUSD",
    )
    assert len(loaded) == 400
    assert loaded == sorted(loaded, key=lambda b: b.bar_time)

    dataset, record = await build_and_register(
        db,
        key="eurusd-h1-e2e",
        symbol="EURUSD",
        provider_symbol="EURUSD.R",
        provider=Provider.mt5,
        timeframe=Timeframe.H1,
        horizon=12,
        spread_points=SPREAD,
        now=datetime(2026, 3, 1),
    )

    assert dataset.ready is True
    assert record.status == "READY"
    assert record.row_count > 0
    row = summarise(record)
    assert row["leakage_passed"] is True
    assert row["leakage_failed_checks"] == 0
    assert row["feature_set_version"] == feature_engine.FEATURE_SET_VERSION


async def test_the_raw_bars_are_unchanged_by_building_a_dataset(db: AsyncSession) -> None:
    """The strongest form of "raw data is never overwritten": read it back."""
    for bar in make_bars(400):
        db.add(
            MarketBar(
                provider=str(bar.provider),
                provider_symbol="EURUSD.R",
                symbol_id="sym-eur",
                timeframe=str(bar.timeframe),
                bar_time=bar.bar_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                spread=bar.spread,
                spread_availability=str(bar.spread_availability),
                ingested_at=datetime(2026, 3, 1),
            )
        )
    await db.flush()
    before = [
        (r.bar_time, r.open, r.high, r.low, r.close, r.volume, r.spread)
        for r in (await db.scalars(select(MarketBar).order_by(MarketBar.bar_time))).all()
    ]

    await build_and_register(
        db,
        key="eurusd-h1-raw",
        symbol="EURUSD",
        provider_symbol="EURUSD.R",
        provider=Provider.mt5,
        timeframe=Timeframe.H1,
        horizon=12,
        spread_points=SPREAD,
        now=datetime(2026, 3, 1),
    )

    after = [
        (r.bar_time, r.open, r.high, r.low, r.close, r.volume, r.spread)
        for r in (await db.scalars(select(MarketBar).order_by(MarketBar.bar_time))).all()
    ]
    assert before == after


def test_live_trading_is_still_disabled_by_default() -> None:
    """Section 61.15. This level builds data and enables nothing."""
    from app.core.settings import LIVE_GATES, Settings, TradingMode

    settings = Settings(_env_file=None)
    assert settings.live_trading is False
    assert settings.trading_mode is TradingMode.paper
    assert all(value is False for value in LIVE_GATES.values())


def test_label_config_refuses_a_spread_in_points_not_price() -> None:
    """The fence that would have caught a whole wasted training round.

    `spread_points` is subtracted straight from a PRICE in `compute_labels`
    (`net_exit = closes[end] - spread`), so the value it wants is a price:
    EURUSD's two points is 0.00002, not 2.0. A caller who reads the field
    name and passes 2.0 gets `1.17 - 2.0 = -0.83` and a forward return of
    -1.71 on EVERY row.

    Measured 2026-09-23 before the fence existed: 4,926 rows, ZERO positive,
    mean -1.720. It built cleanly, passed every leakage check, reached READY,
    trained a model, and produced an economic report of "profit factor 0.0,
    win rate 0.0" across 669 trades -- which read as a devastating result and
    was arithmetic. Nothing in the pipeline objected.
    """
    # The mistake itself.
    with pytest.raises(label_engine.LabelError) as caught:
        label_engine.LabelConfig(spread_points=Decimal("2.0"))
    message = str(caught.value)
    # A refusal that does not name the cause sends the reader to the wrong
    # place; this one has to say which unit is wanted.
    assert "PRICE" in message
    assert "0.00002" in message

    # A real price is still accepted, including a wide one, and zero stays
    # legal -- a caller may deliberately price a frictionless label for
    # comparison, and the config's job is units, not policy.
    for ok in (Decimal("0.00002"), Decimal("0.05"), Decimal("0")):
        assert label_engine.LabelConfig(spread_points=ok).spread_points == ok

    # And the labels it produces are sane rather than uniformly negative.
    bars = make_bars(120)
    rows = label_engine.compute_labels(
        bars, label_engine.LabelConfig(spread_points=Decimal("0.00002"), horizon=5)
    )
    returns = [r.forward_return for r in rows if r.forward_return is not None]
    assert returns, "the fixture produced no labelled rows"
    assert any(r > 0 for r in returns), "no winners at all is the -1.71 signature"
    assert all(abs(r) < 0.5 for r in returns), "a bar return near 100% is a unit error"
