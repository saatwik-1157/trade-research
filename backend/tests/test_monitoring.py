"""Model monitoring: drift, calibration, escalation, and what it may never do."""

from __future__ import annotations

import os
import random
import sys
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from app.db.base import Base
from app.models.ops import Notification
from app.monitoring import checks as C
from app.monitoring import stats
from app.monitoring.escalation import LADDER, build_alerts, recommended_actions
from app.monitoring.findings import (
    FORBIDDEN_ACTIONS,
    Action,
    Check,
    Finding,
    Severity,
    worst,
)
from app.monitoring.monitor import ModelMonitor, MonitoringInputs
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

AT = datetime(2026, 9, 2, 12, 0, 0)


def normals(n: int, mean: float = 0.0, sd: float = 1.0, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


# ------------------------------------------------------------------- statistics


def test_bh_matches_the_research_toolkit_implementation() -> None:
    """The backend restates BH-FDR; this locks it to the original.

    `tools/patterns.py` has had this procedure since the pattern study. Two
    copies that disagree would be worse than one, so the copies are tested
    against each other rather than trusted.
    """
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools"
        ),
    )
    from patterns import benjamini_hochberg as original

    rng = random.Random(7)
    for _ in range(50):
        pvalues = [rng.random() for _ in range(rng.randint(1, 25))]
        assert stats.benjamini_hochberg(pvalues, 0.05) == original(pvalues, 0.05)
    assert stats.benjamini_hochberg([], 0.05) == original([], 0.05)


def test_psi_is_zero_for_the_same_sample_and_grows_with_a_shift() -> None:
    base = normals(1000, seed=1)
    assert stats.population_stability_index(base, base) == pytest.approx(0.0, abs=1e-9)
    small = stats.population_stability_index(base, normals(1000, mean=0.2, seed=2))
    large = stats.population_stability_index(base, normals(1000, mean=2.0, seed=3))
    assert 0 < small < large
    assert large > stats.PSI_SERIOUS


def test_psi_notices_a_region_the_current_sample_abandoned() -> None:
    base = normals(1000, seed=4)
    truncated = [x for x in base if x > 0]  # the whole left tail is gone
    assert stats.population_stability_index(base, truncated) > stats.PSI_SERIOUS


def test_ks_separates_shifted_from_identical_distributions() -> None:
    a, b = normals(500, seed=5), normals(500, seed=6)
    d_same, p_same = stats.ks_statistic(a, b)
    d_shift, p_shift = stats.ks_statistic(a, normals(500, mean=1.0, seed=7))
    assert p_same > 0.05 and d_same < d_shift
    assert p_shift < 0.01


def test_quantile_edges_survive_a_degenerate_reference() -> None:
    assert stats.population_stability_index([1.0] * 200, [1.0] * 200) == 0.0
    with pytest.raises(ValueError):
        stats.quantile_edges([1.0, 2.0], bins=1)


def test_calibration_error_separates_calibrated_from_overconfident() -> None:
    rng = random.Random(11)
    probs, good, bad = [], [], []
    for _ in range(2000):
        p = rng.random()
        probs.append(p)
        good.append(1 if rng.random() < p else 0)
        bad.append(1 if rng.random() < p * 0.4 else 0)  # claims far more than it delivers
    assert stats.expected_calibration_error(probs, good) < 0.05
    assert stats.expected_calibration_error(probs, bad) > 0.15
    assert stats.brier_score(probs, bad) > stats.brier_score(probs, good)


def test_reliability_bins_report_their_sample_size() -> None:
    rows = stats.reliability_bins([0.05, 0.06, 0.95], [0, 0, 1], bins=10)
    assert rows[0]["n"] == 2 and rows[-1]["n"] == 1


# ----------------------------------------------------------------- refusals


def test_every_check_refuses_a_sample_too_small_to_mean_anything() -> None:
    tiny = normals(20, seed=8)
    findings = [
        C.distribution_drift(Check.feature_drift, "rsi", tiny, tiny),
        C.prediction_drift(tiny, tiny),
        C.prediction_distribution(tiny),
        C.calibration([0.5] * 20, [1] * 20),
        C.performance(tiny, tiny),
        C.market_regime(["trend"] * 20, ["range"] * 20),
    ]
    for finding in findings:
        assert finding.severity is Severity.insufficient_data, finding.check
        assert "below the" in finding.summary
        # Insufficient data is never a reason to act.
        assert not finding.actionable


def test_insufficient_data_is_not_the_same_as_ok() -> None:
    assert Severity.insufficient_data is not Severity.ok
    assert worst([Severity.insufficient_data, Severity.ok]) is Severity.ok


# -------------------------------------------------------------------- checks


def test_feature_drift_applies_fdr_across_features() -> None:
    # Twenty stable features and one genuinely shifted.
    reference = {f"f{i}": normals(300, seed=100 + i) for i in range(20)}
    current = {f"f{i}": normals(300, seed=200 + i) for i in range(20)}
    current["f0"] = normals(300, mean=2.0, seed=999)

    findings = C.feature_drift(reference, current)
    by_name = {f.subject: f for f in findings}
    assert by_name["f0"].severity in (Severity.warning, Severity.serious)
    assert by_name["f0"].survives_fdr is True
    assert "survives FDR" in by_name["f0"].summary

    # Nothing else should be left flagged after correction.
    flagged = [f.subject for f in findings if f.actionable]
    assert flagged == ["f0"]


def test_a_feature_that_fails_fdr_is_demoted_and_says_so() -> None:
    # One marginal feature among many: correction should stand it down.
    reference = {f"f{i}": normals(200, seed=300 + i) for i in range(15)}
    current = {f"f{i}": normals(200, seed=400 + i) for i in range(15)}
    current["f3"] = normals(200, mean=0.22, seed=555)
    findings = C.feature_drift(reference, current)
    marginal = next(f for f in findings if f.subject == "f3")
    if marginal.survives_fdr is False:
        assert marginal.severity is Severity.ok
        assert "does not survive FDR" in marginal.summary


def test_prediction_distribution_catches_a_collapsed_model() -> None:
    constant = C.prediction_distribution([0.5] * 200)
    assert constant.severity is Severity.critical
    assert "identical" in constant.summary

    nearly = C.prediction_distribution([0.5 + i * 1e-6 for i in range(200)])
    assert nearly.severity is Severity.serious

    varied = C.prediction_distribution(normals(200, mean=0.5, sd=0.2, seed=12))
    assert varied.severity is Severity.ok


def test_performance_flags_a_fall_but_not_an_improvement() -> None:
    reference = normals(300, mean=0.10, sd=0.5, seed=13)
    worse = normals(300, mean=-0.30, sd=0.5, seed=14)
    better = normals(300, mean=0.50, sd=0.5, seed=15)

    fell = C.performance(reference, worse)
    assert fell.severity in (Severity.warning, Severity.serious)
    assert "mean R" in fell.summary

    rose = C.performance(reference, better)
    assert rose.severity is Severity.ok


def test_trade_outcomes_surfaces_a_losing_run_on_a_smaller_sample() -> None:
    # Deliberately a lower floor: a run of ten losses is an operational fact,
    # not a significance claim.
    trades = [0.5] * 25 + [-1.0] * 10
    finding = C.trade_outcomes(trades, consecutive_loss_limit=10)
    assert finding.severity is Severity.warning
    assert "longest losing run 10" in finding.summary
    assert C.trade_outcomes([0.1] * 35).severity is Severity.ok


def test_regime_mix_drift_is_measured_on_labels() -> None:
    reference = ["trend"] * 150 + ["range"] * 150
    same = ["trend"] * 150 + ["range"] * 150
    shifted = ["range"] * 290 + ["trend"] * 10
    assert C.market_regime(reference, same).severity is Severity.ok
    assert C.market_regime(reference, shifted).severity is Severity.serious


# ---------------------------------------------------------------- escalation


def test_the_ladder_runs_warning_to_validate() -> None:
    assert LADDER[Severity.warning] == (Action.warn,)
    assert Action.review in LADDER[Severity.serious]
    assert LADDER[Severity.critical][-1] is Action.validate
    assert LADDER[Severity.ok] == () and LADDER[Severity.insufficient_data] == ()


def test_a_serious_model_check_escalates_to_retrain_and_validate() -> None:
    model_finding = Finding(
        check=Check.calibration, subject="probabilities", severity=Severity.serious, summary="x"
    )
    market_finding = Finding(
        check=Check.market_regime, subject="regime", severity=Severity.serious, summary="x"
    )
    assert Action.retrain in recommended_actions([model_finding])
    assert Action.validate in recommended_actions([model_finding])
    # A regime shift is the market moving, not the model breaking.
    assert Action.retrain not in recommended_actions([market_finding])


def test_monitoring_can_never_recommend_replacing_a_live_model() -> None:
    every_severity = [
        Finding(check=check, subject="s", severity=severity, summary="x")
        for check in Check
        for severity in Severity
    ]
    actions = {str(a) for a in recommended_actions(every_severity)}
    assert not actions & FORBIDDEN_ACTIONS
    # And no such action exists to be emitted in the first place.
    assert not {str(a) for a in Action} & FORBIDDEN_ACTIONS


def test_alerts_are_grouped_by_severity_and_say_nothing_is_replaced() -> None:
    findings = [
        Finding(check=Check.feature_drift, subject="a", severity=Severity.warning, summary="x"),
        Finding(check=Check.calibration, subject="b", severity=Severity.critical, summary="y"),
        Finding(check=Check.performance, subject="c", severity=Severity.ok, summary="z"),
    ]
    alerts = build_alerts(findings, AT)
    assert [a.severity for a in alerts] == [Severity.critical, Severity.warning]
    assert alerts[0].action is Action.validate
    for alert in alerts:
        assert "No model is promoted, deployed or replaced" in alert.body


def test_no_actionable_findings_raises_no_alerts() -> None:
    findings = [
        Finding(check=Check.feature_drift, subject="a", severity=Severity.ok, summary="x"),
        Finding(
            check=Check.performance,
            subject="b",
            severity=Severity.insufficient_data,
            summary="y",
        ),
    ]
    assert build_alerts(findings, AT) == []


# ------------------------------------------------------------------- monitor


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def test_with_no_model_the_monitor_says_so_rather_than_reporting_health(
    db: AsyncSession,
) -> None:
    report = await ModelMonitor(db).run(MonitoringInputs(), at=AT)
    assert report.model_version_id is None
    assert report.findings == () and report.alerts == ()
    assert report.note is not None
    assert "nothing to monitor" in report.note
    assert "not a clean bill of health" in report.note


async def test_a_full_run_persists_alerts_without_secrets(db: AsyncSession) -> None:
    inputs = MonitoringInputs(
        reference_features={"rsi": normals(300, seed=20)},
        current_features={"rsi": normals(300, mean=2.0, seed=21)},
        reference_predictions=normals(300, mean=0.5, sd=0.1, seed=22),
        current_predictions=[0.5] * 300,
        reference_r=normals(300, mean=0.1, sd=0.5, seed=23),
        current_r=normals(300, mean=-0.4, sd=0.5, seed=24),
    )
    report = await ModelMonitor(db).run(inputs, model_version_id="mv1", at=AT)

    kinds = {str(f.check) for f in report.findings}
    assert {"feature_drift", "prediction_drift", "prediction_distribution", "performance"} <= kinds
    assert report.severity is Severity.critical  # the collapsed prediction distribution
    assert report.alerts

    rows = list((await db.scalars(select(Notification))).all())
    assert rows and all(r.event_type == "model_monitoring" for r in rows)
    # L34 gave the platform one severity vocabulary; these rows now speak it.
    assert {r.severity for r in rows} <= {"WARNING", "ERROR", "CRITICAL"}
    assert all(r.category == "MONITORING" for r in rows)
    payload = rows[0].payload or {}
    assert payload["model_version_id"] == "mv1"
    assert str(payload["action"]) not in FORBIDDEN_ACTIONS


async def test_absent_inputs_skip_their_checks_instead_of_defaulting() -> None:
    monitor = ModelMonitor(None)
    findings = monitor.evaluate(MonitoringInputs())
    assert findings == []
    only_outcomes = monitor.evaluate(MonitoringInputs(current_r=[0.1] * 50))
    # `possible_concept_drift` joins any run that produced a finding at all: it
    # READS the others rather than needing an input of its own, and on this run
    # it reports insufficient_data because none of the four it looks at ran.
    # L29 added it, and it is the one check whose absence would be a silence.
    assert {str(f.check) for f in only_outcomes} == {
        "trade_outcomes",
        "possible_concept_drift",
    }
    concept = next(f for f in only_outcomes if str(f.check) == "possible_concept_drift")
    assert str(concept.severity) == "insufficient_data"
