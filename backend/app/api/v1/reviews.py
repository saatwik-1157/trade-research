"""Post-trade review: what happened on one trade, and what recurs across many.

**Nothing here trades.** Section 1 and §70. This router imports no order
manager, no sizer, no risk decision and no broker write path; a parsed test
keeps that true. The only writes are to `trade_reviews`.

**Nothing here is fabricated.** Section 5. A field the platform did not record
is `null` and the section that needed it says which one was missing. A rating is
`UNKNOWN` rather than guessed, and `UNKNOWN` is not a synonym for POOR.

**Decision quality and outcome are kept apart.** Sections 18, 19 and 63. The
assessors that rate the entry, the strategy and the risk receive only what was
known at or before the entry -- structurally, by signature -- so a losing trade
with a well-formed entry is rated as one.

**Access follows the trade.** Section 46. A review is readable exactly by
whoever may read its trade, checked in the query rather than after it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, page_params, paginate
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import NotFound, ValidationFailed
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.execution import Trade
from app.models.review import TradeReviewRow
from app.review import provider as provider_module
from app.review.contract import PROMPT_VERSION, REVIEW_SCHEMA_VERSION
from app.review.patterns import DIMENSIONS, PatternFinder
from app.review.service import MAX_PER_SWEEP, REVIEWABLE_STATUSES, TradeReviewService

router = APIRouter(tags=["trade-reviews"])

_READ = Depends(require_permission(Permission.view_journal))
#: §46 and §44. Regenerating writes a new version and costs a provider call, so
#: it is gated above reading. `manage_ai_models` is the permission this platform
#: already uses for anything that spends AI budget.
_REGENERATE = Depends(require_permission(Permission.manage_ai_models))

_SERVICE = TradeReviewService()
_PATTERNS = PatternFinder()


# ============================================================ authorization


async def _owned_trade(db: AsyncSession, user: User, trade_id: str) -> Trade:
    """The trade, if this user may read it. Section 46.

    Scoped in the query. A trade on somebody else's account and a trade that
    does not exist give the same 404, because distinguishing them is a
    membership oracle -- the rule `channels.py`, the portfolio router and the
    analytics router already record.
    """
    trade = await db.get(Trade, trade_id)
    if trade is None:
        raise NotFound("no such trade for this user")
    account = trade.paper_account_id or trade.broker_account_id
    if account is None:
        # An imported row with no account. Readable by anyone who may read the
        # journal, which is what serves it today.
        return trade
    owned = await db.scalar(
        select(PaperAccount.id).where(PaperAccount.id == account, PaperAccount.user_id == user.id)
    ) or await db.scalar(
        select(BrokerAccount.id).where(
            BrokerAccount.id == account, BrokerAccount.user_id == user.id
        )
    )
    if owned is None:
        raise NotFound("no such trade for this user")
    return trade


_TRADE = Annotated[str, Query(description="A trade you may read.")]


# =================================================================== routes


@router.get(
    "/trade-reviews/contract",
    summary="What a review contains, and what produces it",
    description=(
        "The schema version, the prompt version, the provider in use, the "
        "rating vocabulary and the three kinds of claim. Served rather than "
        "documented elsewhere, so a client can check what it is rendering."
    ),
)
async def contract(_: User = _READ) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "provider": provider_module.describe(_SERVICE.provider),
        "evidence_kinds": {
            "OBSERVED": "a fact read from a recorded row. It names its source.",
            "INTERPRETED": "a reading of those facts.",
            "HYPOTHESIS": "a possible explanation, offered as one.",
        },
        "ratings": ["GOOD", "FAIR", "POOR", "UNKNOWN"],
        "unknown_means": (
            "the evidence needed to rate this was not recorded. It is a statement "
            "about the platform, not about the trade, and it is NOT a synonym for "
            "POOR."
        ),
        "compliance": ["COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "UNKNOWN"],
        "statuses": ["PENDING", "PROCESSING", "COMPLETED", "FAILED", "RETRYING", "CANCELLED"],
        "reviewable_trade_statuses": sorted(REVIEWABLE_STATUSES),
        "deterministic": [
            "every rating",
            "every OBSERVED statement",
            "strategy compliance, where machine-checkable",
            "the confidence figure",
        ],
        "generated": ["the summary", "key factors", "lessons", "follow-up questions"],
        "future_leakage": (
            "decision-quality assessors receive only what was known at or before the "
            "entry -- the outcome is not on the object they are given. Post-trade facts "
            "may explain the RESULT and may not be used to judge the DECISION."
        ),
        "does_not": [
            "place, modify or cancel anything",
            "change a risk rule, a sizing rule or a kill switch",
            "activate a bot or enable live trading",
            "replace or promote a model",
            "disable a strategy on the basis of a pattern",
            "estimate a figure the platform did not record",
        ],
    }


@router.get(
    "/trades/{trade_id}/review",
    summary="The latest review of one trade",
    description=(
        "The most recent version. Earlier versions remain readable at "
        "/trade-reviews?trade_id=... -- a regeneration never overwrites one."
    ),
)
async def trade_review(
    trade_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    trade = await _owned_trade(db, user, trade_id)
    row = await _SERVICE.latest_for(db, trade.id)
    if row is None:
        eligible, why = await _SERVICE.eligible(db, trade)
        return {
            "trade_id": trade.id,
            "available": False,
            "eligible": eligible,
            "why": why or "no review has been generated for this trade yet.",
        }
    return {"trade_id": trade.id, "available": True, "review": row.as_dict()}


@router.post(
    "/trades/{trade_id}/review",
    summary="Generate a review for one trade",
    description=(
        "Idempotent: a trade that already has a review returns it rather than "
        "producing a second. Generation never blocks or alters the trade -- if "
        "it fails, the review is FAILED and the trade is untouched."
    ),
)
async def generate(
    request: Request,
    trade_id: str,
    request_id: str | None = Query(None, max_length=64, description="Deduplication key."),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    trade = await _owned_trade(db, user, trade_id)
    produced = await _SERVICE.review_for(db, trade, requested_by=user.id, request_id=request_id)
    await db.commit()
    if produced.row is not None:
        await _SERVICE.publish(getattr(request.app.state, "hub", None), db, produced.row)
    return {
        **produced.as_dict(),
        "review": produced.row.as_dict() if produced.row else None,
    }


@router.post(
    "/trade-reviews/{review_id}/regenerate",
    summary="Produce a new version of an existing review",
    description=(
        "Writes the NEXT version and leaves every earlier one readable. "
        "Section 44: both remain traceable, and history is never overwritten."
    ),
)
async def regenerate(
    request: Request,
    review_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _REGENERATE,
) -> dict[str, Any]:
    row = await db.get(TradeReviewRow, review_id)
    if row is None:
        raise NotFound("no such review")
    trade = await _owned_trade(db, user, row.trade_id)
    produced = await _SERVICE.review_for(db, trade, requested_by=user.id, regenerate=True)
    await db.commit()
    if produced.row is not None:
        await _SERVICE.publish(getattr(request.app.state, "hub", None), db, produced.row)
    return {
        **produced.as_dict(),
        "previous_version": row.review_version,
        "review": produced.row.as_dict() if produced.row else None,
        "note": (
            "the earlier version is unchanged and still readable. Section 44: a "
            "regeneration adds a version rather than replacing one."
        ),
    }


@router.get(
    "/trade-reviews",
    response_model=Page[dict[str, Any]],
    summary="Reviews, newest first",
    description=(
        "Filterable by trade, environment, status and outcome. Every row carries "
        "its environment; paper and live are never merged."
    ),
)
async def list_reviews(
    trade_id: str | None = Query(None, max_length=36),
    environment: str | None = Query(None, description="paper | demo | live"),
    status: str | None = Query(None, description="COMPLETED | FAILED | PENDING | ..."),
    outcome: str | None = Query(None, description="WIN | LOSS | BREAKEVEN"),
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> Page[dict[str, Any]]:
    stmt = select(TradeReviewRow)
    if trade_id:
        await _owned_trade(db, user, trade_id)
        stmt = stmt.where(TradeReviewRow.trade_id == trade_id)
    if environment:
        stmt = stmt.where(TradeReviewRow.environment == environment)
    if status:
        stmt = stmt.where(TradeReviewRow.status == status)
    if outcome:
        stmt = stmt.where(TradeReviewRow.outcome == outcome)
    stmt = stmt.order_by(TradeReviewRow.created_at.desc())
    rows, info = await paginate(db, stmt, params)
    return Page(items=[row.as_dict() for row in rows], page=info)


@router.get(
    "/trade-reviews/patterns",
    summary="Shapes across many trades, with their sample sizes",
    description=(
        "Grouped by recorded attributes and read from L32 analytics -- no metric "
        "is recomputed here. Every observation carries its trade count, and one "
        "below the sample floor is labelled insufficient rather than reported as "
        "a pattern. Observations, never actions."
    ),
)
async def patterns(
    environment: str | None = Query(None, description="paper | demo | live"),
    account_id: str | None = Query(None, max_length=36),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    if account_id:
        owned = await db.scalar(
            select(PaperAccount.id).where(
                PaperAccount.id == account_id, PaperAccount.user_id == user.id
            )
        ) or await db.scalar(
            select(BrokerAccount.id).where(
                BrokerAccount.id == account_id, BrokerAccount.user_id == user.id
            )
        )
        if owned is None:
            raise NotFound("no such account for this user")
    return await _PATTERNS.across(db, environment=environment, account_id=account_id)


@router.get(
    "/trade-reviews/summary",
    summary="How many trades have been reviewed, and how they came out",
    description=(
        "Counts by status and outcome. A count of FAILED reviews is a fact "
        "about the review pipeline, not about the trades."
    ),
)
async def summary(
    environment: str | None = Query(None, description="paper | demo | live"),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    from sqlalchemy import func

    stmt = select(TradeReviewRow.status, func.count(TradeReviewRow.id))
    if environment:
        stmt = stmt.where(TradeReviewRow.environment == environment)
    by_status = {
        name: int(count)
        for name, count in (await db.execute(stmt.group_by(TradeReviewRow.status))).all()
    }

    outcome_stmt = select(TradeReviewRow.outcome, func.count(TradeReviewRow.id)).where(
        TradeReviewRow.status == "COMPLETED"
    )
    if environment:
        outcome_stmt = outcome_stmt.where(TradeReviewRow.environment == environment)
    by_outcome = {
        (name or "unrecorded"): int(count)
        for name, count in (await db.execute(outcome_stmt.group_by(TradeReviewRow.outcome))).all()
    }

    reviewable = select(func.count(Trade.id)).where(Trade.status.in_(sorted(REVIEWABLE_STATUSES)))
    if environment:
        reviewable = reviewable.where(Trade.mode == environment)

    return {
        "environment": environment,
        "by_status": by_status,
        "by_outcome": by_outcome,
        "reviews": sum(by_status.values()),
        "reviewable_trades": int(await db.scalar(reviewable) or 0),
        "sweep_ceiling": MAX_PER_SWEEP,
        "dimensions": list(DIMENSIONS),
        "note": (
            "a FAILED review is a fact about the review pipeline, not about the trade. "
            "The trade completed regardless -- review generation is asynchronous and "
            "never blocks a close."
        ),
    }


@router.post(
    "/trade-reviews/sweep",
    summary="Review completed trades that have none",
    description=(
        "Bounded by a ceiling, because reviews must not be generated without "
        "one. Idempotent: a trade that already has a review is skipped."
    ),
)
async def sweep(
    environment: str | None = Query(None, description="paper | demo | live"),
    limit: int = Query(10, ge=1, le=MAX_PER_SWEEP),
    db: AsyncSession = Depends(get_db),
    user: User = _REGENERATE,
) -> dict[str, Any]:
    try:
        produced = await _SERVICE.review_pending(db, environment=environment, limit=limit)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValidationFailed(str(exc)) from exc
    await db.commit()
    return {
        "generated": sum(1 for p in produced if p.created),
        "skipped": sum(1 for p in produced if not p.created),
        "results": [p.as_dict() for p in produced],
        "ceiling": MAX_PER_SWEEP,
    }


__all__ = ["router"]
