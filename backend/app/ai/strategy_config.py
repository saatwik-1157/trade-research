"""Reading and writing a strategy's AI configuration, and journalling what it said.

The database half of L27. `app/ai/integration.py` is a pure function of its
inputs — that is what makes it testable and deterministic — so everything that
touches a session lives here.

**A configuration is refused rather than repaired.** A row naming a model that
is not eligible, a mode that runs inference with no model, or a threshold out of
range raises. Section 41: a caller cannot supply a model path, a Python
expression or an unvalidated version, and the refusal says which.

**A missing configuration is AI_DISABLED**, and that is the only default in the
level. Section 7 makes it the right one: a strategy nobody has configured
behaves exactly as the deterministic strategy does, which is the baseline every
comparison needs.

**Every decision is journalled, including the ones that changed nothing.**
Section 35 and §45. An ADVISORY neutral leaves a row, a DISABLED run leaves a
row, and a failure leaves a row saying what failed. A decision that leaves no
trace is indistinguishable from an AI nobody asked, and the two have opposite
meanings when somebody later counts how often the layer answered.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import eligibility
from app.ai.decision import (
    AiDecision,
    AiIntegrationError,
    AiMode,
    AiPolicy,
    AiStrategyConfig,
    AiThresholds,
    ModelRequirement,
    ScoringMethod,
    SignalContext,
)
from app.models.ai_integration import AiDecisionRecord, AiStrategyConfiguration

log = logging.getLogger("app.ai.integration")


def _requirements(payload: Any, *, optional: bool) -> tuple[ModelRequirement, ...]:
    """`[{"key": ..., "version": ...}]` as requirements. Refuses anything else.

    Section 41: the stored value is data a user supplied, and a key or version
    that is not a plain string is refused rather than coerced. There is no path
    here by which a path, an expression or a callable becomes a model.
    """
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise AiIntegrationError(
            f"a model list must be a list of {{key, version}} objects, not {type(payload).__name__}"
        )
    out: list[ModelRequirement] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise AiIntegrationError("each model entry must be a {key, version} object")
        key, version = entry.get("key"), entry.get("version")
        if not isinstance(key, str) or not isinstance(version, str):
            raise AiIntegrationError(
                "a model entry names a key and a version, both strings. A value that is "
                "not a plain string is refused rather than coerced: this field is the "
                "one place a caller could try to name something other than a model."
            )
        out.append(ModelRequirement(key=key, version=version, optional=optional))
    return tuple(out)


def to_config(row: AiStrategyConfiguration) -> AiStrategyConfig:
    """One stored row as the dataclass the integration service takes."""
    if not row.enabled:
        # A configuration switched off is AI_DISABLED, not "the old settings
        # quietly still applying". Section 28: fallback is never implicit.
        return AiStrategyConfig(
            strategy_key=row.strategy_key,
            mode=AiMode.disabled,
            policy=AiPolicy.optional,
            notes="this configuration is switched off; the strategy runs deterministically",
        )

    thresholds = AiThresholds(
        minimum_probability=float(row.minimum_probability),
        maximum_anomaly_score=(
            float(row.maximum_anomaly_score) if row.maximum_anomaly_score is not None else None
        ),
        allowed_regimes=tuple(str(r) for r in (row.allowed_regimes or [])),
        minimum_expected_return=(
            float(row.minimum_expected_return) if row.minimum_expected_return is not None else None
        ),
        maximum_latency_ms=float(row.max_latency_ms),
        scoring_method=ScoringMethod(row.scoring_method),
        ai_weight=float(row.ai_weight),
        minimum_combined_score=float(row.minimum_combined_score),
    )
    return AiStrategyConfig(
        strategy_key=row.strategy_key,
        mode=AiMode(row.mode),
        policy=AiPolicy(row.policy),
        models=_requirements(row.required_models, optional=False)
        + _requirements(row.optional_models, optional=True),
        thresholds=thresholds,
        feature_version=row.feature_version or "",
        notes=row.notes or "",
    )


async def config_for(
    db: AsyncSession, strategy_key: str, *, account_id: str | None = None
) -> AiStrategyConfig:
    """The configuration for one strategy, or AI_DISABLED when there is none.

    An account-scoped row wins over the strategy default, so one strategy can
    run advisory on a demo account and disabled elsewhere without two strategy
    rows.
    """
    rows = list(
        (
            await db.scalars(
                select(AiStrategyConfiguration).where(
                    AiStrategyConfiguration.strategy_key == strategy_key
                )
            )
        ).all()
    )
    scoped = next((r for r in rows if r.account_id == account_id), None) if account_id else None
    default = next((r for r in rows if r.account_id is None), None)
    row = scoped or default
    if row is None:
        return AiStrategyConfig(
            strategy_key=strategy_key,
            mode=AiMode.disabled,
            policy=AiPolicy.optional,
            notes=(
                "no AI configuration exists for this strategy, so it runs exactly as the "
                "deterministic strategy does. That is the baseline, not a degraded mode."
            ),
        )
    return to_config(row)


async def check_models_are_eligible(db: AsyncSession, config: AiStrategyConfig) -> None:
    """Every model this configuration names must be usable. Section 12.

    Raises rather than filtering: a configuration that silently dropped an
    ineligible model would run a different pipeline from the one it describes,
    and the operator who wrote it would have no way to notice.
    """
    for requirement in config.models:
        verdict = await eligibility.check(db, key=requirement.key, version=requirement.version)
        if not verdict.eligible:
            raise AiIntegrationError(
                f"{requirement.key} v{requirement.version} may not be used: {verdict.reason}"
            )


async def save(
    db: AsyncSession,
    *,
    strategy_key: str,
    config: AiStrategyConfig,
    account_id: str | None = None,
    enabled: bool = True,
    user_id: str | None = None,
) -> AiStrategyConfiguration:
    """Write a configuration, after checking every model it names.

    Idempotent on `(strategy_key, account_id)`: saving twice updates one row
    rather than minting a second, on the same reasoning `register_version` uses
    — two rows under one scope are two answers to "how does this strategy use
    AI", and the one nobody looked at is the one a bot would run.
    """
    await check_models_are_eligible(db, config)

    row = await db.scalar(
        select(AiStrategyConfiguration).where(
            AiStrategyConfiguration.strategy_key == strategy_key,
            AiStrategyConfiguration.account_id == account_id,
        )
    )
    if row is None:
        row = AiStrategyConfiguration(strategy_key=strategy_key, account_id=account_id)
        db.add(row)

    thresholds = config.thresholds
    row.mode = str(config.mode)
    row.policy = str(config.policy)
    row.required_models = [m.as_dict() for m in config.required()] or None
    row.optional_models = [m.as_dict() for m in config.optional_models()] or None
    row.feature_version = config.feature_version or None
    row.minimum_probability = Decimal(str(thresholds.minimum_probability))
    row.maximum_anomaly_score = (
        Decimal(str(thresholds.maximum_anomaly_score))
        if thresholds.maximum_anomaly_score is not None
        else None
    )
    row.allowed_regimes = list(thresholds.allowed_regimes) or None
    row.minimum_expected_return = (
        Decimal(str(thresholds.minimum_expected_return))
        if thresholds.minimum_expected_return is not None
        else None
    )
    row.max_latency_ms = int(thresholds.maximum_latency_ms)
    row.scoring_method = str(thresholds.scoring_method)
    row.ai_weight = Decimal(str(thresholds.ai_weight))
    row.minimum_combined_score = Decimal(str(thresholds.minimum_combined_score))
    row.enabled = enabled
    row.notes = config.notes or None
    row.updated_by_user_id = user_id
    await db.commit()
    await db.refresh(row)
    return row


# ================================================================= journal


def _ratio(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 6)))


def record_of(
    decision: AiDecision,
    context: SignalContext | None,
    *,
    signal_id: str | None = None,
    prediction_id: str | None = None,
    model_version_id: str | None = None,
    bot_id: str | None = None,
    account_id: str | None = None,
    trading_mode: str | None = None,
    execution_id: str | None = None,
) -> AiDecisionRecord:
    """One decision as a journal row. Section 35.

    A pure function, so the caller decides when to write and the shape can be
    tested without a session.
    """
    return AiDecisionRecord(
        strategy_key=(context.strategy_key if context else "unknown"),
        strategy_version=(context.strategy_version if context else None),
        symbol=(context.symbol if context else None),
        timeframe=(context.timeframe if context else None),
        bar_time=(
            context.bar_time.replace(tzinfo=None)
            if context and context.bar_time.tzinfo
            else (context.bar_time if context else None)
        ),
        side=(context.side if context else None),
        signal_id=signal_id,
        prediction_id=prediction_id,
        model_version_id=model_version_id,
        model_key=decision.model_key,
        model_version=decision.model_version,
        feature_version=decision.feature_version,
        mode=str(decision.mode),
        policy=str(decision.policy),
        decision=str(decision.decision),
        status=decision.status[:32],
        reason=decision.reason,
        probability=_ratio(decision.probability),
        predicted_class=decision.predicted_class,
        regime=decision.regime,
        anomaly_score=_ratio(decision.anomaly_score),
        confidence=_ratio(decision.confidence),
        strategy_score=_ratio(decision.strategy_score),
        combined_score=_ratio(decision.combined_score),
        feature_latency_ms=(
            Decimal(str(decision.feature_latency_ms))
            if decision.feature_latency_ms is not None
            else None
        ),
        inference_latency_ms=(
            Decimal(str(decision.inference_latency_ms))
            if decision.inference_latency_ms is not None
            else None
        ),
        total_latency_ms=(
            Decimal(str(decision.total_latency_ms))
            if decision.total_latency_ms is not None
            else None
        ),
        bot_id=bot_id,
        account_id=account_id,
        mode_of_trading=trading_mode,
        execution_id=execution_id,
        detail={
            "predictions": list(decision.predictions),
            "explanation": list(decision.explanation),
            "authority": (
                "advisory. This decision could not place, size or approve an order, "
                "raise a risk limit, or enable live trading."
            ),
        },
    )


async def journal(db: AsyncSession, record: AiDecisionRecord) -> AiDecisionRecord:
    """Write one decision. A trade never fails because a log did."""
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def attach_outcome(
    db: AsyncSession,
    record_id: str,
    *,
    final_outcome: str | None = None,
    risk_verdict: str | None = None,
    order_id: str | None = None,
) -> None:
    """What happened AFTER the AI layer, on the same row. Section 35.

    So "the AI accepted and risk vetoed" is one row rather than a correlation
    somebody has to reconstruct from two tables and a timestamp.
    """
    row = await db.get(AiDecisionRecord, record_id)
    if row is None:
        return
    if final_outcome is not None:
        row.final_outcome = final_outcome[:32]
    if risk_verdict is not None:
        row.risk_verdict = risk_verdict[:32]
    if order_id is not None:
        row.order_id = order_id
    await db.commit()


def summarise(row: AiDecisionRecord) -> dict[str, Any]:
    """One journal row as an API row."""
    return {
        "id": row.id,
        "strategy_key": row.strategy_key,
        "strategy_version": row.strategy_version,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "bar_time": row.bar_time.isoformat() if row.bar_time else None,
        "side": row.side,
        "mode": row.mode,
        "policy": row.policy,
        "decision": row.decision,
        "status": row.status,
        "reason": row.reason,
        "model": f"{row.model_key} v{row.model_version}" if row.model_key else None,
        "model_key": row.model_key,
        "model_version": row.model_version,
        "feature_version": row.feature_version,
        "probability": float(row.probability) if row.probability is not None else None,
        "predicted_class": row.predicted_class,
        "regime": row.regime,
        "anomaly_score": float(row.anomaly_score) if row.anomaly_score is not None else None,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "strategy_score": float(row.strategy_score) if row.strategy_score is not None else None,
        "combined_score": float(row.combined_score) if row.combined_score is not None else None,
        "latency_ms": {
            "features": (
                float(row.feature_latency_ms) if row.feature_latency_ms is not None else None
            ),
            "inference": (
                float(row.inference_latency_ms) if row.inference_latency_ms is not None else None
            ),
            "total": float(row.total_latency_ms) if row.total_latency_ms is not None else None,
        },
        "final_outcome": row.final_outcome,
        "risk_verdict": row.risk_verdict,
        "order_id": row.order_id,
        "execution_id": row.execution_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "authority": (
            "advisory. An ACCEPT means the AI layer did not object; the risk engine, "
            "position sizing and the OMS all ran afterwards and any of them could refuse."
        ),
    }


def summarise_config(row: AiStrategyConfiguration) -> dict[str, Any]:
    """One configuration row as an API row. No secret is ever on it."""
    return {
        "id": row.id,
        "strategy_key": row.strategy_key,
        "account_id": row.account_id,
        "mode": row.mode,
        "policy": row.policy,
        "enabled": row.enabled,
        "required_models": row.required_models or [],
        "optional_models": row.optional_models or [],
        "feature_version": row.feature_version,
        "thresholds": {
            "minimum_probability": float(row.minimum_probability),
            "maximum_anomaly_score": (
                float(row.maximum_anomaly_score) if row.maximum_anomaly_score is not None else None
            ),
            "allowed_regimes": row.allowed_regimes or [],
            "minimum_expected_return": (
                float(row.minimum_expected_return)
                if row.minimum_expected_return is not None
                else None
            ),
            "maximum_latency_ms": row.max_latency_ms,
            "scoring_method": row.scoring_method,
            "ai_weight": float(row.ai_weight),
            "minimum_combined_score": float(row.minimum_combined_score),
        },
        "notes": row.notes,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "authority": (
            "no mode here lets the AI layer place, size or approve an order, raise a risk "
            "limit or enable live trading. The strongest thing it can do is decline a "
            "signal the strategy produced."
        ),
    }
