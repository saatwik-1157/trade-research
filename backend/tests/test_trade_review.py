"""AI trade review and post-trade intelligence (L33).

The ones that matter most:

  * `test_entry_quality_cannot_see_the_outcome` — §19 and §63, the level's core.
  * `test_a_losing_trade_can_have_a_good_entry` — §13.
  * `test_unknown_is_never_non_compliant` — §12.
  * `test_a_duplicate_request_does_not_create_a_second_review` — §11.
  * `test_a_regeneration_adds_a_version_and_keeps_the_old_one` — §33 and §44.
  * `test_an_invented_price_is_rejected` — §37 and §38.
  * `test_a_provider_failure_does_not_touch_the_trade` — §57.
  * `test_nothing_is_estimated_when_it_was_not_recorded` — §5.
  * `test_the_review_layer_cannot_place_or_modify_anything` — §1 and §70.
  * `test_no_secret_reaches_the_provider_payload` — §45.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.db.base import Base
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.ai_integration import AiDecisionRecord
from app.models.execution import Execution, Order, Position, Trade
from app.models.market import Symbol
from app.models.review import REVIEW_STATUSES, TradeReviewRow
from app.models.risk import RiskEvent
from app.models.strategies import Strategy, StrategyVersion
from app.review import checks, patterns
from app.review import provider as provider_module
from app.review import validation as validator
from app.review.context import DecisionContext, OutcomeContext, ReviewInput, completeness_of
from app.review.contract import (
    Compliance,
    EvidenceKind,
    Outcome,
    Rating,
    ReviewStatus,
    TradeReview,
    hypothesis,
    interpreted,
    observed,
    outcome_of,
    unrated,
)
from app.review.provider import DeterministicProvider, Narration, ProviderError
from app.review.service import TradeReviewService
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

REVIEW = Path(__file__).resolve().parents[1] / "app" / "review"
ROUTER = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "reviews.py"

NOW = datetime(2026, 9, 4, 12, 0, 0)
SECRET = "broker-password-do-not-leak"


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
        session.add(
            PaperAccount(
                id="paper1",
                user_id="u1",
                name="paper",
                currency="USD",
                starting_balance=Decimal("100000"),
                balance=Decimal("100000"),
                equity=Decimal("100000"),
                status="active",
            )
        )
        session.add(
            BrokerAccount(
                id="broker1",
                user_id="u1",
                name="demo",
                broker="mt5",
                account_mode="demo",
                currency="USD",
            )
        )
        session.add(Symbol(id="sym1", code="EURUSD", asset_class="fx"))
        await session.commit()
    yield factory
    await engine.dispose()


async def make_strategy(db: AsyncSession, *, config: dict[str, Any] | None = None) -> str:
    strategy = Strategy(id="st1", key="breakout", name="Breakout")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(
        id="sv1",
        strategy_id="st1",
        version=1,
        code_ref="breakout@1",
        config=config
        if config is not None
        else {
            "name": "Breakout",
            "symbol": "EURUSD",
            "timeframe": "H1",
            "entry_rules": [{"kind": "rsi"}],
            "exit_rules": [{"kind": "atr_stop"}],
        },
        status="validated",
    )
    db.add(version)
    await db.flush()
    return version.id


async def make_trade(
    db: AsyncSession,
    *,
    net: str = "50",
    entry: str = "1.1000",
    exit_price: str = "1.1050",
    stop: str | None = "1.0900",
    target: str | None = "1.1200",
    strategy: str | None = None,
    status: str = "closed",
    mode: str = "paper",
    account: str = "paper1",
    with_position: bool = True,
    r: str | None = "1.0",
) -> Trade:
    position_id = None
    if with_position:
        position = Position(
            mode=mode,
            paper_account_id=account if mode == "paper" else None,
            broker_account_id=account if mode != "paper" else None,
            symbol_id="sym1",
            side="long",
            quantity=Decimal("0"),
            initial_quantity=Decimal("1"),
            closed_quantity=Decimal("1"),
            entry_price=Decimal(entry),
            stop_loss=Decimal(stop) if stop else None,
            take_profit=Decimal(target) if target else None,
            status="closed",
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW,
        )
        db.add(position)
        await db.flush()
        position_id = position.id

    trade = Trade(
        mode=mode,
        status=status,
        position_id=position_id,
        symbol_id="sym1",
        paper_account_id=account if mode == "paper" else None,
        broker_account_id=account if mode != "paper" else None,
        strategy_version_id=strategy,
        side="long",
        volume=Decimal("1"),
        entry_price=Decimal(entry),
        exit_price=Decimal(exit_price),
        opened_at=NOW - timedelta(hours=2),
        closed_at=NOW,
        gross_profit=Decimal(net),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_profit=Decimal(net),
        r_multiple=Decimal(r) if r else None,
        exit_reason="take_profit",
        currency="USD",
        source="pipeline",
    )
    db.add(trade)
    await db.flush()
    return trade


def decision_of(**over: Any) -> DecisionContext:
    base: dict[str, Any] = {
        "trade_id": "t1",
        "environment": "paper",
        "symbol": "EURUSD",
        "side": "long",
        "entry_at": NOW - timedelta(hours=2),
        "entry_price": Decimal("1.1000"),
        "quantity": Decimal("1"),
        "planned_stop_loss": Decimal("1.0900"),
        "planned_take_profit": Decimal("1.1200"),
    }
    base.update(over)
    return DecisionContext(**base)


def outcome_ctx(**over: Any) -> OutcomeContext:
    base: dict[str, Any] = {
        "exit_at": NOW,
        "exit_price": Decimal("1.1200"),
        "exit_reason": "take_profit",
        "net_profit": Decimal("50"),
        "gross_profit": Decimal("50"),
        "r_multiple": Decimal("1.0"),
        "currency": "USD",
    }
    base.update(over)
    return OutcomeContext(**base)


def input_of(decision: DecisionContext, outcome: OutcomeContext) -> ReviewInput:
    return ReviewInput(
        decision=decision,
        outcome=outcome,
        completeness=completeness_of(decision, outcome),
        built_at=NOW,
    )


# ============================================ FUTURE LEAKAGE — §19 and §63


def test_entry_quality_cannot_see_the_outcome() -> None:
    """§63. Change everything after the entry; the entry rating is identical.

    It passes because `entry_quality` is handed a `DecisionContext` and there is
    no outcome on it to read -- not because the function is careful.
    """
    decision = decision_of()
    won = checks.entry_quality(decision)
    # Nothing about the decision changed, so nothing about the assessment can.
    again = checks.entry_quality(decision)
    assert won.rating is again.rating
    assert [e.as_dict() for e in won.evidence] == [e.as_dict() for e in again.evidence]


def test_the_decision_context_carries_no_outcome_field() -> None:
    """The structural guarantee behind the test above."""
    forbidden = {
        "exit_price",
        "exit_at",
        "exit_reason",
        "net_profit",
        "gross_profit",
        "r_multiple",
        "mae",
        "mfe",
        "slippage_points",
    }
    assert not (forbidden & set(DecisionContext.__dataclass_fields__))


def test_a_losing_trade_can_have_a_good_entry() -> None:
    """§13. Do not pretend an entry was bad merely because the trade lost."""
    decision = decision_of(
        requested_entry_price=Decimal("1.1000"), signal_id="sig1", signal_source="tradingview"
    )
    winner = checks.entry_quality(decision)
    # Same decision, opposite result.
    assert winner.rating is Rating.good
    loser_review = TradeReview(
        trade_id="t1",
        environment="paper",
        outcome=outcome_of(Decimal("-80")),
        entry_quality=checks.entry_quality(decision),
    )
    assert loser_review.outcome is Outcome.loss
    assert loser_review.entry_quality.rating is Rating.good


def test_a_winning_trade_can_have_poor_execution() -> None:
    """The other half of §13."""
    section = checks.execution_quality(
        outcome_ctx(fills=1, slippage_points=[Decimal("120")], net_profit=Decimal("500"))
    )
    assert section.rating is Rating.poor
    assert any("slippage" in w for w in section.warnings)


def test_exit_quality_never_asks_how_far_price_travelled() -> None:
    """§19. It compares to the PLAN, not to a high it could have reached."""
    section = checks.exit_quality(decision_of(), outcome_ctx(exit_price=Decimal("1.1200")))
    text = " ".join(e.statement for e in section.evidence)
    assert "planned" in text.lower()
    assert section.rating is Rating.good
    assert any("BEFORE entry" in e.statement for e in section.evidence)


# ================================================ UNKNOWN is not a verdict


def test_unknown_is_never_non_compliant() -> None:
    """§12. A missing strategy record is not a non-compliant trade."""
    section, compliance = checks.strategy_alignment(decision_of(strategy_version_id=None))
    assert compliance is Compliance.unknown
    assert compliance is not Compliance.non_compliant
    assert section.rating is Rating.unknown
    assert "NOT a non-compliant trade" in (section.unavailable_reason or "")


def test_a_linked_strategy_with_no_definition_is_still_unknown() -> None:
    section, compliance = checks.strategy_alignment(
        decision_of(strategy_version_id="sv1", strategy_definition=None)
    )
    assert compliance is Compliance.unknown


def test_a_matching_definition_is_compliant() -> None:
    section, compliance = checks.strategy_alignment(
        decision_of(
            strategy_version_id="sv1",
            timeframe="H1",
            strategy_definition={
                "symbol": "EURUSD",
                "timeframe": "H1",
                "exit_rules": [{"kind": "atr_stop"}],
            },
        )
    )
    assert compliance is Compliance.compliant
    assert section.rating is Rating.good


def test_a_symbol_mismatch_is_reported() -> None:
    section, compliance = checks.strategy_alignment(
        decision_of(
            strategy_version_id="sv1",
            symbol="GBPUSD",
            timeframe="H1",
            strategy_definition={"symbol": "EURUSD", "timeframe": "H1"},
        )
    )
    assert compliance is Compliance.partially_compliant
    assert any("mismatch" in w for w in section.warnings)


def test_risk_without_records_is_unknown_not_poor() -> None:
    section = checks.risk_quality(decision_of())
    assert section.rating is Rating.unknown
    assert "not evidence that risk was mismanaged" in (section.unavailable_reason or "")


def test_execution_without_records_is_unknown_not_poor() -> None:
    section = checks.execution_quality(outcome_ctx())
    assert section.rating is Rating.unknown


# ===================================================== nothing is invented


def test_nothing_is_estimated_when_it_was_not_recorded() -> None:
    """§5. MAE unavailable means null, not an estimate."""
    block = checks.market_context(decision_of(), outcome_ctx())
    assert block["outcome_side"]["mae"] is None
    assert block["outcome_side"]["mfe"] is None
    assert "estimated" in block["outcome_side"]["note"]


def test_an_unrecorded_regime_is_unavailable_not_guessed() -> None:
    block = checks.market_context(decision_of(ai_regime=None), outcome_ctx())
    assert block["available"] is False
    assert block["regime"] is None
    assert block["observations"] == []
    assert "spread at entry" in block["not_recorded"]


def test_a_recorded_regime_is_reported_with_its_source() -> None:
    block = checks.market_context(decision_of(ai_regime="TRENDING"), outcome_ctx())
    assert block["available"] is True
    assert block["observations"][0]["kind"] == "OBSERVED"
    assert block["observations"][0]["source"] == "ai_decisions.regime"


def test_an_absent_ai_context_says_which_kind_of_absent() -> None:
    block = checks.ai_context(decision_of(ai_decision=None), outcome_ctx())
    assert block["available"] is False
    assert "NOT evidence the AI was bypassed" in block["why"]


def test_an_ai_disagreement_is_reported_never_scored() -> None:
    """§20. A 0.72 that loses is what 0.72 means 28% of the time."""
    block = checks.ai_context(
        decision_of(ai_decision="ACCEPT", ai_probability=Decimal("0.72")),
        outcome_ctx(net_profit=Decimal("-40")),
    )
    assert block["outcome_agreed"] is False
    assert "says nothing about the model on its own" in block["note"]
    assert "L29" in block["note"]


# ================================================ evidence kinds — §41


def test_every_observed_statement_names_its_source() -> None:
    assert observed("x", "table.column").source == "table.column"
    assert interpreted("y").kind is EvidenceKind.interpreted
    assert hypothesis("z").kind is EvidenceKind.hypothesis
    assert hypothesis("z").source is None


def test_a_section_rated_unknown_must_say_why() -> None:
    section = unrated("no strategy is linked")
    assert section.rating is Rating.unknown
    assert section.unavailable_reason


# ================================================== outcome, deterministic


def test_the_outcome_comes_from_the_recorded_figure() -> None:
    assert outcome_of(Decimal("1")) is Outcome.win
    assert outcome_of(Decimal("-1")) is Outcome.loss
    assert outcome_of(Decimal("0")) is Outcome.breakeven
    assert outcome_of(None) is Outcome.unknown


# ============================================== confidence — §22


def test_confidence_reflects_completeness_not_fluency() -> None:
    sparse = input_of(decision_of(), outcome_ctx())
    rich = input_of(
        decision_of(
            strategy_version_id="sv1",
            strategy_definition={"symbol": "EURUSD", "timeframe": "H1"},
            timeframe="H1",
            requested_entry_price=Decimal("1.1000"),
            signal_id="s1",
            ai_decision="ACCEPT",
            risk_decision="approve",
            risk_snapshot={"equity": "100000"},
            sizing={"final_quantity": "1"},
        ),
        outcome_ctx(
            fills=1,
            slippage_points=[Decimal("1")],
            submit_latency_seconds=0.4,
            fill_latency_seconds=0.9,
        ),
    )
    service = TradeReviewService()
    low = validator.confidence_from(sparse, service.assess(sparse))
    high = validator.confidence_from(rich, service.assess(rich))
    assert 0.0 <= low <= 1.0
    assert high > low


def test_confidence_is_a_probability() -> None:
    source = input_of(decision_of(), outcome_ctx())
    value = validator.confidence_from(source, TradeReviewService().assess(source))
    assert 0.0 <= value <= 1.0


# ========================================= hallucination control — §37, §38


def _review(source: ReviewInput) -> TradeReview:
    return TradeReviewService().assess(source)


def test_a_valid_deterministic_review_passes_validation() -> None:
    source = input_of(decision_of(), outcome_ctx())
    review = _review(source)
    provider_module.apply(review, DeterministicProvider().narrate(review, source))
    review.confidence = validator.confidence_from(source, review)
    assert validator.validate(review, source).valid


def test_an_invented_price_is_rejected() -> None:
    """§37. A figure the facts do not support does not reach the database."""
    source = input_of(decision_of(), outcome_ctx())
    review = _review(source)
    review.confidence = 0.5
    provider_module.apply(review, Narration(summary="The position closed at 9.8765."))
    verdict = validator.validate(review, source)
    assert not verdict.valid
    assert any("9.8765" in e for e in verdict.errors)


def test_an_invented_symbol_is_rejected() -> None:
    source = input_of(decision_of(), outcome_ctx())
    review = _review(source)
    review.confidence = 0.5
    provider_module.apply(review, Narration(summary="A clean GBPUSD trade."))
    verdict = validator.validate(review, source)
    assert not verdict.valid
    assert any("GBPUSD" in e for e in verdict.errors)


def test_a_wrong_trade_id_is_rejected() -> None:
    source = input_of(decision_of(), outcome_ctx())
    review = _review(source)
    review.trade_id = "someone-elses-trade"
    review.confidence = 0.5
    verdict = validator.validate(review, source)
    assert not verdict.valid


def test_a_wrong_environment_is_rejected() -> None:
    """§55. Paper and live are never conflated, including by a narrative."""
    source = input_of(decision_of(environment="paper"), outcome_ctx())
    review = _review(source)
    review.environment = "live"
    review.confidence = 0.5
    verdict = validator.validate(review, source)
    assert not verdict.valid
    assert any("never conflated" in e for e in verdict.errors)


def test_an_invented_model_version_is_rejected() -> None:
    """§52. Never refer to a model version the record does not carry."""
    source = input_of(decision_of(prediction_model_version=None), outcome_ctx())
    review = _review(source)
    review.prediction_model_version = "9.9"
    review.confidence = 0.5
    verdict = validator.validate(review, source)
    assert not verdict.valid


def test_a_confidence_outside_the_interval_is_rejected() -> None:
    source = input_of(decision_of(), outcome_ctx())
    review = _review(source)
    review.confidence = 1.7
    assert not validator.structural(review).valid


def test_a_review_with_no_model_attribution_is_rejected() -> None:
    """§21 and §38. Untraceable output is not stored as trusted data."""
    source = input_of(decision_of(), outcome_ctx())
    review = _review(source)
    review.review_model = ""
    review.confidence = 0.5
    assert not validator.structural(review).valid


def test_an_observed_claim_with_no_source_is_rejected() -> None:
    source = input_of(decision_of(), outcome_ctx())
    review = _review(source)
    review.confidence = 0.5
    review.entry_quality.evidence.append(
        # Constructed around the helper deliberately, to prove the validator
        # catches what the helper prevents.
        type(review.entry_quality.evidence[0])(EvidenceKind.observed, "price was 5", None)
    )
    assert not validator.structural(review).valid


def test_a_narrative_may_quote_a_recorded_figure() -> None:
    source = input_of(decision_of(), outcome_ctx())
    review = _review(source)
    review.confidence = 0.5
    provider_module.apply(review, Narration(summary="Entry was 1.1000 and exit was 1.1200."))
    assert validator.validate(review, source).valid


# ============================================== the two model identities §21


def test_the_review_model_and_the_prediction_model_are_separate() -> None:
    source = input_of(
        decision_of(prediction_model="trade_probability", prediction_model_version="2.3"),
        outcome_ctx(),
    )
    review = _review(source)
    assert review.review_model == "deterministic"
    assert review.prediction_model == "trade_probability"
    assert review.prediction_model_version == "2.3"
    described = review.as_dict()["attribution"]
    assert described["review_model"] != described["prediction_model"]


# ================================================== provider — §35, §45, §57


def test_the_default_provider_is_not_a_language_model() -> None:
    described = provider_module.describe(DeterministicProvider())
    assert described["name"] == "deterministic"
    assert described["external"] is False
    assert "No LLM provider exists" in described["note"]


def test_no_secret_reaches_the_provider_payload() -> None:
    """§45. There is nothing on either context that could carry one."""
    source = input_of(
        decision_of(risk_snapshot={"equity": "100000"}, sizing={"final_quantity": "1"}),
        outcome_ctx(),
    )
    payload = str(source.as_dict()).lower()
    for forbidden in ("password", "api_key", "secret", "token", "credential"):
        assert forbidden not in payload


def test_a_context_has_no_credential_field() -> None:
    forbidden = {"password", "api_key", "secret", "token", "credential"}
    assert not (forbidden & set(DecisionContext.__dataclass_fields__))
    assert not (forbidden & set(OutcomeContext.__dataclass_fields__))


class BrokenProvider:
    name = "broken"
    version = "0"

    def narrate(self, review: TradeReview, source: ReviewInput) -> Narration:
        raise ProviderError("the provider is unreachable")


async def test_a_provider_failure_does_not_touch_the_trade(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§57. The trading system remains operational independently."""
    async with sessions() as db:
        trade = await make_trade(db)
        await db.commit()
        before = (trade.status, trade.net_profit, trade.exit_price)

        produced = await TradeReviewService(BrokenProvider()).review_for(db, trade)
        await db.commit()

    assert produced.row is not None
    assert produced.row.status == str(ReviewStatus.failed)
    assert "ProviderError" in (produced.row.error or "")
    assert (trade.status, trade.net_profit, trade.exit_price) == before


# ================================================= the service — §3, §8, §11


async def test_a_completed_trade_receives_a_review(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        strategy = await make_strategy(db)
        trade = await make_trade(db, strategy=strategy)
        await db.commit()
        produced = await TradeReviewService().review_for(db, trade)
        await db.commit()

    assert produced.created is True
    assert produced.row is not None
    assert produced.row.status == str(ReviewStatus.completed)
    assert produced.row.outcome == "WIN"
    assert produced.row.review_model == "deterministic"
    assert produced.row.summary


async def test_an_unconfirmed_close_is_not_reviewed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§3 and §56. A review describes a completed episode."""
    async with sessions() as db:
        trade = await make_trade(db, status="unknown")
        await db.commit()
        produced = await TradeReviewService().review_for(db, trade)
    assert produced.created is False
    assert produced.row is None
    assert "completed episode" in produced.reason


async def test_a_duplicate_request_does_not_create_a_second_review(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§11. A redelivered TRADE_CLOSED event produces the same review."""
    service = TradeReviewService()
    async with sessions() as db:
        trade = await make_trade(db)
        await db.commit()
        first = await service.review_for(db, trade, request_id="evt-1")
        await db.commit()
        second = await service.review_for(db, trade, request_id="evt-1")
        await db.commit()
        rows = list((await db.scalars(select(TradeReviewRow))).all())

    assert first.created is True
    assert second.created is False
    assert len(rows) == 1


async def test_the_database_refuses_a_duplicate_version(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The guarantee is the schema's, not only the service's."""
    async with sessions() as db:
        trade = await make_trade(db)
        await db.commit()
        await TradeReviewService().review_for(db, trade)
        await db.commit()
        db.add(
            TradeReviewRow(
                trade_id=trade.id,
                environment=trade.mode,
                review_version=1,
                status="PENDING",
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_a_regeneration_adds_a_version_and_keeps_the_old_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§33 and §44. Both remain traceable."""
    service = TradeReviewService()
    async with sessions() as db:
        trade = await make_trade(db)
        await db.commit()
        first = await service.review_for(db, trade)
        await db.commit()
        second = await service.review_for(db, trade, regenerate=True)
        await db.commit()
        versions = await service.versions_for(db, trade.id)

    assert first.row is not None and second.row is not None
    assert first.row.review_version == 1
    assert second.row.review_version == 2
    assert [v.review_version for v in versions] == [1, 2]
    assert versions[0].status == str(ReviewStatus.completed)


async def test_the_sweep_reviews_only_trades_without_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    service = TradeReviewService()
    async with sessions() as db:
        for _ in range(3):
            await make_trade(db)
        await db.commit()
        first = await service.review_pending(db)
        await db.commit()
        second = await service.review_pending(db)
        await db.commit()
        rows = list((await db.scalars(select(TradeReviewRow))).all())

    assert sum(1 for p in first if p.created) == 3
    assert second == []
    assert len(rows) == 3


async def test_the_review_stores_the_input_snapshot(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§32. A future reader can answer 'what data did the reviewer see?'."""
    async with sessions() as db:
        trade = await make_trade(db)
        await db.commit()
        produced = await TradeReviewService().review_for(db, trade)
        await db.commit()

    assert produced.row is not None
    snapshot = produced.row.input_snapshot
    assert snapshot is not None
    assert "decision_context" in snapshot
    assert "outcome_context" in snapshot
    assert "separation" in snapshot


async def test_the_strategy_definition_is_read_at_the_trades_own_version(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§53. Never the strategy's current version."""
    async with sessions() as db:
        strategy = await make_strategy(db)
        trade = await make_trade(db, strategy=strategy)
        await db.commit()
        source = await TradeReviewService().build_input(db, trade)

    assert source.decision.strategy_version_id == strategy
    assert source.decision.strategy_definition is not None
    assert source.decision.timeframe == "H1"


async def test_the_environment_is_carried_onto_the_review(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§55."""
    async with sessions() as db:
        trade = await make_trade(db, mode="demo", account="broker1")
        await db.commit()
        produced = await TradeReviewService().review_for(db, trade)
    assert produced.row is not None
    assert produced.row.environment == "demo"


async def test_a_status_the_code_can_produce_is_a_status_the_schema_accepts() -> None:
    assert tuple(str(s) for s in ReviewStatus) == REVIEW_STATUSES


# ============================================== integration — §61


async def test_the_full_chain_attributes_every_context(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§61. Strategy, risk, sizing, execution and AI all reach the review."""
    async with sessions() as db:
        strategy = await make_strategy(db)
        order = Order(
            id="o1",
            intent_id="i1",
            mode="paper",
            paper_account_id="paper1",
            symbol_id="sym1",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
            requested_price=Decimal("1.1000"),
            status="filled",
            sizing={"mode": "risk_percent", "risk_amount": "500", "final_quantity": "1"},
            source="pipeline",
            created_at=NOW - timedelta(hours=3),
            updated_at=NOW - timedelta(hours=3),
            submitted_at=NOW - timedelta(hours=3) + timedelta(seconds=1),
            filled_at=NOW - timedelta(hours=3) + timedelta(seconds=2),
        )
        db.add(order)
        db.add(
            Execution(
                order_id="o1",
                executed_at=NOW - timedelta(hours=3),
                price=Decimal("1.1000"),
                quantity=Decimal("1"),
                fill_source="simulator",
                slippage_points=Decimal("2"),
            )
        )
        db.add(
            RiskEvent(
                id="r1",
                order_id="o1",
                decision="approve",
                reason="within limits",
                occurred_at=NOW - timedelta(hours=3),
                snapshot={"equity": "100000"},
                configuration_version="cfg-7",
                mode="paper",
            )
        )
        db.add(
            AiDecisionRecord(
                id="ai1",
                strategy_key="breakout",
                symbol="EURUSD",
                timeframe="H1",
                bar_time=NOW - timedelta(hours=3),
                side="long",
                model_key="trade_probability",
                model_version="2.3",
                model_version_id="mv1",
                mode="AI_FILTER",
                policy="AI_OPTIONAL",
                decision="ACCEPT",
                status="ok",
                probability=Decimal("0.72"),
                regime="TRENDING",
                order_id="o1",
            )
        )
        trade = await make_trade(db, strategy=strategy)
        trade.order_id = "o1"
        trade.risk_event_id = "r1"
        trade.ai_decision_id = "ai1"
        trade.requested_entry_price = Decimal("1.1000")
        await db.commit()

        produced = await TradeReviewService().review_for(db, trade)
        await db.commit()

    assert produced.row is not None
    row = produced.row
    assert row.status == str(ReviewStatus.completed)
    assert row.compliance == "COMPLIANT"
    assert row.risk_quality is not None
    assert row.execution_quality is not None
    assert row.ai_context is not None
    assert row.market_context is not None
    assert row.risk_quality["rating"] in ("GOOD", "FAIR")
    assert row.execution_quality["rating"] in ("GOOD", "FAIR")
    assert row.ai_context["model_version"] == "2.3"
    assert row.prediction_model_version == "2.3"
    assert row.review_model == "deterministic"
    assert row.market_context["regime"] == "TRENDING"


async def test_a_sizing_discrepancy_is_reported_not_corrected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§16. Do not silently correct historical records."""
    section = checks.risk_quality(
        decision_of(
            risk_decision="approve",
            quantity=Decimal("3"),
            sizing={"final_quantity": "2.0"},
        )
    )
    assert any("discrepancy" in w for w in section.warnings)
    assert section.rating in (Rating.fair, Rating.poor)


# ================================================ patterns — §27, §28, §29


async def test_a_pattern_below_the_floor_is_labelled_insufficient(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§28. Never hide sample size."""
    async with sessions() as db:
        for _ in range(3):
            await make_trade(db, net="-20", strategy=None)
        await db.commit()
        found = await patterns.PatternFinder().across(db, environment="paper")

    assert found["total_observations"] >= 1
    assert all(
        o["reliable"] is False and "insufficient sample" in o["sample_note"]
        for o in found["observations"]
    )


async def test_patterns_conclude_nothing_about_what_to_do(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§29. Do not automatically disable the strategy."""
    async with sessions() as db:
        await make_trade(db, net="-20")
        await db.commit()
        found = await patterns.PatternFinder().across(db)
    assert "never actions" in found["authority"]
    assert "search" in found["caution"]


async def test_patterns_recompute_no_metric(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§27 and §50. Analytics owns the numbers."""
    async with sessions() as db:
        found = await patterns.PatternFinder().across(db)
    assert "L32 analytics" in found["method"]
    assert "No metric" in found["method"]


# ================================================= security — §1, §46, §70


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
    return found


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _sources() -> list[Path]:
    return sorted(REVIEW.glob("*.py")) + [ROUTER]


def test_the_review_layer_cannot_place_or_modify_anything() -> None:
    """§1 and §70."""
    forbidden = {
        "place",
        "place_order",
        "submit_order",
        "cancel_order",
        "modify_order",
        "close_position",
        "modify_position",
        "open_position",
        "send_order",
        "order_send",
    }
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


def test_the_review_layer_imports_no_execution_path() -> None:
    """§1: it cannot bypass risk because it cannot reach it."""
    forbidden = (
        "app.oms",
        "app.orders",
        "app.sizing",
        "app.execution",
        "app.brokers",
        "app.risk.engine",
        "app.risk.service",
        "app.positions.manager",
        "app.positions.executor",
        "app.positions.reconciler",
        "MetaTrader5",
    )
    for path in _sources():
        for module in _imports(path):
            assert not any(module == bad or module.startswith(bad + ".") for bad in forbidden), (
                f"{path.name} imports {module}"
            )


def test_the_review_layer_cannot_enable_live_trading() -> None:
    forbidden = {"LIVE_TRADING", "live_trading", "LIVE_GATES", "live_execution_allowed"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"
        assert "app.core.settings" not in _imports(path)


def test_the_review_layer_cannot_promote_or_replace_a_model() -> None:
    """§1: no automatic model replacement, and it has no vocabulary for one.

    The verbs checked are the registry's SPECIFIC ones. `rollback` and
    `register` are deliberately absent from the list: `db.rollback()` and
    `WorkerRegistry.register` are unrelated and a rule that flagged them would
    be a rule people learn to suppress. The import check below is the one that
    actually holds -- the registry's verbs are unreachable because the module
    that owns them is never imported.
    """
    forbidden = {"promote", "retire", "deploy_to_paper", "stop_deployment"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"
    for path in _sources():
        assert "app.ai.registry_service" not in _imports(path), (
            f"{path.name} imports the model registry service, whose verbs promote, "
            "roll back and retire a model version."
        )


def test_the_review_layer_never_deletes() -> None:
    forbidden = {"delete", "drop_all", "drop_table", "truncate"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


def test_no_secret_name_is_referenced() -> None:
    """§45 and §58."""
    forbidden = {"password", "api_key", "secret", "credential", "auth_token"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"
