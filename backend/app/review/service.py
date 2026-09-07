"""Assembling, running, validating and storing a review.

Section 8's pipeline, and §64's constraint on it:

    trade closed -> eligibility -> collect context -> build input
                 -> narrate -> validate -> store -> event

**Nothing here can delay a trade.** §8 and §64. The journal writes its row and
returns; a review is produced by a separate pass over trades that have none.
That is the same sweep shape L31 chose, for the same reason -- a hook missed at
one of four close sites produces a review that silently never exists -- and it
has the additional property §9 asks for: if the provider is unavailable, the
trade is already complete and unaffected.

**Nothing here executes.** §1 and §70. This module imports no order manager, no
sizer, no risk decision and no broker write path, and a parsed test keeps that
true. It cannot change a risk rule because it cannot reach one.

**Idempotent by construction.** §11. `review_for` returns the existing row for a
(trade, version) rather than writing a second, and a unique constraint makes the
race a database error rather than a duplicate. A redelivered TRADE_CLOSED event
produces the same review.

**A failure is recorded, not raised.** §57. A provider error marks the review
FAILED with the reason and leaves the trade, the journal, the portfolio and the
OMS untouched.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_integration import AiDecisionRecord
from app.models.execution import Execution, Order, Position, Trade
from app.models.market import Symbol
from app.models.review import TradeReviewRow
from app.models.risk import RiskEvent
from app.models.signals import Signal
from app.models.strategies import StrategyVersion
from app.review import checks
from app.review import validation as validator
from app.review.context import DecisionContext, OutcomeContext, ReviewInput, completeness_of
from app.review.contract import (
    PROMPT_VERSION,
    REVIEW_SCHEMA_VERSION,
    Compliance,
    ReviewStatus,
    TradeReview,
    outcome_of,
)
from app.review.provider import (
    DeterministicProvider,
    ProviderError,
    ReviewProvider,
    apply,
    utcnow,
)

log = logging.getLogger("app.review")

#: §47. How many times a failed review is retried before it stays FAILED. Small
#: deliberately: a provider that fails three times is not going to succeed on
#: the fourth, and an unbounded retry is the runaway cost §47 warns about.
MAX_ATTEMPTS = 3

#: §47. How many reviews one sweep will generate. A ceiling rather than a rate
#: limiter: this is the number that stops a first run over a large journal from
#: becoming an unbounded job.
MAX_PER_SWEEP = 50

#: §3 and §56. A review is written for a completed trade. `unknown` is
#: excluded: the venue never confirmed the close, so what is being reviewed is
#: not yet a fact.
REVIEWABLE_STATUSES = frozenset({"closed"})


class ReviewError(Exception):
    """A review could not be produced, and the message says why."""


@dataclass(frozen=True)
class Produced:
    """The outcome of one `review_for` call."""

    row: TradeReviewRow | None
    created: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.row.id if self.row else None,
            "created": self.created,
            "reason": self.reason,
        }


class TradeReviewService:
    """Reviews completed trades. Owns no trading state and changes none."""

    def __init__(self, provider: ReviewProvider | None = None) -> None:
        self.provider: ReviewProvider = provider or DeterministicProvider()

    # -------------------------------------------------------- eligibility

    async def eligible(self, db: AsyncSession, trade: Trade) -> tuple[bool, str]:
        """Section 3. A completed trade, and nothing else.

        §3 forbids reviewing incomplete orders and hypothetical trades. A trade
        the venue never confirmed is `unknown` in the journal and is refused
        here with that reason, rather than reviewed as though it had closed.
        """
        if trade.status not in REVIEWABLE_STATUSES:
            return False, (
                f"the trade is {trade.status!r}. A review describes a completed episode, "
                "and reviewing one the venue has not confirmed would put an unconfirmed "
                "fact in a record that reads as analysis."
            )
        if trade.net_profit is None or trade.exit_price is None:
            return False, "the trade records no exit price or no realised result."
        return True, ""

    # ------------------------------------------------------------ context

    async def build_input(self, db: AsyncSession, trade: Trade) -> ReviewInput:
        """Sections 4 and 7: the facts, collected before any interpretation."""
        symbol = await db.get(Symbol, trade.symbol_id)
        position = await db.get(Position, trade.position_id) if trade.position_id else None
        order = await db.get(Order, trade.order_id) if trade.order_id else None
        signal = await db.get(Signal, trade.signal_id) if trade.signal_id else None
        risk = await db.get(RiskEvent, trade.risk_event_id) if trade.risk_event_id else None
        ai = await db.get(AiDecisionRecord, trade.ai_decision_id) if trade.ai_decision_id else None
        definition = await self._strategy_definition(db, trade.strategy_version_id)

        fills: list[Execution] = []
        if order is not None:
            fills = list(
                (
                    await db.scalars(
                        select(Execution)
                        .where(Execution.order_id == order.id)
                        .order_by(Execution.executed_at)
                    )
                ).all()
            )

        decision = DecisionContext(
            trade_id=trade.id,
            environment=trade.mode,
            symbol=symbol.code if symbol else None,
            side=trade.side,
            entry_at=trade.opened_at,
            entry_price=trade.entry_price,
            requested_entry_price=trade.requested_entry_price,
            quantity=trade.volume,
            # The levels as PLANNED, from the position record. §16.
            planned_stop_loss=position.stop_loss if position else None,
            planned_take_profit=position.take_profit if position else None,
            strategy_version_id=trade.strategy_version_id,
            strategy_definition=definition,
            timeframe=(definition or {}).get("timeframe") if definition else None,
            signal_id=trade.signal_id,
            signal_source=getattr(signal, "source", None),
            signal_confidence=getattr(signal, "confidence", None),
            signal_at=getattr(signal, "signal_time", None),
            tradingview_event_id=getattr(signal, "webhook_event_id", None),
            ai_decision=getattr(ai, "decision", None),
            ai_probability=getattr(ai, "probability", None),
            ai_confidence=getattr(ai, "confidence", None),
            ai_regime=getattr(ai, "regime", None),
            ai_anomaly_score=getattr(ai, "anomaly_score", None),
            prediction_model=getattr(ai, "model_key", None),
            prediction_model_version=getattr(ai, "model_version", None),
            ai_mode=getattr(ai, "policy", None),
            risk_decision=getattr(risk, "decision", None),
            risk_reason=getattr(risk, "reason", None),
            risk_snapshot=getattr(risk, "snapshot", None),
            risk_configuration_version=getattr(risk, "configuration_version", None),
            sizing=getattr(order, "sizing", None),
        )

        outcome = OutcomeContext(
            exit_at=trade.closed_at,
            exit_price=trade.exit_price,
            exit_reason=trade.exit_reason,
            closes=await self._close_count(db, trade),
            gross_profit=trade.gross_profit,
            net_profit=trade.net_profit,
            commission=trade.commission,
            swap=trade.swap,
            fees=trade.fees,
            r_multiple=trade.r_multiple,
            currency=trade.currency,
            fills=len(fills),
            slippage_points=[f.slippage_points for f in fills if f.slippage_points is not None],
            submit_latency_seconds=_seconds(
                getattr(order, "created_at", None), getattr(order, "submitted_at", None)
            ),
            fill_latency_seconds=_seconds(
                getattr(order, "submitted_at", None), getattr(order, "filled_at", None)
            ),
            partial_fill=bool(
                order is not None
                and order.filled_quantity is not None
                and order.quantity is not None
                and order.filled_quantity < order.quantity
            ),
            broker_position_id=trade.broker_position_id,
            # §4 and §5. MAE and MFE need an intratrade price series this
            # deployment does not store. Null, never estimated.
            mae=None,
            mfe=None,
            data_quality=trade.data_quality,
        )

        return ReviewInput(
            decision=decision,
            outcome=outcome,
            baselines=await self._baselines(db, trade),
            completeness=completeness_of(decision, outcome),
            built_at=utcnow(),
        )

    async def _strategy_definition(
        self, db: AsyncSession, strategy_version_id: str | None
    ) -> dict[str, Any] | None:
        """The definition as THAT version recorded it. Section 53.

        Read by version id, never by strategy id: attaching a strategy's current
        definition to an old trade would judge it against rules it was never
        run under, which §53 forbids in as many words.
        """
        if not strategy_version_id:
            return None
        row = await db.get(StrategyVersion, strategy_version_id)
        return row.config if row is not None else None

    async def _close_count(self, db: AsyncSession, trade: Trade) -> int:
        if not trade.position_id:
            return 0
        from app.models.execution import PositionEvent

        return int(
            await db.scalar(
                select(func.count(PositionEvent.id)).where(
                    PositionEvent.position_id == trade.position_id,
                    PositionEvent.event_type.in_(("partially_closed", "closed")),
                )
            )
            or 0
        )

    async def _baselines(self, db: AsyncSession, trade: Trade) -> dict[str, Any]:
        """Historical context from L32 analytics. Section 50.

        Consumed, never recomputed: expectancy and execution medians belong to
        the analytics engine, and a second computation here would eventually
        disagree with the dashboard showing the first.
        """
        from app.analytics.service import AnalyticsService, Scope
        from app.analytics.windows import Window

        account = trade.paper_account_id or trade.broker_account_id
        scope = Scope(
            window=Window(None, None, "all_time"),
            environment=trade.mode,
            account_id=account,
            strategy_version_id=trade.strategy_version_id,
        )
        try:
            summary = await AnalyticsService().summary(db, scope)
        except Exception:  # noqa: BLE001 - a review does not fail because analytics did
            log.warning(
                "review baselines unavailable",
                extra={"event": "review_baselines_failed", "trade_id": trade.id},
            )
            return {"available": False, "why": "analytics could not be read for this scope"}
        return {
            "available": True,
            "strategy_trades": summary["trade_count"],
            "strategy_expectancy": summary["currency"]["expectancy"],
            "strategy_win_rate": summary["currency"]["win_rate"],
            "source": "/v1/analytics/summary (L32), consumed rather than recomputed",
            "sample_note": (
                f"{summary['trade_count']} trades in this scope. A comparison against "
                "fewer than 30 is a description of the sample, not a pattern."
            ),
        }

    # ------------------------------------------------------------- review

    def assess(self, source: ReviewInput) -> TradeReview:
        """Every rating, deterministically. Section 36.

        The narrative layer never runs before this: §7 says the reviewer must
        receive structured facts rather than reconstruct them, and this is where
        those facts become an assessment.
        """
        alignment, compliance = checks.strategy_alignment(source.decision)
        review = TradeReview(
            trade_id=source.trade_id,
            environment=source.environment,
            outcome=outcome_of(source.outcome.net_profit),
            strategy_alignment=alignment,
            # Decision-side assessors receive the decision context ALONE.
            entry_quality=checks.entry_quality(source.decision),
            exit_quality=checks.exit_quality(source.decision, source.outcome),
            risk_quality=checks.risk_quality(source.decision),
            execution_quality=checks.execution_quality(source.outcome),
            compliance=compliance,
            market_context=checks.market_context(source.decision, source.outcome),
            ai_context=checks.ai_context(source.decision, source.outcome),
            completeness=source.completeness,
            review_model=self.provider.name,
            review_model_version=self.provider.version,
            prediction_model=source.decision.prediction_model,
            prediction_model_version=source.decision.prediction_model_version,
            schema_version=REVIEW_SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            generated_at=utcnow(),
        )
        for section in review.sections().values():
            review.warnings.extend(section.warnings)
        if source.outcome.data_quality and source.outcome.data_quality.get("errors"):
            review.warnings.append(
                "the journal flagged data-quality errors on this trade; the figures "
                "reviewed here carry those flags."
            )
        return review

    # -------------------------------------------------------------- store

    async def review_for(
        self,
        db: AsyncSession,
        trade: Trade,
        *,
        requested_by: str | None = None,
        request_id: str | None = None,
        regenerate: bool = False,
    ) -> Produced:
        """One review of one trade. Idempotent unless `regenerate`. Sections 11, 33.

        A regeneration writes the NEXT version and leaves every earlier one
        readable -- §44: both remain traceable.
        """
        latest = await self.latest_for(db, trade.id)
        if latest is not None and not regenerate:
            return Produced(latest, created=False, reason="a review already exists")

        ok, why = await self.eligible(db, trade)
        if not ok:
            return Produced(None, created=False, reason=why)

        version = (latest.review_version + 1) if latest is not None else 1
        clock = time.perf_counter()
        row = TradeReviewRow(
            trade_id=trade.id,
            environment=trade.mode,
            review_version=version,
            status=str(ReviewStatus.processing),
            request_id=request_id,
            requested_by_user_id=requested_by,
            schema_version=REVIEW_SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            started_at=utcnow(),
            attempts=1,
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            existing = await self.latest_for(db, trade.id)
            if existing is not None:
                return Produced(existing, created=False, reason="written concurrently")
            raise

        source = await self.build_input(db, trade)
        review = self.assess(source)

        try:
            apply(review, self.provider.narrate(review, source))
        except ProviderError as exc:
            # §57. The trade is already complete and is untouched.
            row.status = str(ReviewStatus.failed)
            row.error = f"{type(exc).__name__}: {exc}"[:500]
            row.duration_ms = (time.perf_counter() - clock) * 1000
            row.input_snapshot = source.as_dict()
            log.warning(
                "trade review failed",
                extra={
                    "event": "trade_review_failed",
                    "trade_id": trade.id,
                    "review_version": version,
                    "provider": self.provider.name,
                    "attempts": row.attempts,
                },
            )
            return Produced(row, created=True, reason="the provider failed")

        review.confidence = validator.confidence_from(source, review)
        verdict = validator.validate(review, source)

        row.duration_ms = (time.perf_counter() - clock) * 1000
        row.input_snapshot = source.as_dict()
        row.validation = verdict.as_dict()

        if not verdict.valid:
            # §38. Never stored as trusted data, and never repaired.
            row.status = str(ReviewStatus.failed)
            row.error = "; ".join(verdict.errors)[:500]
            log.warning(
                "trade review failed validation",
                extra={
                    "event": "trade_review_invalid",
                    "trade_id": trade.id,
                    "review_version": version,
                    "errors": len(verdict.errors),
                },
            )
            return Produced(row, created=True, reason="the review failed validation")

        _write(row, review)
        row.status = str(ReviewStatus.completed)
        row.completed_at = utcnow()
        log.info(
            "trade review completed",
            extra={
                "event": "trade_review_completed",
                "trade_id": trade.id,
                "review_version": version,
                "provider": self.provider.name,
                "confidence": review.confidence,
                "duration_ms": round(row.duration_ms, 2),
            },
        )
        return Produced(row, created=True)

    async def latest_for(self, db: AsyncSession, trade_id: str) -> TradeReviewRow | None:
        return await db.scalar(
            select(TradeReviewRow)
            .where(TradeReviewRow.trade_id == trade_id)
            .order_by(TradeReviewRow.review_version.desc())
            .limit(1)
        )

    async def versions_for(self, db: AsyncSession, trade_id: str) -> list[TradeReviewRow]:
        """Every version, oldest first. Section 44: none is overwritten."""
        return list(
            (
                await db.scalars(
                    select(TradeReviewRow)
                    .where(TradeReviewRow.trade_id == trade_id)
                    .order_by(TradeReviewRow.review_version)
                )
            ).all()
        )

    # -------------------------------------------------------------- sweep

    async def review_pending(
        self, db: AsyncSession, *, environment: str | None = None, limit: int = MAX_PER_SWEEP
    ) -> list[Produced]:
        """Every completed trade with no review yet. Sections 8, 48 and 64.

        A sweep rather than a hook, for L31's reason: several paths close a
        trade and a missed hook produces a review that silently never exists.
        Bounded by `limit`, because §47 asks that reviews not be generated
        without a ceiling.
        """
        query = (
            select(Trade)
            .outerjoin(TradeReviewRow, TradeReviewRow.trade_id == Trade.id)
            .where(Trade.status.in_(sorted(REVIEWABLE_STATUSES)), TradeReviewRow.id.is_(None))
            .order_by(Trade.closed_at.desc())
            .limit(min(limit, MAX_PER_SWEEP))
        )
        if environment:
            query = query.where(Trade.mode == environment)
        out: list[Produced] = []
        for trade in (await db.scalars(query)).all():
            out.append(await self.review_for(db, trade))
        return out

    # ------------------------------------------------------------- events

    async def publish(self, hub: Any | None, db: AsyncSession, row: TradeReviewRow) -> bool:
        """Section 49. The events L34 will consume; no notification is sent here.

        Account-scoped, so `channels.py` authorizes it with the rule it already
        has. The account is read from the TRADE rather than assumed: a review
        published on the wrong channel is a private figure delivered to whoever
        happened to be subscribed there.
        """
        if hub is None:
            return False
        from app.core.events import Event

        account = await self._account_for(db, row)
        if account is None:
            # No account, no channel. Not published rather than published
            # somewhere plausible.
            return False

        kind = (
            "TRADE_REVIEW_COMPLETED"
            if row.status == str(ReviewStatus.completed)
            else "TRADE_REVIEW_FAILED"
        )
        try:
            await hub.publish(
                Event(
                    type=kind,
                    payload={
                        "review_id": row.id,
                        "trade_id": row.trade_id,
                        "environment": row.environment,
                        "review_version": row.review_version,
                        "status": row.status,
                        "outcome": row.outcome,
                        "confidence": row.confidence,
                    },
                    source="trade_review",
                    channel=f"account:{account}",
                )
            )
        except Exception:  # noqa: BLE001 - a review does not fail because an event did
            log.warning(
                "a trade review event could not be published",
                extra={"event": "review_event_failed", "review_id": row.id},
            )
            return False
        return True

    async def _account_for(self, db: AsyncSession, row: TradeReviewRow) -> str | None:
        """The account the reviewed trade belongs to."""
        trade = await db.get(Trade, row.trade_id)
        if trade is None:
            return None
        return trade.paper_account_id or trade.broker_account_id


def _write(row: TradeReviewRow, review: TradeReview) -> None:
    """Copy a validated review onto its row. Nothing else touches these columns."""
    row.summary = review.summary
    row.outcome = str(review.outcome)
    row.compliance = str(review.compliance)
    row.strategy_alignment = review.strategy_alignment.as_dict()
    row.entry_quality = review.entry_quality.as_dict()
    row.exit_quality = review.exit_quality.as_dict()
    row.risk_quality = review.risk_quality.as_dict()
    row.execution_quality = review.execution_quality.as_dict()
    row.market_context = review.market_context
    row.ai_context = review.ai_context
    row.key_factors = [e.as_dict() for e in review.key_factors]
    row.warnings = review.warnings
    row.lessons = [e.as_dict() for e in review.lessons]
    row.follow_up_questions = review.follow_up_questions
    row.confidence = review.confidence
    row.completeness = review.completeness.as_dict()
    row.review_model = review.review_model
    row.review_model_version = review.review_model_version
    row.prediction_model = review.prediction_model
    row.prediction_model_version = review.prediction_model_version


def _seconds(start: Any, end: Any) -> float | None:
    """A duration, or None. Never inferred from a missing timestamp. Section 17."""
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


__all__ = [
    "MAX_ATTEMPTS",
    "MAX_PER_SWEEP",
    "REVIEWABLE_STATUSES",
    "Compliance",
    "Produced",
    "ReviewError",
    "TradeReviewService",
]
