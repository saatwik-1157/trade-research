"""What is held, what it is worth, and how sure we are of the answer.

**Nothing here executes.** Section 63. This router imports no order manager, no
sizing calculator, no risk decision and no broker WRITE path; every verb is
GET. A test parses the module to keep that true, in the same shape L27 and L29
already use, because a docstring saying "this cannot place an order" is not
evidence that it cannot.

**Nothing here is fabricated.** Section 59. A broker with no connected adapter
answers with every figure `null` and a reason attached, not with the last values
seen. A position whose contract size was never measured is reported as
uncomputable rather than counted as zero. A portfolio nobody reconciled says so
rather than saying it agrees.

**Paper and live are never pooled.** Section 41. The account id decides which
system is asked, `environment` is carried on every response, and the two live in
different tables -- so a query that forgot to filter cannot reach across.

**No credential is served.** Section 55. `AccountState` has no field for a
login, a server password or an API key; `from_broker_account` copies the
broker's balance and equity and nothing else, so there is nothing here to
redact.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, page_params, paginate
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import NotFound
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.journal import PortfolioSnapshot
from app.portfolio import exposure as exposure_engine
from app.portfolio.service import PortfolioService, PortfolioView

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

_READ = Depends(require_permission(Permission.view_portfolio))

#: One service instance. It holds two configuration values and no state, so a
#: per-request one would allocate for nothing.
_SERVICE = PortfolioService()


# ============================================================ authorization


async def _owned(db: AsyncSession, user: User, account_id: str) -> tuple[str, str]:
    """The account and its environment, or 404. Sections 41 and 55.

    Scoped in the QUERY, not after it -- `WHERE user_id = :me` cannot leak
    through a bug in a filter written later.

    A missing account and somebody else's account give the same 404 message,
    deliberately. Distinguishing them turns this route into a membership
    oracle, which is the reasoning `channels.py` already records.
    """
    paper = await db.scalar(
        select(PaperAccount.id).where(
            PaperAccount.id == account_id, PaperAccount.user_id == user.id
        )
    )
    if paper is not None:
        return account_id, "paper"
    broker = await db.scalar(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id, BrokerAccount.user_id == user.id
        )
    )
    if broker is not None:
        return account_id, broker.account_mode
    raise NotFound("no such account for this user")


def _adapter(request: Request, account_id: str, environment: str) -> Any | None:
    """The connected broker adapter, or None. Section 31.

    None is the honest answer for a paper account (there is no venue) and for a
    live one whose terminal is not connected. The portfolio engine turns that
    into an `unavailable` state rather than into a stale figure.
    """
    if environment == "paper":
        return None
    registry = getattr(request.app.state, "brokers", None)
    if registry is None:
        return None
    try:
        return registry.get(account_id)
    except Exception:  # noqa: BLE001 - "not connected" is a state, not an error
        return None


async def _view(request: Request, db: AsyncSession, user: User, account_id: str) -> PortfolioView:
    """One complete view, assembled from whichever systems can answer."""
    resolved, environment = await _owned(db, user, account_id)
    adapter = _adapter(request, resolved, environment)
    account = await _SERVICE.account_state_for(
        db, account_id=resolved, environment=environment, adapter=adapter
    )
    positions = await _SERVICE.positions_for(db, account_id=resolved, environment=environment)
    prices, _unpriced = await _SERVICE.marks_for(positions, adapter=adapter)
    reconciliation = await _SERVICE.reconcile_for(
        db, account_id=resolved, environment=environment, adapter=adapter
    )
    return await _SERVICE.build(db, account=account, reconciliation=reconciliation, prices=prices)


_ACCOUNT = Annotated[str, Query(description="A paper or broker account you own.")]


# =================================================================== routes


@router.get(
    "",
    summary="The whole portfolio for one account",
    description=(
        "Balance, equity, margin, exposure, P&L, drawdown, reconciliation and "
        "health, assembled from the systems that own each piece. Every figure "
        "names its source. An unavailable figure is null and says why; it is "
        "never zero and never the last value seen."
    ),
)
async def portfolio(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    return (await _view(request, db, user, account_id)).as_dict()


@router.get(
    "/summary",
    summary="Balance, equity, margin, exposure and drawdown",
    description=(
        "The headline figures only. Same data as the full view, shaped for a "
        "dashboard header. `health` and `health_reasons` say whether the "
        "numbers can be read as current."
    ),
)
async def summary(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    view = await _view(request, db, user, account_id)
    return {
        "at": view.at.isoformat(),
        "environment": view.account.environment,
        "health": str(view.health),
        "health_reasons": view.health_reasons,
        "freshness": str(view.account.freshness),
        "account": view.account.as_dict(),
        "pnl": view.pnl.as_dict() if view.pnl else None,
        "drawdown": view.drawdown.as_dict() if view.drawdown else None,
        "margin": view.margin.as_dict() if view.margin else None,
        "exposure": (
            {
                "gross": str(view.exposure.total.gross),
                "net": str(view.exposure.total.net),
                "positions": view.exposure.total.positions,
                "uncomputable": view.exposure.total.uncomputable,
            }
            if view.exposure
            else None
        ),
        "position_count": len(view.positions),
    }


@router.get(
    "/account",
    summary="Balance, equity and margin as their owner reports them",
    description=(
        "The broker's own figures for a live or demo account, the paper "
        "engine's for a paper one. Not recomputed here: deriving equity as "
        "balance plus unrealized would double-count whenever the source "
        "already had."
    ),
)
async def account(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    resolved, environment = await _owned(db, user, account_id)
    adapter = _adapter(request, resolved, environment)
    state = await _SERVICE.account_state_for(
        db, account_id=resolved, environment=environment, adapter=adapter
    )
    return state.as_dict()


@router.get(
    "/positions",
    summary="Open positions, marked where a live price exists",
    description=(
        "The `positions` table (L21), joined to the instrument metadata that "
        "makes a notional computable. A position with no live quote is "
        "returned unmarked rather than valued at its entry price, which would "
        "be a stale figure presented as a current one."
    ),
)
async def positions(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    view = await _view(request, db, user, account_id)
    return {
        "environment": view.account.environment,
        "positions": [
            {
                "position_id": p.position_id,
                "symbol": p.symbol,
                "side": p.side,
                "quantity": str(p.quantity),
                "entry_price": str(p.entry_price),
                "current_price": None if p.current_price is None else str(p.current_price),
                "unrealized_pnl": None if p.unrealized_pnl is None else str(p.unrealized_pnl),
                "stop_loss": None if p.stop_loss is None else str(p.stop_loss),
                "take_profit": None if p.take_profit is None else str(p.take_profit),
                "strategy_id": p.strategy_id,
                "bot_id": p.bot_id,
                "asset_class": p.asset_class,
                "notional": exposure_engine.notional_of(p).as_dict(),
            }
            for p in view.positions
        ],
        "count": len(view.positions),
        "unmarked": [p.symbol for p in view.positions if p.current_price is None],
    }


@router.get(
    "/exposure",
    summary="Gross and net exposure, by symbol, strategy, bot, class and currency",
    description=(
        "Gross is total absolute exposure and net is the directional "
        "difference: long 50,000 and short 30,000 is 80,000 gross and +20,000 "
        "net. Both are carried on every aggregate because a report showing one "
        "where the other was meant understates the position by more than half."
    ),
)
async def exposure(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    view = await _view(request, db, user, account_id)
    return {
        "environment": view.account.environment,
        "exposure": view.exposure.as_dict() if view.exposure else None,
        "open_risk": view.open_risk,
        "correlation": exposure_engine.correlation_note(),
    }


@router.get("/by-symbol", summary="Exposure grouped by instrument")
async def by_symbol(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    return await _grouped(request, db, user, account_id, "by_symbol")


@router.get("/by-strategy", summary="Exposure grouped by the strategy version that opened it")
async def by_strategy(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    return await _grouped(request, db, user, account_id, "by_strategy")


@router.get(
    "/by-bot",
    summary="Exposure grouped by bot",
    description=(
        "Attribution runs through the Bot Manager's own link -- "
        "`bots.paper_account_id` -- because no execution table carries a bot "
        "id. A position whose bot cannot be identified is grouped as "
        "`unattributed` rather than guessed at."
    ),
)
async def by_bot(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    return await _grouped(request, db, user, account_id, "by_bot")


async def _grouped(
    request: Request, db: AsyncSession, user: User, account_id: str, attribute: str
) -> dict[str, Any]:
    view = await _view(request, db, user, account_id)
    if view.exposure is None:
        return {"environment": view.account.environment, "groups": {}, "total": None}
    buckets: dict[str, exposure_engine.Bucket] = getattr(view.exposure, attribute)
    return {
        "environment": view.account.environment,
        "grouped_by": attribute.removeprefix("by_"),
        "groups": {key: bucket.as_dict() for key, bucket in buckets.items()},
        "total": view.exposure.total.as_dict(),
    }


@router.get(
    "/pnl",
    summary="Realized, unrealized and today, with what each is made of",
    description=(
        "Realized comes from the trade journal -- what a venue confirmed -- and "
        "unrealized from open positions valued at their current mark. The two "
        "sets are disjoint, so the total is their sum. The trading-day boundary "
        "is the one the risk engine already uses; a second definition would "
        "reintroduce the timezone bug it exists to avoid."
    ),
)
async def pnl(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    view = await _view(request, db, user, account_id)
    return {
        "environment": view.account.environment,
        "pnl": view.pnl.as_dict() if view.pnl else None,
    }


@router.get(
    "/drawdown",
    summary="Peak, current and maximum drawdown",
    description=(
        "The peak is read from the recorded equity history and only ever "
        "rises. A peak recomputed from a rolling window would fall as the "
        "window passed an old high, and the drawdown would shrink without the "
        "account recovering."
    ),
)
async def drawdown(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    from app.portfolio.events import DRAWDOWN_STEPS

    view = await _view(request, db, user, account_id)
    return {
        "environment": view.account.environment,
        "drawdown": view.drawdown.as_dict() if view.drawdown else None,
        "thresholds": {
            "steps": [str(step) for step in DRAWDOWN_STEPS],
            "note": (
                "display thresholds for the drawdown alert, not risk limits. The "
                "RISK ENGINE holds the limits that stop a trade."
            ),
        },
    }


@router.get(
    "/margin",
    summary="Used margin against equity",
    description=(
        "Margin utilisation is not portfolio risk. It says how much of the "
        "account the broker is holding against open positions, which is a "
        "different question from how much could be lost. Reported, never "
        "enforced."
    ),
)
async def margin(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    view = await _view(request, db, user, account_id)
    return {
        "environment": view.account.environment,
        "margin": view.margin.as_dict() if view.margin else None,
    }


@router.get(
    "/reconciliation",
    summary="Whether internal state and the broker agree",
    description=(
        "Compared, never repaired: settling a discrepancy is the position "
        "reconciler's job, behind its own route. `checked: false` means no "
        "reconciliation has run, which is not the same as agreement."
    ),
)
async def reconciliation(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    resolved, environment = await _owned(db, user, account_id)
    adapter = _adapter(request, resolved, environment)
    found = await _SERVICE.reconcile_for(
        db, account_id=resolved, environment=environment, adapter=adapter
    )
    return {"environment": environment, "reconciliation": found.as_dict()}


@router.get(
    "/history",
    response_model=Page[dict[str, Any]],
    summary="Recorded portfolio snapshots, newest first",
    description=(
        "The equity history the drawdown is computed from. Written by the "
        "platform when balance and equity could both be read; a view whose "
        "account could not be read is not recorded, because a snapshot of "
        "nothing would enter the curve as a real point."
    ),
)
async def history(
    account_id: _ACCOUNT,
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> Page[dict[str, Any]]:
    resolved, environment = await _owned(db, user, account_id)
    stmt = (
        select(PortfolioSnapshot)
        .where(
            PortfolioSnapshot.mode == environment,
            (PortfolioSnapshot.broker_account_id == resolved)
            | (PortfolioSnapshot.paper_account_id == resolved),
        )
        .order_by(PortfolioSnapshot.taken_at.desc())
    )
    rows, info = await paginate(db, stmt, params)
    return Page(
        items=[
            {
                "taken_at": row.taken_at.isoformat(),
                "mode": row.mode,
                "balance": str(row.balance),
                "equity": str(row.equity),
                "margin": None if row.margin is None else str(row.margin),
                "free_margin": None if row.free_margin is None else str(row.free_margin),
                "open_positions": row.open_positions,
                "drawdown_pct": None if row.drawdown_pct is None else float(row.drawdown_pct),
                "exposure": row.exposure,
            }
            for row in rows
        ],
        page=info,
    )


@router.get(
    "/risk-state",
    summary="The fields the risk engine reads from this portfolio",
    description=(
        "Shown so an operator can see what the risk engine is working from. "
        "Every value may be null, and a null a limit needs is treated as a "
        "VETO rather than an assumption -- so a stale or unreadable portfolio "
        "makes trading more conservative, never less. The portfolio engine "
        "supplies this; it does not decide anything with it."
    ),
)
async def risk_state(
    request: Request,
    account_id: _ACCOUNT,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    view = await _view(request, db, user, account_id)
    state = view.to_risk_state()
    return {
        "environment": view.account.environment,
        "risk_state": {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in state.items()
            if key not in ("open_symbols", "exposure_by_currency")
        },
        "open_symbols": sorted(state["open_symbols"]),
        "exposure_by_currency": {
            k: str(v) for k, v in (state["exposure_by_currency"] or {}).items()
        },
        "authority": (
            "the RISK ENGINE decides. This is the input it reads, published so "
            "the reasoning behind a veto is visible."
        ),
    }


__all__ = ["router"]
