"""Reading what actually happened, so the monitor has something real to measure.

This is the piece that was missing. `app/monitoring/` has had the statistical
engine, the checks, the findings vocabulary and the escalation ladder since
before L24 — and `MonitoringInputs` was a dataclass a caller filled by hand, so
**the monitor had never run on a recorded inference.** Its own docstring said
so: *"There is no model in this platform yet."* That was true when it was
written. L27 and L28 made it false.

Every figure here is read from a table something else wrote:

  * `ai_decisions` (L27) — the probability, the confidence, the regime, the
    anomaly score, all three latencies, the status, and what happened to the
    signal afterwards.
  * `model_predictions` (L24) — every inference including the refusals, which is
    the denominator an answer rate needs.
  * `model_deployments` (L28) — which version was active, where, and when.
  * `trades` (L19) — the realised outcome.

**Nothing is computed here.** This module gathers and shapes; `checks.py` does
the statistics. The separation is what lets the checks be tested without a
database and the collection be tested without arithmetic.

**A missing input is absent, never zero.** Section 52. `MonitoringInputs` skips
a check whose data it does not have, and the check reports INSUFFICIENT_DATA —
which is a different claim from "fine", and the only one the evidence supports.

**Nothing here runs in the execution path.** Section 45: this reads rows that
were already written, after the fact. A monitoring query has never blocked an
order because it cannot: no execution code calls it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_integration import AiDecisionRecord
from app.models.ai_registry import ModelDeployment
from app.models.execution import Trade
from app.models.market import Symbol
from app.monitoring.config import Windows


def _naive(at: datetime) -> datetime:
    """The storage convention: naive UTC, as every table in this project uses."""
    return at.replace(tzinfo=None) if at.tzinfo else at


@dataclass
class Window:
    """One time range, named so a payload can say which one a figure came from."""

    name: str
    start: datetime
    end: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "hours": round((self.end - self.start).total_seconds() / 3600, 2),
        }


def windows_for(now: datetime, config: Windows) -> dict[str, Window]:
    end = _naive(now)
    return {
        "health": Window("health", end - config.health, end),
        "drift": Window("drift", end - config.drift, end),
        "performance": Window("performance", end - config.performance, end),
    }


@dataclass
class Collected:
    """What one run found in the tables. Shaped, not computed."""

    model_version_id: str
    model_key: str
    model_version: str | None
    strategy_key: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    environment: str = "paper"

    #: The health window: statuses and latencies, one entry per decision.
    statuses: list[str] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    feature_latencies_ms: list[float] = field(default_factory=list)

    #: The drift window: what the model said.
    probabilities: list[float] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    regimes: list[str] = field(default_factory=list)
    anomaly_scores: list[float] = field(default_factory=list)

    #: The performance window: what the model said, and what happened.
    outcome_probabilities: list[float] = field(default_factory=list)
    outcomes: list[int] = field(default_factory=list)
    r_multiples: list[float] = field(default_factory=list)

    #: Section 29: how the AI's own decisions were distributed, and what
    #: happened to the signals it accepted against the ones it rejected.
    decisions: dict[str, int] = field(default_factory=dict)
    final_outcomes: dict[str, int] = field(default_factory=dict)
    accepted_outcomes: dict[str, int] = field(default_factory=dict)
    rejected_outcomes: dict[str, int] = field(default_factory=dict)

    counts: dict[str, int] = field(default_factory=dict)
    windows: dict[str, Window] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version_id": self.model_version_id,
            "model": self.model_key,
            "version": self.model_version,
            "scope": {
                "strategy_key": self.strategy_key,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "environment": self.environment,
            },
            "counts": self.counts,
            "windows": {name: w.as_dict() for name, w in self.windows.items()},
            "decisions": dict(sorted(self.decisions.items())),
            "final_outcomes": dict(sorted(self.final_outcomes.items())),
            "source": (
                "ai_decisions (L27), model_predictions (L24), model_deployments (L28) "
                "and trades (L19). Nothing is computed here and nothing is invented: a "
                "missing input is absent rather than zero."
            ),
        }


async def _decisions(
    db: AsyncSession,
    *,
    model_version_id: str,
    window: Window,
    strategy_key: str | None,
    symbol: str | None,
) -> list[AiDecisionRecord]:
    statement = select(AiDecisionRecord).where(
        AiDecisionRecord.model_version_id == model_version_id,
        AiDecisionRecord.created_at >= window.start,
        AiDecisionRecord.created_at <= window.end,
    )
    if strategy_key:
        statement = statement.where(AiDecisionRecord.strategy_key == strategy_key)
    if symbol:
        statement = statement.where(AiDecisionRecord.symbol == symbol)
    return list((await db.scalars(statement.order_by(AiDecisionRecord.created_at))).all())


async def collect(
    db: AsyncSession,
    deployment: ModelDeployment,
    *,
    config: Windows,
    now: datetime | None = None,
) -> Collected:
    """Everything one monitoring run needs, from the tables that recorded it.

    Scoped to the deployment: a model active on one strategy and superseded on
    another must not have the two pooled, which is §17 and §18 applied at the
    point the data is read rather than at the point it is displayed.
    """
    now = now or datetime.now(UTC)
    bounds = windows_for(now, config)

    out = Collected(
        model_version_id=deployment.model_version_id,
        model_key=deployment.model_key,
        model_version=deployment.model_version_label,
        strategy_key=deployment.strategy_key,
        symbol=deployment.symbol,
        timeframe=deployment.timeframe,
        environment=deployment.environment,
        windows=bounds,
    )

    # --- health window: how the service behaved.
    health = await _decisions(
        db,
        model_version_id=deployment.model_version_id,
        window=bounds["health"],
        strategy_key=deployment.strategy_key,
        symbol=deployment.symbol,
    )
    for row in health:
        out.statuses.append(row.status)
        if row.total_latency_ms is not None:
            out.latencies_ms.append(float(row.total_latency_ms))
        if row.feature_latency_ms is not None:
            out.feature_latencies_ms.append(float(row.feature_latency_ms))

    # --- drift window: what the model said.
    drift = await _decisions(
        db,
        model_version_id=deployment.model_version_id,
        window=bounds["drift"],
        strategy_key=deployment.strategy_key,
        symbol=deployment.symbol,
    )
    for row in drift:
        out.decisions[row.decision] = out.decisions.get(row.decision, 0) + 1
        if row.final_outcome:
            out.final_outcomes[row.final_outcome] = out.final_outcomes.get(row.final_outcome, 0) + 1
            bucket = out.accepted_outcomes if row.decision == "ACCEPT" else out.rejected_outcomes
            bucket[row.final_outcome] = bucket.get(row.final_outcome, 0) + 1
        if row.probability is not None:
            out.probabilities.append(float(row.probability))
        if row.confidence is not None:
            out.confidences.append(float(row.confidence))
        if row.regime:
            out.regimes.append(row.regime)
        if row.anomaly_score is not None:
            out.anomaly_scores.append(float(row.anomaly_score))

    # --- performance window: what the model said, and what happened.
    #
    # An outcome is only usable where BOTH exist. A decision with no outcome is
    # not a loss and not a win; it is a trade that has not resolved, and
    # counting it either way would be the fabrication §52 forbids.
    performance = await _decisions(
        db,
        model_version_id=deployment.model_version_id,
        window=bounds["performance"],
        strategy_key=deployment.strategy_key,
        symbol=deployment.symbol,
    )
    for row in performance:
        if row.probability is None or not row.final_outcome:
            continue
        resolved = _outcome_of(row.final_outcome)
        if resolved is None:
            continue
        out.outcome_probabilities.append(float(row.probability))
        out.outcomes.append(resolved)

    out.r_multiples = await _r_multiples(db, deployment, bounds["performance"])

    out.counts = {
        "health_decisions": len(health),
        "drift_decisions": len(drift),
        "performance_decisions": len(performance),
        "probabilities": len(out.probabilities),
        "resolved_outcomes": len(out.outcomes),
        "trades": len(out.r_multiples),
        "latency_observations": len(out.latencies_ms),
    }
    return out


def _outcome_of(final_outcome: str) -> int | None:
    """A decision's eventual result as 0/1, or None when it did not resolve.

    Only `filled` produces an outcome, and only because a fill is where a trade
    begins. Every refusal — the AI's own, the risk engine's, sizing's — says
    nothing about whether the model was right, so it is excluded rather than
    scored as a loss. Counting a risk veto as a wrong prediction would make a
    conservative risk configuration look like a broken model.
    """
    if final_outcome == "filled":
        return 1
    return None


async def _r_multiples(
    db: AsyncSession, deployment: ModelDeployment, window: Window
) -> list[float]:
    """Realised outcomes from the TRADE JOURNAL, never a second trade history.

    Section 31. `trades` is L19's record of what a venue confirmed; monitoring
    reads it and does not keep its own. R multiples rather than currency,
    because currency cannot be pooled across positions sized differently —
    the correction `track_record.py` already applies and the reason
    `app/monitoring/checks.performance` measures in R.
    """
    statement = select(Trade).where(
        Trade.closed_at >= window.start,
        Trade.closed_at <= window.end,
        Trade.mode == deployment.environment,
    )
    if deployment.symbol:
        # `trades` keys the instrument by id, not by name. Resolved rather than
        # skipped: filtering on the wrong column would silently pool every
        # symbol, which is the aggregation §17 asks not to hide a problem in.
        symbol_id = await db.scalar(select(Symbol.id).where(Symbol.code == deployment.symbol))
        if symbol_id is None:
            return []
        statement = statement.where(Trade.symbol_id == symbol_id)
    rows = list((await db.scalars(statement)).all())

    out: list[float] = []
    for row in rows:
        r = getattr(row, "r_multiple", None)
        if r is not None:
            out.append(float(r))
    return out


async def active_deployments(
    db: AsyncSession, *, model_key: str | None = None, environment: str | None = None
) -> list[ModelDeployment]:
    """Every deployment monitoring should be looking at. Section 4.

    Read from L28's registry rather than from a status guess, so "which version
    was active" is the registry's answer and not a second one that could
    disagree with it.
    """
    statement = select(ModelDeployment).where(ModelDeployment.status == "active")
    if model_key:
        statement = statement.where(ModelDeployment.model_key == model_key)
    if environment:
        statement = statement.where(ModelDeployment.environment == environment)
    return list((await db.scalars(statement.order_by(ModelDeployment.activated_at))).all())


def split_window(values: list[Any], at: float = 0.5) -> tuple[list[Any], list[Any]]:
    """An earlier half and a later half, for a PREVIOUS_PERIOD baseline.

    Sequential, never shuffled: the whole point of comparing a window to the one
    before it is that they are adjacent in time, and a shuffle would compare a
    sample to itself.
    """
    cut = int(len(values) * at)
    return values[:cut], values[cut:]


def to_inputs(collected: Collected, baseline: Any, thresholds: Any) -> dict[str, Any]:
    """The shape `ModelMonitor.evaluate` takes, with only what exists filled in.

    Returned as a mapping rather than the dataclass so this module does not have
    to import the monitor — the dependency runs monitor -> collect, and a cycle
    between them is the L22 mistake repeated.
    """
    reference_predictions, current_predictions = split_window(collected.probabilities)
    reference_confidence, current_confidence = split_window(collected.confidences)
    reference_regimes, current_regimes = split_window(collected.regimes)
    reference_r, current_r = split_window(collected.r_multiples)

    return {
        "reference_predictions": reference_predictions or None,
        "current_predictions": current_predictions or None,
        "reference_confidence": reference_confidence or None,
        "current_confidence": current_confidence or None,
        "probabilities": collected.outcome_probabilities or None,
        "outcomes": collected.outcomes or None,
        "reference_r": reference_r or None,
        "current_r": current_r or None,
        "reference_regimes": reference_regimes or None,
        "current_regimes": current_regimes or None,
        "statuses": collected.statuses or None,
        "latencies_ms": collected.latencies_ms or None,
        "min_sample": thresholds.minimum_samples,
        "baseline": baseline,
    }
