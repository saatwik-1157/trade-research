"""AI model monitoring and drift detection (L29).

The ones that matter most, and they are the rules the brief calls strict:

  * `test_monitoring_never_replaces_retrains_or_promotes_a_model` — §26, §49.
  * `test_insufficient_data_never_becomes_a_healthy_state` — §13, §43.
  * `test_an_identical_condition_is_suppressed_inside_the_cooldown` — §24.
  * `test_a_condition_that_clears_produces_a_recovery` — §25.
  * `test_concept_drift_is_never_claimed_from_input_drift_alone` — §15.
  * `test_a_baseline_that_does_not_exist_is_not_a_baseline_of_zeros` — §5, §52.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.ai import registry_service as registry
from app.datasets import features as feature_engine
from app.db.base import Base
from app.models.ai import AIModel, ModelVersion
from app.models.ai_integration import AiDecisionRecord
from app.models.monitoring import ModelAlert, ModelMonitoringSnapshot
from app.models.validation import ValidationRun
from app.monitoring import alerts as alerting
from app.monitoring import baselines, checks, collect, health
from app.monitoring.config import (
    DEFAULT_WINDOWS,
    BaselineKind,
    HealthState,
    Thresholds,
    describe,
    worst_health,
)
from app.monitoring.findings import Check, Finding, Severity
from app.monitoring.service import MonitoringService
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 4, 12, 0)
FEATURES = ["rsi_14", "atr_pct_14", "ema_spread_10_50", "return_5"]
LOOSE = Thresholds(minimum_samples=20, minimum_outcomes=10, minimum_trades=5)


def artifact() -> dict[str, Any]:
    return {
        "kind": "logistic",
        "coefficients": {
            "features": FEATURES,
            "weights": [0.1, -0.2, 0.3, 0.05],
            "bias": 0.4,
            "fitted_rows": 500,
        },
    }


# ================================================================= fixtures


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        from app.auth.models import User

        session.add(User(id="u1", email="a@b.io", password_hash="x", role="admin"))
        await session.commit()
    yield factory
    await engine.dispose()


async def deployed_version(db: AsyncSession, *, verdict: str = "PASS"):  # noqa: ANN201
    """A model that has been through the whole L23-L28 chain."""
    model = AIModel(key="trade_probability", name="trade probability", kind="classifier")
    db.add(model)
    await db.flush()
    version = ModelVersion(
        model_id=model.id,
        version=1,
        artifact_ref="trade_probability:1.0",
        status="draft",
        features=FEATURES,
        feature_version=feature_engine.FEATURE_SET_VERSION,
        label_version="1.0",
        dataset_version="1",
        dataset_fingerprint="d" * 32,
        params={"artifact": artifact()},
        metrics={"classification": {"samples": 400, "accuracy": 0.55, "log_loss": 0.68}},
        test_start=datetime(2026, 4, 1),
        test_end=datetime(2026, 5, 1),
    )
    db.add(version)
    await db.flush()
    db.add(
        ValidationRun(
            model_version_id=version.id,
            status="completed",
            verdict=verdict,
            summary=f"{verdict} on all checks",
            report={
                "verdict": verdict,
                "checks": [
                    {"check": "discrimination", "evidence": {"auc": 0.64}},
                    {"check": "calibration", "evidence": {"expected_calibration_error": 0.06}},
                    {"check": "economic", "evidence": {"profit_factor": 1.3, "trades": 120}},
                ],
                "context": {"scored": {"rows": 400}},
            },
        )
    )
    await db.commit()
    await registry.register(db, version, actor_user_id="u1")
    deployment = await registry.deploy_to_paper(
        db, version, registry.Scope(), actor_user_id="u1", reason="paper run"
    )
    await db.refresh(version)
    return version, deployment


async def seed_decisions(
    db: AsyncSession,
    version: ModelVersion,
    *,
    n: int = 60,
    probability: float = 0.7,
    status: str = "OK",
    latency_ms: float = 12.0,
    at: datetime | None = None,
) -> None:
    """Real `ai_decisions` rows, the shape L27 writes."""
    base = at or (NOW - timedelta(minutes=30))
    for i in range(n):
        db.add(
            AiDecisionRecord(
                strategy_key="rsi_reversion",
                symbol="EURUSD",
                timeframe="H1",
                mode="AI_FILTER",
                policy="AI_OPTIONAL",
                decision="ACCEPT" if status == "OK" else "ERROR",
                status=status,
                model_version_id=version.id,
                model_key="trade_probability",
                model_version="1.0",
                probability=Decimal(str(probability)) if status == "OK" else None,
                confidence=Decimal(str(probability)) if status == "OK" else None,
                total_latency_ms=Decimal(str(latency_ms)),
                created_at=base + timedelta(seconds=i * 10),
                final_outcome="filled" if i % 3 == 0 else "risk_vetoed",
            )
        )
    await db.commit()


# ================================================== 1. the config and health


def test_health_precedence_puts_insufficient_data_above_healthy() -> None:
    """§43. Too little data must never become a false healthy state."""
    assert worst_health([HealthState.healthy, HealthState.insufficient_data]) is (
        HealthState.insufficient_data
    )
    assert worst_health([HealthState.warning, HealthState.insufficient_data]) is (
        HealthState.warning
    )
    assert worst_health([]) is HealthState.insufficient_data


def test_offline_outranks_every_other_state() -> None:
    """A model that is not serving cannot be healthy, degraded or anything else."""
    assert worst_health([HealthState.critical, HealthState.offline]) is HealthState.offline
    assert health.derive([], deployed=False) is HealthState.offline


def test_a_market_facing_warning_does_not_degrade_the_model() -> None:
    """§42. The inputs moving is the world moving, not a broken model."""
    market = Finding(
        check=Check.feature_distribution,
        subject="rsi_14",
        severity=Severity.serious,
        summary="PSI 0.31",
    )
    model = Finding(
        check=Check.calibration,
        subject="probabilities",
        severity=Severity.serious,
        summary="ECE 0.24",
    )
    assert health.state_for(market) is HealthState.warning
    assert health.state_for(model) is HealthState.degraded


def test_every_threshold_is_configurable_and_the_psi_bands_say_they_are_convention() -> None:
    """§41, and §42's caution about a number nobody measured."""
    strict = Thresholds(psi_warning=0.05, latency_p95_critical_ms=250.0)
    assert strict.psi_warning != Thresholds().psi_warning
    payload = describe()
    assert "CONVENTION" in payload["thresholds"]["psi_bands"]
    assert "not measured here" in payload["thresholds"]["psi_bands"]
    joined = " ".join(payload["does_not"])
    assert "retrain, promote, replace, deploy or roll back a model" in joined
    assert "convert insufficient data into a healthy state" in joined
    assert "claim concept drift from input drift alone" in joined
    assert "investigation recommended" in payload["statistical_caution"]


def test_different_metrics_get_different_windows() -> None:
    """§14. None is hardcoded, and they differ for a stated reason."""
    payload = DEFAULT_WINDOWS.as_dict()
    assert payload["health_hours"] < payload["drift_days"] * 24
    assert payload["drift_days"] < payload["performance_days"]
    assert "different amounts of evidence" in payload["why"]


# ================================================= 2. the checks L29 added


def test_latency_reports_the_p95_not_the_mean() -> None:
    """§19. A mean hides the tail, and the tail is what a strategy experiences."""
    values = [10.0] * 95 + [5000.0] * 5
    finding = checks.latency(values, p95_warning_ms=500.0, p95_critical_ms=2000.0)
    assert finding.severity is Severity.critical
    assert finding.detail["p50"] == 10.0
    assert finding.statistic is not None and finding.statistic >= 2000.0
    assert "never bypasses the risk engine" in finding.detail["note"]


def test_latency_on_too_few_observations_is_insufficient_data() -> None:
    assert checks.latency([10.0] * 5).severity is Severity.insufficient_data


def test_availability_excludes_disabled_from_the_denominator() -> None:
    """§20. A strategy that was never asked did not fail to answer."""
    finding = checks.availability(["OK"] * 90 + ["MODEL_UNAVAILABLE"] * 10 + ["DISABLED"] * 500)
    assert finding.statistic == pytest.approx(0.10)
    assert finding.detail["excluded_disabled"] == 500
    assert "perfect uptime" in finding.detail["note"]


def test_availability_names_which_failures_happened() -> None:
    finding = checks.availability(
        ["OK"] * 50 + ["MODEL_UNAVAILABLE"] * 10 + ["LATENCY_EXCEEDED"] * 5
    )
    assert finding.detail["by_status"] == {"LATENCY_EXCEEDED": 5, "MODEL_UNAVAILABLE": 10}


def test_confidence_drift_flags_for_investigation_not_as_a_conclusion() -> None:
    """§10. Do not automatically conclude the model is wrong."""
    reference = [0.5 + (i % 40) / 100 for i in range(200)]
    current = [0.97] * 200
    finding = checks.confidence_drift(reference, current, min_sample=50)
    assert finding.severity in (Severity.warning, Severity.serious)
    assert "reason to investigate, not a conclusion" in finding.summary
    assert finding.detail["current_extreme_share"] == 1.0


def test_concept_drift_is_never_claimed_from_input_drift_alone() -> None:
    """§15, the level's sharpest caution."""
    inputs_only = [
        Finding(Check.feature_drift, "rsi_14", Severity.serious, "PSI 0.4"),
        Finding(Check.calibration, "probabilities", Severity.ok, "ECE 0.03"),
    ]
    finding = checks.possible_concept_drift(inputs_only)
    assert finding.severity is Severity.ok
    assert "NOT concept drift" in finding.summary


def test_concept_drift_is_inferred_when_outcomes_moved_and_inputs_did_not() -> None:
    outcomes_only = [
        Finding(Check.feature_drift, "rsi_14", Severity.ok, "PSI 0.02"),
        Finding(Check.calibration, "probabilities", Severity.serious, "ECE 0.24"),
    ]
    finding = checks.possible_concept_drift(outcomes_only)
    assert finding.severity is Severity.warning
    assert "POSSIBLE concept drift" in finding.summary
    assert "investigation recommended, not a conclusion" in finding.summary


def test_concept_drift_with_nothing_measured_is_insufficient_data() -> None:
    unmeasured = [Finding(Check.feature_drift, "rsi_14", Severity.insufficient_data, "too few")]
    assert checks.possible_concept_drift(unmeasured).severity is Severity.insufficient_data


# ==================================================== 3. baselines (§5, §40)


async def test_the_validation_report_is_the_preferred_baseline(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        version, _ = await deployed_version(db)
        baseline = await baselines.resolve(db, version)

    assert baseline is not None
    assert baseline.kind is BaselineKind.validation
    assert baseline.metrics["auc"] == 0.64
    assert baseline.metrics["expected_calibration_error"] == 0.06
    assert "held-out measurement" in baseline.reason


async def test_a_baseline_that_does_not_exist_is_not_a_baseline_of_zeros(sessions) -> None:  # noqa: ANN001
    """§5 and §52."""
    async with sessions() as db:
        model = AIModel(key="bare", name="bare", kind="classifier")
        db.add(model)
        await db.flush()
        version = ModelVersion(
            model_id=model.id, version=1, artifact_ref="bare:1.0", status="draft", metrics=None
        )
        db.add(version)
        await db.commit()
        baseline = await baselines.resolve(db, version)

    assert baseline is None


async def test_the_training_record_is_a_weaker_baseline_and_says_so(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        model = AIModel(key="trained_only", name="x", kind="classifier")
        db.add(model)
        await db.flush()
        version = ModelVersion(
            model_id=model.id,
            version=1,
            artifact_ref="trained_only:1.0",
            status="draft",
            metrics={"classification": {"samples": 300, "accuracy": 0.54, "log_loss": 0.69}},
        )
        db.add(version)
        await db.commit()
        baseline = await baselines.resolve(db, version)

    assert baseline is not None
    assert baseline.kind is BaselineKind.training
    assert "Weaker" in baseline.reason


def test_a_metric_measured_on_only_one_side_is_not_a_difference_of_zero() -> None:
    baseline = baselines.Baseline(
        kind=BaselineKind.validation,
        baseline_id="v1",
        model_version_id="m1",
        metrics={"auc": 0.7},
    )
    out = baselines.compare(baseline, {"auc": 0.6, "brier": 0.2}, ("auc", "brier"))
    assert out["auc"]["delta"] == pytest.approx(-0.1)
    assert out["brier"]["delta"] is None
    assert "not measured on both sides" in out["brier"]["note"]


# ====================================================== 4. collection (§3)


async def test_collection_reads_recorded_decisions_and_computes_nothing(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        await seed_decisions(db, version, n=40)
        gathered = await collect.collect(db, deployment, config=DEFAULT_WINDOWS, now=NOW)

    assert gathered.counts["health_decisions"] == 40
    assert gathered.counts["probabilities"] == 40
    assert len(gathered.latencies_ms) == 40
    assert gathered.decisions == {"ACCEPT": 40}
    assert "Nothing is computed here" in gathered.as_dict()["source"]


async def test_collection_is_scoped_to_the_deployment(sessions) -> None:  # noqa: ANN001
    """§17 and §18: aggregation hides exactly the problems worth finding."""
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        await seed_decisions(db, version, n=20)
        db.add(
            AiDecisionRecord(
                strategy_key="other_strategy",
                symbol="GBPUSD",
                mode="AI_FILTER",
                policy="AI_OPTIONAL",
                decision="ACCEPT",
                status="OK",
                model_version_id=version.id,
                model_key="trade_probability",
                created_at=NOW - timedelta(minutes=5),
            )
        )
        await db.commit()

        deployment.strategy_key = "rsi_reversion"
        deployment.symbol = "EURUSD"
        gathered = await collect.collect(db, deployment, config=DEFAULT_WINDOWS, now=NOW)

    assert gathered.counts["health_decisions"] == 20  # the GBPUSD row is excluded


async def test_an_unresolved_decision_is_not_scored_as_a_loss(sessions) -> None:  # noqa: ANN001
    """A risk veto says nothing about whether the model was right."""
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        await seed_decisions(db, version, n=30)
        gathered = await collect.collect(db, deployment, config=DEFAULT_WINDOWS, now=NOW)

    # Only `filled` resolves; `risk_vetoed` is excluded rather than counted as 0.
    assert len(gathered.outcomes) == 10
    assert all(o == 1 for o in gathered.outcomes)


def test_a_window_split_is_sequential_never_shuffled() -> None:
    earlier, later = collect.split_window([1, 2, 3, 4, 5, 6])
    assert earlier == [1, 2, 3]
    assert later == [4, 5, 6]


# ================================================== 5. alerts (§22 to §25)


def test_a_severity_change_is_a_new_alert() -> None:
    """§24. Suppressing WARNING -> CRITICAL would hide what matters most."""
    common = {
        "model_version_id": "m1",
        "scope_key": "||",
        "check": "feature_drift",
        "subject": "rsi_14",
    }
    warning = alerting.fingerprint(**common, severity="warning")
    critical = alerting.fingerprint(**common, severity="critical")
    assert warning != critical


def test_the_same_condition_observed_twice_is_one_fingerprint() -> None:
    common = {
        "model_version_id": "m1",
        "scope_key": "||",
        "check": "feature_drift",
        "subject": "rsi_14",
        "severity": "warning",
    }
    assert alerting.fingerprint(**common) == alerting.fingerprint(**common)


def test_an_identical_condition_is_suppressed_inside_the_cooldown() -> None:
    """§24, and the reason: a monitor that repeats itself is muted."""
    candidate = alerting.Candidate(
        fingerprint="abc",
        model_version_id="m1",
        model_key="trade_probability",
        model_version="1.0",
        scope_key="||",
        check="feature_drift",
        subject="rsi_14",
        severity=Severity.warning,
        title="t",
        body="b",
    )
    decisions = alerting.deduplicate(
        [candidate],
        open_alerts={"abc": NOW - timedelta(minutes=5)},
        now=NOW,
        cooldown=timedelta(minutes=60),
    )
    assert decisions[0].notify is False
    assert "muted within a week" in decisions[0].reason


def test_a_condition_still_open_after_the_cooldown_is_said_again() -> None:
    candidate = alerting.Candidate(
        fingerprint="abc",
        model_version_id="m1",
        model_key="k",
        model_version="1.0",
        scope_key="||",
        check="feature_drift",
        subject="rsi_14",
        severity=Severity.warning,
        title="t",
        body="b",
    )
    decisions = alerting.deduplicate(
        [candidate],
        open_alerts={"abc": NOW - timedelta(hours=3)},
        now=NOW,
        cooldown=timedelta(minutes=60),
    )
    assert decisions[0].notify is True


def test_a_condition_that_clears_produces_a_recovery() -> None:
    """§25."""
    gone = alerting.recoveries(open_alerts={"a", "b"}, current=[])
    assert gone == {"a", "b"}


def test_insufficient_data_does_not_raise_an_alert() -> None:
    """Paging somebody because a window was quiet teaches them to ignore it."""
    findings = [
        Finding(Check.feature_drift, "rsi_14", Severity.insufficient_data, "too few"),
        Finding(Check.calibration, "probabilities", Severity.ok, "fine"),
    ]
    out = alerting.candidates_from(
        findings,
        model_version_id="m1",
        model_key="k",
        model_version="1.0",
        scope_key="||",
        baseline_id="v1",
    )
    assert out == []


def test_an_alert_carries_its_metric_value_threshold_and_sample_size() -> None:
    """§22. "Drift detected" and nothing else cannot be acted on."""
    findings = [
        Finding(
            check=Check.feature_drift,
            subject="rsi_14",
            severity=Severity.serious,
            summary="PSI 0.31",
            statistic=0.31,
            current_n=450,
            detail={"psi_serious": 0.25},
        )
    ]
    candidate = alerting.candidates_from(
        findings,
        model_version_id="m1",
        model_key="k",
        model_version="1.0",
        scope_key="||",
        baseline_id="v1",
    )[0]
    assert candidate.current_value == 0.31
    assert candidate.threshold == 0.25
    assert candidate.sample_size == 450
    assert candidate.detail is not None
    assert candidate.detail["baseline_id"] == "v1"


# ============================================== 6. the run, end to end (§48)


async def test_a_run_writes_a_reproducible_snapshot(sessions) -> None:  # noqa: ANN001
    """§34."""
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        await seed_decisions(db, version, n=60)
        service = MonitoringService(thresholds=LOOSE)
        result = await service.run_for(db, deployment, now=NOW)

    snapshot = result.snapshot
    assert snapshot is not None
    assert snapshot.model_version_id == version.id
    assert snapshot.model_version_label == "1.0"
    assert snapshot.window_end > snapshot.window_start
    assert snapshot.baseline_kind == "VALIDATION"
    assert snapshot.baseline_id
    assert snapshot.sample_count == 60
    assert snapshot.monitoring_engine_version
    assert snapshot.thresholds is not None
    assert snapshot.thresholds["minimum_samples"] == 20


async def test_insufficient_data_never_becomes_a_healthy_state(sessions) -> None:  # noqa: ANN001
    """§13 and §43, end to end: no decisions at all."""
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        service = MonitoringService(thresholds=LOOSE)
        result = await service.run_for(db, deployment, now=NOW)

    assert result.snapshot is not None
    assert result.snapshot.health_state == "INSUFFICIENT_DATA"
    assert result.snapshot.sample_count == 0


async def test_the_schema_refuses_a_healthy_snapshot_with_no_samples(sessions) -> None:  # noqa: ANN001
    """The gate is in the database, not only in the service."""
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        db.add(
            ModelMonitoringSnapshot(
                model_version_id=version.id,
                model_key="trade_probability",
                environment="paper",
                window_start=NOW - timedelta(days=1),
                window_end=NOW,
                sample_count=0,
                health_state="HEALTHY",
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_a_snapshot_must_state_its_window(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        version, _ = await deployed_version(db)
        db.add(
            ModelMonitoringSnapshot(
                model_version_id=version.id,
                model_key="trade_probability",
                environment="paper",
                window_start=NOW,
                window_end=NOW - timedelta(days=1),  # backwards
                sample_count=10,
                health_state="WARNING",
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_a_failing_service_raises_an_alert_once_then_suppresses_it(sessions) -> None:  # noqa: ANN001
    """§24, through the whole run."""
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        await seed_decisions(db, version, n=60, status="MODEL_UNAVAILABLE")
        service = MonitoringService(thresholds=LOOSE)

        first = await service.run_for(db, deployment, now=NOW)
        second = await service.run_for(db, deployment, now=NOW + timedelta(minutes=5))

        rows = list((await db.scalars(select(ModelAlert))).all())

    assert any(a["check"] == "availability" for a in first.raised)
    assert first.raised and not first.suppressed
    assert second.suppressed and not second.raised
    # One ROW, not two. A state machine, not a log.
    availability = [r for r in rows if r.check == "availability"]
    assert len(availability) == 1
    assert availability[0].occurrences == 2


async def test_a_condition_that_clears_is_resolved_and_announced(sessions) -> None:  # noqa: ANN001
    """§25, through the whole run."""
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        await seed_decisions(db, version, n=60, status="MODEL_UNAVAILABLE")
        service = MonitoringService(thresholds=LOOSE)
        await service.run_for(db, deployment, now=NOW)

        # The failures age out of the health window; healthy traffic replaces them.
        later = NOW + timedelta(hours=3)
        await seed_decisions(db, version, n=60, at=later - timedelta(minutes=10))
        result = await service.run_for(db, deployment, now=later)

        rows = list((await db.scalars(select(ModelAlert))).all())

    assert result.resolved
    resolved = [r for r in rows if r.status == "resolved"]
    assert resolved and all(r.resolved_at is not None for r in resolved)


async def test_the_schema_refuses_a_resolved_alert_with_no_time(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        version, _ = await deployed_version(db)
        db.add(
            ModelAlert(
                fingerprint="f1",
                model_version_id=version.id,
                model_key="trade_probability",
                check="latency",
                subject="total_ms",
                severity="warning",
                status="resolved",
                title="t",
                first_seen_at=NOW,
                last_seen_at=NOW,
                occurrences=1,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_only_one_open_alert_exists_per_fingerprint(sessions) -> None:  # noqa: ANN001
    """§24, as a database guarantee."""
    async with sessions() as db:
        version, _ = await deployed_version(db)
        for _ in range(2):
            db.add(
                ModelAlert(
                    fingerprint="same",
                    model_version_id=version.id,
                    model_key="trade_probability",
                    check="latency",
                    subject="total_ms",
                    severity="warning",
                    status="firing",
                    title="t",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    occurrences=1,
                )
            )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_nothing_deployed_is_offline_not_healthy(sessions) -> None:  # noqa: ANN001
    """§45 and §43: a statement about the registry, not a clean bill of health."""
    async with sessions() as db:
        service = MonitoringService(thresholds=LOOSE)
        results = await service.run_all(db)

    assert len(results) == 1
    assert results[0].snapshot is None
    assert results[0].as_dict()["health"] == "OFFLINE"
    assert results[0].note is not None
    assert "not a clean bill of health" in results[0].note


async def test_the_trading_block_labels_itself_an_observed_comparison(sessions) -> None:  # noqa: ANN001
    """§30. No causal claim from observational data."""
    async with sessions() as db:
        version, deployment = await deployed_version(db)
        await seed_decisions(db, version, n=60)
        service = MonitoringService(thresholds=LOOSE)
        result = await service.run_for(db, deployment, now=NOW)

    assert result.snapshot is not None
    trading = result.snapshot.trading_metrics
    assert trading is not None
    assert "OBSERVED COMPARISON" in str(trading["reading"])
    assert "No causal claim" in trading["reading"]
    assert trading["acceptance_rate"] == 1.0


# ============================================================== 7. safety


MONITORING_MODULES = sorted((Path(__file__).parent.parent / "app" / "monitoring").glob("*.py"))

FORBIDDEN = (
    "app.execution",
    "app.oms",
    "app.risk",
    "app.sizing",
    "app.brokers",
    "app.orders",
    "app.positions",
    "app.paper",
    "app.bots",
    "app.training",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def test_monitoring_never_replaces_retrains_or_promotes_a_model() -> None:
    """§26 and §54, parsed. The strict rule of the level."""
    assert MONITORING_MODULES
    for path in MONITORING_MODULES:
        for imported in _imports(path):
            for banned in FORBIDDEN:
                assert not imported.startswith(banned), f"{path.name} imports {imported}"
        # And no call by name to a lifecycle verb.
        for verb in ("promote", "rollback", "retire", "deploy_to_paper", "register"):
            assert verb not in _names(path), f"{path.name} calls {verb}"


def test_monitoring_cannot_enable_live_trading_or_change_a_limit() -> None:
    for path in MONITORING_MODULES:
        assert not _names(path) & {
            "LIVE_TRADING",
            "live_trading",
            "LIVE_GATES",
            "live_execution_allowed",
            "get_settings",
        }, path.name
        for imported in _imports(path):
            assert not imported.startswith("app.core.settings"), path.name


def test_the_forbidden_actions_are_still_forbidden() -> None:
    """The vocabulary a monitor may not emit, unchanged since the module was written."""
    from app.monitoring.findings import FORBIDDEN_ACTIONS, Action

    assert FORBIDDEN_ACTIONS == {"promote", "deploy", "replace_production_model", "rollback"}
    for action in Action:
        assert str(action) not in FORBIDDEN_ACTIONS


def test_the_escalation_ladder_tops_out_at_validate_not_deploy() -> None:
    """A monitor may conclude a model needs revalidating. Never that one goes live."""
    from app.monitoring.escalation import LADDER
    from app.monitoring.findings import Action
    from app.monitoring.findings import Severity as S

    top = LADDER[S.critical]
    assert Action.validate in top
    assert Action.retrain in top
    assert not any(str(a) in {"promote", "deploy"} for a in top)
