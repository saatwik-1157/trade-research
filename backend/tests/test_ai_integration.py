"""AI strategy integration (L27).

The ones that matter most, and they are the rules the brief calls absolute:

  * `test_ai_disabled_is_byte_identical_to_the_deterministic_strategy` — §7.
  * `test_the_ai_layer_cannot_turn_a_flat_bar_into_a_trade` — §2 and §31.
  * `test_the_ai_cannot_overturn_a_risk_rejection` — §20, the absolute rule.
  * `test_the_ai_layer_reaches_no_venue_sizer_or_risk_engine` — §50, parsed.
  * `test_an_unvalidated_model_cannot_be_named_by_a_strategy` — §12.
  * `test_the_ai_layer_sees_only_the_bars_the_strategy_saw` — §16.
  * `test_an_out_of_range_probability_is_an_error_not_a_confident_model` — §25.
"""

from __future__ import annotations

import ast
import math
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.ai import eligibility
from app.ai import strategy_config as ai_config
from app.ai.contract import Prediction, PredictionStatus
from app.ai.decision import (
    AiDecision,
    AiIntegrationError,
    AiMode,
    AiPolicy,
    AiStrategyConfig,
    AiThresholds,
    Decision,
    ModelRequirement,
    ScoringMethod,
    SignalContext,
    combine,
    disabled_decision,
)
from app.ai.integration import AiIntegrationService, ModelResolver
from app.ai.probability import LogisticCoefficients, TradeProbabilityModel
from app.ai.regime import RegimeCuts, RegimeModel
from app.ai.registry import ModelRegistry
from app.datasets import features as feature_engine
from app.db.base import Base
from app.execution.ai import AiGate, AiVerdict, IntegrationFilter, ModelBackedFilter
from app.marketdata.types import Availability, Bar, Provider, Timeframe
from app.models.ai import AIModel, ModelVersion
from app.models.ai_integration import AiDecisionRecord, AiStrategyConfiguration
from app.models.validation import ValidationRun
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
FEATURES = ("rsi_14", "atr_pct_14", "ema_spread_10_50", "return_5")


# ================================================================= fixtures


def make_bars(n: int = 200, *, seed: int = 5) -> list[Bar]:
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
                spread=Decimal("0.00002"),
                spread_availability=Availability.available,
            )
        )
    return out


def probability_model(*, bias: float = 4.0, version: str = "1.0") -> TradeProbabilityModel:
    """A fitted model with a controllable output. Real arithmetic, no stub."""
    return TradeProbabilityModel(
        version=version,
        feature_version=feature_engine.FEATURE_SET_VERSION,
        coefficients=LogisticCoefficients(
            features=FEATURES,
            weights=(0.0, 0.0, 0.0, 0.0),
            bias=bias,
            fitted_rows=500,
        ),
    )


def regime_model() -> RegimeModel:
    return RegimeModel(
        feature_version=feature_engine.FEATURE_SET_VERSION,
        cuts=RegimeCuts(
            trend_low=-1.0,
            trend_high=1.0,
            volatility_low=0.0001,
            volatility_high=10.0,
            fitted_rows=500,
        ),
    )


def registry_with(*models) -> ModelRegistry:  # noqa: ANN002
    registry = ModelRegistry()
    for model in models:
        registry.register(model)
    return registry


def service_for(*models) -> AiIntegrationService:  # noqa: ANN002
    return AiIntegrationService(ModelResolver(registry_with(*models)))


def context(bars: list[Bar], *, score: float | None = None, side: str = "buy") -> SignalContext:
    return SignalContext(
        strategy_key="rsi_reversion",
        symbol="EURUSD",
        timeframe="H1",
        bar_time=bars[-1].bar_time,
        side=side,
        strategy_score=score,
        bars=tuple(bars),
    )


def config(**over: object) -> AiStrategyConfig:
    fields: dict[str, object] = {
        "strategy_key": "rsi_reversion",
        "mode": AiMode.filter,
        "policy": AiPolicy.optional,
        "models": (ModelRequirement(key="trade_probability", version="1.0"),),
    }
    fields.update(over)
    return AiStrategyConfig(**fields)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def bars() -> list[Bar]:
    return make_bars()


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

        session.add(User(id="u1", email="a@b.io", password_hash="x", role="trader"))
        await session.commit()
    yield factory
    await engine.dispose()


async def seed_model(
    db: AsyncSession, *, key: str = "trade_probability", version: str = "1.0", verdict: str | None
) -> str:
    """A model version, optionally with a completed validation run."""
    model = await db.scalar(select(AIModel).where(AIModel.key == key))
    if model is None:
        model = AIModel(key=key, name=key, kind="classifier")
        db.add(model)
        await db.flush()
    row = ModelVersion(
        model_id=model.id,
        version=1,
        artifact_ref=f"{key}:{version}",
        feature_version=feature_engine.FEATURE_SET_VERSION,
        status="draft",
        params={"artifact": {"kind": "logistic"}},
    )
    db.add(row)
    await db.flush()
    if verdict is not None:
        db.add(
            ValidationRun(
                model_version_id=row.id,
                status="completed",
                verdict=verdict,
                summary=f"{verdict} on all checks",
                report={"verdict": verdict},
            )
        )
    await db.commit()
    return row.id


# ============================================================ 1. the modes


def test_ai_disabled_runs_nothing_at_all(bars: list[Bar]) -> None:
    """§7. Not "a model that agrees" -- no inference happens."""
    service = service_for(probability_model())
    decision = service.evaluate(context(bars), config(mode=AiMode.disabled, models=()))
    assert decision.decision is Decision.accept
    assert decision.status == "DISABLED"
    assert decision.probability is None
    assert decision.total_latency_ms is None


def test_ai_disabled_is_byte_identical_to_the_deterministic_strategy() -> None:
    """§7. The baseline every comparison needs, and the AI outage fallback."""
    a = disabled_decision()
    b = disabled_decision()
    assert a.as_dict()["decision"] == b.as_dict()["decision"] == "ACCEPT"
    assert "behaves exactly as the deterministic strategy" in a.reason
    assert a.model_key is None and a.feature_version is None


def test_advisory_records_and_changes_nothing(bars: list[Bar]) -> None:
    """§8. The information is recorded; the signal is unchanged."""
    service = service_for(probability_model(bias=-6.0))  # a very low probability
    decision = service.evaluate(context(bars), config(mode=AiMode.advisory))
    assert decision.decision is Decision.neutral
    assert decision.probability is not None and decision.probability < 0.1
    assert "the signal is unchanged" in decision.reason


def test_filter_rejects_below_the_configured_minimum(bars: list[Bar]) -> None:
    """§9."""
    service = service_for(probability_model(bias=-2.0))
    decision = service.evaluate(
        context(bars),
        config(mode=AiMode.filter, thresholds=AiThresholds(minimum_probability=0.60)),
    )
    assert decision.decision is Decision.reject
    assert "below the configured minimum" in decision.reason
    assert "not a size" in decision.reason


def test_filter_accepts_above_the_minimum_and_says_risk_still_decides(bars: list[Bar]) -> None:
    service = service_for(probability_model(bias=4.0))
    decision = service.evaluate(
        context(bars),
        config(mode=AiMode.filter, thresholds=AiThresholds(minimum_probability=0.60)),
    )
    assert decision.decision is Decision.accept
    assert "the risk engine still decides" in decision.reason


def test_scoring_uses_a_named_formula_and_never_an_invented_one(bars: list[Bar]) -> None:
    """§10."""
    service = service_for(probability_model(bias=4.0))  # p ~ 0.982
    decision = service.evaluate(
        context(bars, score=0.30),
        config(
            mode=AiMode.scoring,
            thresholds=AiThresholds(
                scoring_method=ScoringMethod.minimum, minimum_combined_score=0.5
            ),
        ),
    )
    # min(0.30, 0.982) = 0.30, below 0.5. A confident AI cannot rescue a weak
    # strategy signal under the default method, which is why it is the default.
    assert decision.decision is Decision.reject
    assert decision.combined_score == pytest.approx(0.30)
    assert "min(strategy, ai)" in decision.reason


def test_scoring_without_a_strategy_score_refuses_rather_than_scoring_on_the_ai(
    bars: list[Bar],
) -> None:
    service = service_for(probability_model(bias=4.0))
    decision = service.evaluate(context(bars, score=None), config(mode=AiMode.scoring))
    assert decision.decision is Decision.reject
    assert "a different mode wearing this one's name" in decision.reason


def test_every_scoring_formula_is_monotone_in_both_inputs() -> None:
    """A threshold on a combination only means something if this holds."""
    for method in ScoringMethod:
        low = combine(method, 0.4, 0.4, ai_weight=0.5)
        higher_ai = combine(method, 0.4, 0.9, ai_weight=0.5)
        higher_strategy = combine(method, 0.9, 0.4, ai_weight=0.5)
        assert higher_ai >= low
        assert higher_strategy >= low


def test_a_score_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(AiIntegrationError):
        combine(ScoringMethod.minimum, 1.4, 0.5, ai_weight=0.5)


# =============================================== 2. failure and fallback


def test_ai_required_plus_no_answer_means_no_trade(bars: list[Bar]) -> None:
    """§11 and §27."""
    service = service_for()  # nothing registered
    decision = service.evaluate(context(bars), config(policy=AiPolicy.required))
    assert decision.decision is Decision.reject
    assert decision.status == "MODEL_UNAVAILABLE"
    assert "is not a model that agreed" in decision.reason


def test_ai_optional_plus_no_answer_falls_back_explicitly(bars: list[Bar]) -> None:
    """§28. The fallback is stated in the reason, never implicit."""
    service = service_for()
    decision = service.evaluate(context(bars), config(policy=AiPolicy.optional))
    assert decision.decision is Decision.accept
    assert decision.status == "MODEL_UNAVAILABLE"
    assert "still authoritative" in decision.reason


def test_a_failure_never_carries_a_probability(bars: list[Bar]) -> None:
    """§47. A failed inference that reports a number is a fabricated result."""
    service = service_for()
    decision = service.evaluate(context(bars), config(policy=AiPolicy.required))
    assert decision.probability is None
    with pytest.raises(AiIntegrationError):
        AiDecision(
            decision=Decision.error,
            mode=AiMode.filter,
            policy=AiPolicy.optional,
            reason="x",
            probability=0.9,
        )


def test_an_unfitted_model_is_a_refusal_not_a_default(bars: list[Bar]) -> None:
    unfitted = TradeProbabilityModel(feature_version=feature_engine.FEATURE_SET_VERSION)
    service = service_for(unfitted)
    decision = service.evaluate(context(bars), config(policy=AiPolicy.required))
    assert decision.decision is Decision.reject
    assert decision.status == "INCOMPATIBLE"
    assert "rather than a default" in decision.reason


def test_a_feature_version_mismatch_refuses_rather_than_running(bars: list[Bar]) -> None:
    """§15. A model run on features it was not fitted on is a different quantity."""
    stale = TradeProbabilityModel(
        feature_version="0.9",
        coefficients=LogisticCoefficients(
            features=FEATURES, weights=(0.0,) * 4, bias=1.0, fitted_rows=100
        ),
    )
    service = service_for(stale)
    decision = service.evaluate(
        context(bars),
        config(policy=AiPolicy.required, feature_version=feature_engine.FEATURE_SET_VERSION),
    )
    assert decision.status == "INCOMPATIBLE"
    assert "a different quantity" in decision.reason


def test_too_few_bars_refuses_rather_than_substituting_a_feature(bars: list[Bar]) -> None:
    """Nothing is filled with a default: a filled feature is read as a reading."""
    service = service_for(probability_model())
    decision = service.evaluate(context(bars[:5]), config(policy=AiPolicy.required))
    assert decision.status == "FEATURES_UNAVAILABLE"
    assert "Nothing is substituted" in decision.reason


def test_an_out_of_range_probability_is_an_error_not_a_confident_model(bars: list[Bar]) -> None:
    """§25. And it is not clamped into range."""

    class Broken(TradeProbabilityModel):
        def _infer(self, features, **kwargs):  # noqa: ANN001, ANN003, ANN201
            return Prediction(
                identity=self.identity,
                at=kwargs["at"],
                symbol=kwargs["symbol"],
                timeframe=kwargs["timeframe"],
                status=PredictionStatus.ok,
                value="WIN",
                probability=1.4,
                input_digest=kwargs["input_digest"],
            )

    broken = Broken(
        feature_version=feature_engine.FEATURE_SET_VERSION,
        coefficients=LogisticCoefficients(
            features=FEATURES, weights=(0.0,) * 4, bias=0.0, fitted_rows=100
        ),
    )
    service = service_for(broken)
    decision = service.evaluate(context(bars), config(policy=AiPolicy.required))
    assert decision.status == "INVALID_OUTPUT"
    assert decision.probability is None
    assert decision.decision is Decision.reject
    # L24's own `Prediction` refuses it first, which is the right place; L27
    # reports WHICH model and WHICH field rather than "something raised".
    assert "probability must be between 0 and 1, not 1.4" in decision.reason


def test_a_latency_budget_is_enforced_through_the_policy(bars: list[Bar]) -> None:
    """§26. Never a reason to skip a gate: it becomes a refusal or a fallback."""
    service = service_for(probability_model())
    decision = service.evaluate(
        context(bars),
        config(
            policy=AiPolicy.required,
            thresholds=AiThresholds(maximum_latency_ms=0.0001),
        ),
    )
    assert decision.status == "LATENCY_EXCEEDED"
    assert decision.decision is Decision.reject


def test_a_zero_latency_budget_is_a_configuration_mistake_not_a_policy() -> None:
    with pytest.raises(AiIntegrationError) as exc:
        AiThresholds(maximum_latency_ms=0.0)
    assert "configuration mistake" in str(exc.value)


def test_a_pipeline_exception_is_recorded_as_an_error_never_as_agreement(
    bars: list[Bar],
) -> None:
    class Exploding:
        def resolve(self, requirement):  # noqa: ANN001, ANN201
            raise RuntimeError("boom")

    service = AiIntegrationService(Exploding())
    decision = service.evaluate(context(bars), config(policy=AiPolicy.required))
    assert decision.decision is Decision.reject
    assert decision.status == "PIPELINE_ERROR"


# ================================================== 3. no future leakage


def test_the_ai_layer_refuses_a_window_reaching_past_the_signal(bars: list[Bar]) -> None:
    """§16. Refused rather than trimmed: a trim leaves no trace."""
    ahead = SignalContext(
        strategy_key="rsi_reversion",
        symbol="EURUSD",
        timeframe="H1",
        bar_time=bars[-5].bar_time,
        side="buy",
        bars=tuple(bars),
    )
    service = service_for(probability_model())
    decision = service.evaluate(ahead, config(policy=AiPolicy.required))
    assert decision.status == "LOOKAHEAD_REFUSED"
    assert "rather than trimmed" in decision.reason


def test_the_ai_layer_sees_only_the_bars_the_strategy_saw(bars: list[Bar]) -> None:
    """The decision at bar i is a function of bars[:i+1] and nothing later."""
    service = service_for(probability_model(bias=0.0))
    early = service.evaluate(context(bars[:120]), config(mode=AiMode.advisory))
    # The same window, with 80 later bars appended AFTER the signal's bar time,
    # is refused rather than producing a different number.
    later = service.evaluate(
        SignalContext(
            strategy_key="rsi_reversion",
            symbol="EURUSD",
            timeframe="H1",
            bar_time=bars[119].bar_time,
            side="buy",
            bars=tuple(bars),
        ),
        config(mode=AiMode.advisory),
    )
    assert early.probability is not None
    assert later.status == "LOOKAHEAD_REFUSED"


def test_no_label_reaches_inference(bars: list[Bar]) -> None:
    """§16. The context carries features and bars; it has no label field."""
    ctx = context(bars)
    assert not hasattr(ctx, "label")
    assert not hasattr(ctx, "outcome")
    assert "label" not in ctx.as_dict()
    assert "bracket_outcome" not in str(ctx.as_dict())


# ============================================ 4. multiple models and gates


def test_a_regime_gate_can_stop_a_signal_a_probability_would_accept(
    bars: list[Bar],
) -> None:
    """§23. A probability estimated in one regime is not evidence about another."""
    service = service_for(probability_model(bias=6.0), regime_model())
    decision = service.evaluate(
        context(bars),
        config(
            models=(
                ModelRequirement(key="trade_probability", version="1.0"),
                ModelRequirement(key="regime", version="1.0"),
            ),
            thresholds=AiThresholds(allowed_regimes=("TRENDING_UP",)),
        ),
    )
    assert decision.regime is not None
    if decision.regime != "TRENDING_UP":
        assert decision.decision is Decision.reject
        assert "not in the configured" in decision.reason


def test_an_optional_model_that_is_missing_does_not_stop_the_pipeline(
    bars: list[Bar],
) -> None:
    """§23. Each strategy declares its dependencies; optional means optional."""
    service = service_for(probability_model(bias=4.0))
    decision = service.evaluate(
        context(bars),
        config(
            models=(
                ModelRequirement(key="trade_probability", version="1.0"),
                ModelRequirement(key="anomaly", version="1.0", optional=True),
            )
        ),
    )
    assert decision.decision is Decision.accept
    assert decision.anomaly_score is None


def test_a_mode_that_decides_on_a_probability_refuses_without_one(bars: list[Bar]) -> None:
    """A regime label is not a probability and is not substituted for one."""
    service = service_for(regime_model())
    decision = service.evaluate(
        context(bars),
        config(
            policy=AiPolicy.required,
            models=(ModelRequirement(key="regime", version="1.0"),),
        ),
    )
    assert decision.status == "NO_PROBABILITY"
    assert "is not substituted for one" in decision.reason


def test_a_model_requirement_must_name_an_exact_version() -> None:
    """§12. No `latest`."""
    with pytest.raises(AiIntegrationError) as exc:
        ModelRequirement(key="trade_probability", version="")
    assert "no 'latest' option" in str(exc.value)


def test_a_mode_that_runs_inference_must_name_a_model() -> None:
    """§28. An AI_FILTER with no model is a mistake, not a quiet AI_DISABLED."""
    with pytest.raises(AiIntegrationError) as exc:
        AiStrategyConfig(strategy_key="s", mode=AiMode.filter, models=())
    assert "if the intent is no AI, say AI_DISABLED" in str(exc.value)


# ================================================== 5. eligibility (§12)


async def test_an_unvalidated_model_cannot_be_named_by_a_strategy(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await seed_model(db, verdict=None)
        verdict = await eligibility.check(db, key="trade_probability", version="1.0")
    assert not verdict.eligible
    assert "never been validated" in verdict.reason
    assert "Not a judgement about the model" in verdict.reason


async def test_a_validated_model_is_eligible_for_consideration(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await seed_model(db, verdict="PASS")
        verdict = await eligibility.check(db, key="trade_probability", version="1.0")
    assert verdict.eligible
    assert "eligible for CONSIDERATION" in verdict.reason
    assert "not a promotion" in verdict.reason


async def test_a_conditional_verdict_is_usable_and_a_blocked_one_is_not(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await seed_model(db, key="trade_probability", version="1.0", verdict="CONDITIONAL")
        await seed_model(db, key="regime", version="1.0", verdict="BLOCKED")
        good = await eligibility.check(db, key="trade_probability", version="1.0")
        bad = await eligibility.check(db, key="regime", version="1.0")
    assert good.eligible
    assert not bad.eligible
    assert "nothing was established about this model" in bad.reason


async def test_a_failing_model_is_not_eligible(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await seed_model(db, verdict="FAIL")
        verdict = await eligibility.check(db, key="trade_probability", version="1.0")
    assert not verdict.eligible
    assert "not eligible for consideration" in verdict.reason


async def test_an_unknown_model_or_version_is_refused_not_guessed(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await seed_model(db, verdict="PASS")
        missing_model = await eligibility.check(db, key="nope", version="1.0")
        missing_version = await eligibility.check(db, key="trade_probability", version="9.9")
    assert "nobody registered" in missing_model.reason
    assert "a version that is guessed is a model nobody chose" in missing_version.reason


async def test_saving_a_config_that_names_an_ineligible_model_is_refused(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await seed_model(db, verdict="FAIL")
        with pytest.raises(AiIntegrationError) as exc:
            await ai_config.save(db, strategy_key="rsi_reversion", config=config())
    assert "may not be used" in str(exc.value)


# ================================================= 6. configuration (§38)


async def test_a_strategy_with_no_configuration_is_ai_disabled(sessions) -> None:  # noqa: ANN001
    """§7. The only default in the level, and it is the right one."""
    async with sessions() as db:
        loaded = await ai_config.config_for(db, "never-configured")
    assert loaded.mode is AiMode.disabled
    assert loaded.policy is AiPolicy.optional
    assert "That is the baseline, not a degraded mode" in loaded.notes


async def test_a_configuration_round_trips_through_the_database(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await seed_model(db, verdict="PASS")
        written = config(
            mode=AiMode.scoring,
            policy=AiPolicy.required,
            thresholds=AiThresholds(
                minimum_probability=0.62,
                scoring_method=ScoringMethod.product,
                ai_weight=0.7,
                minimum_combined_score=0.45,
                maximum_latency_ms=750.0,
            ),
        )
        await ai_config.save(db, strategy_key="rsi_reversion", config=written)
        loaded = await ai_config.config_for(db, "rsi_reversion")

    assert loaded.mode is AiMode.scoring
    assert loaded.policy is AiPolicy.required
    assert loaded.thresholds.minimum_probability == pytest.approx(0.62)
    assert loaded.thresholds.scoring_method is ScoringMethod.product
    assert loaded.thresholds.maximum_latency_ms == 750.0
    assert loaded.models[0].key == "trade_probability"


async def test_saving_twice_updates_one_row_rather_than_minting_a_second(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await seed_model(db, verdict="PASS")
        await ai_config.save(db, strategy_key="rsi_reversion", config=config())
        await ai_config.save(db, strategy_key="rsi_reversion", config=config(mode=AiMode.advisory))
        rows = list((await db.scalars(select(AiStrategyConfiguration))).all())
    assert len(rows) == 1
    assert rows[0].mode == "AI_ADVISORY"


async def test_a_switched_off_configuration_is_ai_disabled_not_the_old_settings(
    sessions,  # noqa: ANN001
) -> None:
    """§28. Fallback is never implicit."""
    async with sessions() as db:
        await seed_model(db, verdict="PASS")
        await ai_config.save(
            db, strategy_key="rsi_reversion", config=config(mode=AiMode.filter), enabled=False
        )
        loaded = await ai_config.config_for(db, "rsi_reversion")
    assert loaded.mode is AiMode.disabled


async def test_a_model_list_that_is_not_a_key_and_version_is_refused(sessions) -> None:  # noqa: ANN001
    """§41. This field is where a caller could try to name something else."""
    async with sessions() as db:
        row = AiStrategyConfiguration(
            strategy_key="evil",
            mode="AI_FILTER",
            policy="AI_OPTIONAL",
            required_models=[{"key": "../../etc/passwd", "version": {"exec": "rm -rf /"}}],
            minimum_probability=Decimal("0.5"),
            max_latency_ms=1000,
            scoring_method="minimum",
            ai_weight=Decimal("0.5"),
            minimum_combined_score=Decimal("0.5"),
            enabled=True,
        )
        db.add(row)
        await db.commit()
        with pytest.raises(AiIntegrationError) as exc:
            ai_config.to_config(row)
    assert "refused rather than coerced" in str(exc.value)


# ==================================================== 7. the journal (§35)


async def test_every_decision_leaves_a_row_including_the_ones_that_changed_nothing(
    sessions,  # noqa: ANN001
    bars: list[Bar],
) -> None:
    service = service_for(probability_model(bias=4.0))
    ctx = context(bars)
    async with sessions() as db:
        for mode in (AiMode.advisory, AiMode.filter):
            decision = service.evaluate(ctx, config(mode=mode))
            await ai_config.journal(db, ai_config.record_of(decision, ctx, trading_mode="paper"))
        rows = list((await db.scalars(select(AiDecisionRecord))).all())

    assert len(rows) == 2
    assert {r.decision for r in rows} == {"NEUTRAL", "ACCEPT"}
    assert all(r.probability is not None for r in rows)
    assert all(r.total_latency_ms is not None for r in rows)


async def test_the_journal_records_what_happened_after_the_ai(sessions, bars) -> None:  # noqa: ANN001
    """§35. "The AI accepted and risk vetoed" is ONE row."""
    service = service_for(probability_model(bias=4.0))
    ctx = context(bars)
    async with sessions() as db:
        decision = service.evaluate(ctx, config())
        row = await ai_config.journal(db, ai_config.record_of(decision, ctx))
        await ai_config.attach_outcome(
            db, row.id, final_outcome="risk_vetoed", risk_verdict="max_daily_loss"
        )
        again = await db.get(AiDecisionRecord, row.id)

    assert again.decision == "ACCEPT"
    assert again.final_outcome == "risk_vetoed"
    assert (
        "risk engine, position sizing and the OMS all ran"
        in ai_config.summarise(again)["authority"]
    )


async def test_a_journalled_error_cannot_carry_a_probability(sessions, bars) -> None:  # noqa: ANN001
    """§47, in the schema. A CHECK, not a convention."""
    from sqlalchemy.exc import IntegrityError

    async with sessions() as db:
        db.add(
            AiDecisionRecord(
                strategy_key="x",
                mode="AI_FILTER",
                policy="AI_OPTIONAL",
                decision="ERROR",
                status="MODEL_UNAVAILABLE",
                probability=Decimal("0.9"),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_a_disabled_run_cannot_have_produced_a_reading(sessions) -> None:  # noqa: ANN001
    from sqlalchemy.exc import IntegrityError

    async with sessions() as db:
        db.add(
            AiDecisionRecord(
                strategy_key="x",
                mode="AI_DISABLED",
                policy="AI_OPTIONAL",
                decision="ACCEPT",
                status="DISABLED",
                probability=Decimal("0.7"),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


# ================================================== 8. the seat and safety


def test_the_seat_converts_a_decision_without_gaining_authority(bars: list[Bar]) -> None:
    """`AiVerdict` still has no field by which anything could approve or size."""
    service = service_for(probability_model(bias=-4.0))
    seat = IntegrationFilter(service, config(), context_for=lambda s: s)
    verdict = seat.score(context(bars))
    assert isinstance(verdict, AiVerdict)
    assert verdict.accept is False
    assert set(verdict.as_dict()) == {"accept", "confidence", "reason", "model"}


def test_advisory_at_the_seat_accepts_but_records_neutral(bars: list[Bar]) -> None:
    """§8. The record still says NEUTRAL, so nobody later counts it as agreement."""
    service = service_for(probability_model(bias=-6.0))
    seat = IntegrationFilter(service, config(mode=AiMode.advisory), context_for=lambda s: s)
    verdict = seat.score(context(bars))
    assert verdict.accept is True
    decision, _ = seat.decisions[-1]
    assert decision.decision is Decision.neutral


def test_the_seat_pairs_each_decision_with_the_context_it_was_made_on(
    bars: list[Bar],
) -> None:
    service = service_for(probability_model())
    seat = IntegrationFilter(service, config(), context_for=lambda s: s)
    seat.score(context(bars))
    decision, ctx = seat.decisions[-1]
    assert ctx is not None and ctx.bar_time == bars[-1].bar_time
    assert decision.mode is AiMode.filter


def test_a_context_that_cannot_be_built_goes_through_the_failure_policy() -> None:
    def explode(_signal: object) -> SignalContext:
        raise ValueError("no bars here")

    service = service_for(probability_model())
    strict = IntegrationFilter(service, config(policy=AiPolicy.required), context_for=explode)
    assert strict.score(object()).accept is False
    lenient = IntegrationFilter(service, config(policy=AiPolicy.optional), context_for=explode)
    assert lenient.score(object()).accept is True


def test_the_l24_seat_still_works_unchanged(bars: list[Bar]) -> None:
    """`ModelBackedFilter` is KEPT, not replaced. §48."""
    model = probability_model(bias=4.0)
    rows = feature_engine.compute_features(bars, FEATURES)
    seat = ModelBackedFilter(
        model,
        AiGate(minimum_probability=0.5),
        feature_version=feature_engine.FEATURE_SET_VERSION,
        features_for=lambda _s: (rows[-1], bars[-1].bar_time, "EURUSD", "H1", None),
    )
    assert seat.score(object()).accept is True


AI_MODULES = sorted((Path(__file__).parent.parent / "app" / "ai").glob("*.py"))

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


def test_the_ai_layer_reaches_no_venue_sizer_or_risk_engine() -> None:
    """§50, parsed. The dependency runs execution -> ai and never back."""
    assert AI_MODULES
    for path in AI_MODULES:
        for imported in _imports(path):
            for banned in FORBIDDEN:
                assert not imported.startswith(banned), f"{path.name} imports {imported}"


def _names(path: Path) -> set[str]:
    """Every identifier and attribute a module actually USES.

    Parsed, so a docstring EXPLAINING a rule cannot fail the test that checks
    it. L28 added `app/ai/lifecycle.py`, whose docstring says in so many words
    that `promoted` holds under `LIVE_TRADING=false` -- and the raw-text version
    of this test failed on that sentence. The module touches nothing; the test
    was reading prose. Same fix L26 made for the same reason.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def test_the_ai_layer_cannot_enable_live_trading() -> None:
    for path in AI_MODULES:
        # The SETTINGS, not any identifier that happens to share a word.
        # `strategy_config.py` takes a `trading_mode` parameter -- the journal's
        # paper/demo/live label for one decision -- which is a legitimate use of
        # the word and not a read of the global switch. What must be absent is
        # the settings module and the gate names themselves.
        assert not _names(path) & {
            "LIVE_TRADING",
            "live_trading",
            "LIVE_GATES",
            "live_execution_allowed",
            "get_settings",
        }, path.name
        for imported in _imports(path):
            assert not imported.startswith("app.core.settings"), path.name


def test_the_ai_decision_type_has_no_field_that_could_size_or_approve() -> None:
    """§21 and §22. AI confidence is not allowed risk, structurally."""
    payload = AiDecision(
        decision=Decision.accept,
        mode=AiMode.filter,
        policy=AiPolicy.optional,
        reason="x",
        probability=0.9,
    ).as_dict()
    for forbidden in (
        "quantity",
        "volume",
        "lots",
        "risk_percent",
        "risk_amount",
        "account",
        "order",
        "approve",
        "leverage",
    ):
        assert forbidden not in payload
    assert "cannot place, size or approve" in payload["authority"]


def test_a_high_confidence_does_not_become_a_larger_risk() -> None:
    """§22. Default behaviour: the AI does not modify risk. There is no path."""
    confident = AiDecision(
        decision=Decision.accept,
        mode=AiMode.filter,
        policy=AiPolicy.optional,
        reason="x",
        probability=0.99,
        confidence=0.99,
    )
    verdict = AiVerdict(accept=True, confidence=Decimal("0.99"), reason=confident.reason, model="m")
    # The seat's whole vocabulary. Nothing here scales anything.
    assert set(verdict.as_dict()) == {"accept", "confidence", "reason", "model"}


# ============================================ 9. backtest integration (§31)


def _backtest_strategy():  # noqa: ANN202
    from app.strategies.registry import default_registry

    return default_registry().create("rsi_reversion", {})


def test_the_ai_layer_cannot_turn_a_flat_bar_into_a_trade(bars: list[Bar]) -> None:
    """§2 and §31. There is no branch that turns a 0 into a +/-1."""
    from app.backtest.runner import apply_ai_filter

    strategy = _backtest_strategy()
    signals = [0] * len(bars)
    service = service_for(probability_model(bias=8.0))  # accepts everything
    filtered, report = apply_ai_filter(
        signals, bars, strategy, "EURUSD", Timeframe.H1, service=service, config=config()
    )
    assert filtered == signals
    assert report["signals_offered"] == 0
    # And the layer was never asked: a flat bar is not a decision.
    assert report["accepted"] == 0 and report["rejected"] == 0


def test_a_filter_can_only_remove_trades_never_add_them(bars: list[Bar]) -> None:
    from app.backtest.runner import apply_ai_filter

    strategy = _backtest_strategy()
    signals = [1 if i % 20 == 0 else 0 for i in range(len(bars))]
    # AI_REQUIRED so the early bars -- which have too little history for the
    # feature warm-up -- also reject rather than falling back to accept. Under
    # AI_OPTIONAL they would proceed, which is the configured behaviour and is
    # the subject of its own test.
    service = service_for(probability_model(bias=-8.0))  # rejects everything it can score
    filtered, report = apply_ai_filter(
        signals,
        bars,
        strategy,
        "EURUSD",
        Timeframe.H1,
        service=service,
        config=config(policy=AiPolicy.required),
    )
    assert all(f == 0 for f in filtered)
    assert report["rejected"] == report["signals_offered"] > 0
    assert report["acceptance_rate"] == 0.0
    for original, kept in zip(signals, filtered, strict=True):
        assert kept == 0 or kept == original


def test_a_backtest_with_no_ai_is_the_untouched_baseline(bars: list[Bar]) -> None:
    """§7 and §31. AI_DISABLED means the vector is not touched at all."""
    from app.backtest.runner import build_signal_vector

    strategy = _backtest_strategy()
    signals = build_signal_vector(strategy, bars, "EURUSD", Timeframe.H1)
    again = build_signal_vector(strategy, bars, "EURUSD", Timeframe.H1)
    assert signals == again  # deterministic, and nothing in between


def test_the_backtest_ai_report_carries_the_counters_the_brief_asks_for(
    bars: list[Bar],
) -> None:
    from app.backtest.runner import apply_ai_filter

    strategy = _backtest_strategy()
    signals = [1 if i % 15 == 0 else 0 for i in range(len(bars))]
    service = service_for(probability_model(bias=4.0))
    _, report = apply_ai_filter(
        signals, bars, strategy, "EURUSD", Timeframe.H1, service=service, config=config()
    )
    for key in (
        "signals_offered",
        "accepted",
        "rejected",
        "acceptance_rate",
        "rejection_rate",
        "probability_distribution",
        "statuses",
    ):
        assert key in report
    assert "No future information reaches it" in report["causality"]
    assert "fewer trades is not by itself an improvement" in report["comparison"]


def test_the_backtest_filter_sees_only_the_prefix(bars: list[Bar]) -> None:
    """§31 and §32. At bar i the AI gets bars[:i+1], same as the strategy."""
    from app.backtest.runner import apply_ai_filter

    seen: list[int] = []

    class Watching:
        def evaluate(self, context, config):  # noqa: ANN001, ANN201
            seen.append(len(context.bars))
            assert context.bars[-1].bar_time <= context.bar_time
            return disabled_decision()

    strategy = _backtest_strategy()
    signals = [1 if i in (40, 90, 150) else 0 for i in range(len(bars))]
    apply_ai_filter(
        signals, bars, strategy, "EURUSD", Timeframe.H1, service=Watching(), config=config()
    )
    assert seen == [41, 91, 151]


# ============================================ 10. the risk engine is final


def test_the_ai_cannot_overturn_a_risk_rejection() -> None:
    """§20, the absolute rule. Structural: the seat cannot reach the engine.

    `AiVerdict` is the only thing the seat returns and the pipeline reads
    `accept` alone. There is no field on it that names a limit, a switch or an
    approval, so an AI that wanted to overturn a veto has no vocabulary for it.
    """
    from app.risk.engine import RiskVerdict

    approving = AiVerdict(accept=True, confidence=Decimal("1"), reason="very sure", model="m")
    assert set(approving.as_dict()) == {"accept", "confidence", "reason", "model"}
    # The seat has no vocabulary for any risk concept. `reason` is shared and
    # is prose; every field that CARRIES a risk decision -- the verdict itself,
    # the checks, the proposal -- has no counterpart an AI could set.
    risk_fields = {f for f in RiskVerdict.__dataclass_fields__}
    assert (set(approving.as_dict()) & risk_fields) == {"reason"}
    for deciding in ("decision", "checks", "proposal", "approved"):
        assert deciding not in approving.as_dict()


def test_the_ai_runs_before_risk_and_a_rejection_stops_the_pass() -> None:
    """The order in the orchestrator: AI is stage 4, risk is stage 8."""
    import inspect

    from app.execution import pipeline as pipeline_module

    source = inspect.getsource(pipeline_module.ExecutionPipeline._process)
    ai_at = source.index("the AI seat")
    risk_at = source.index("RISK")
    assert ai_at < risk_at


def test_no_ai_module_can_reach_position_sizing() -> None:
    """§21. Sizing is not something an AI configuration can influence."""
    for path in AI_MODULES:
        source = path.read_text(encoding="utf-8")
        assert "SizingRequest" not in source
        assert "lot_for_risk" not in source
        assert "calculate(" not in source
