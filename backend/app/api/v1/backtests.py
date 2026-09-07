"""Backtest routes: queue a run, read its status, its trades and its curve.

Results are owned. A user sees their own runs, scoped in the query, and someone
else's answers exactly as one that does not exist.

**A run never blocks the request.** Queueing returns immediately with
`status: queued`; the work happens in a background task. Polling the same id
reports `queued`, `running`, `finished`, `failed` or `cancelled` — and a failed run
is never reported as finished, which is the distinction that makes a result
trustworthy. (`finished` is the L05 vocabulary for what the brief calls
COMPLETED; they name the same state.)

**Every assumption is in the response.** `params` carries the whole
configuration including the cost model, the slippage, the execution semantics
and a fingerprint. Two identical configurations over the same data produce the
same fingerprint, so a stored result can be checked against the configuration
that claims to have produced it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, page_params, paginate
from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.backtest.config import BacktestConfig, ConfigError, CostModel, SizingMode
from app.backtest.service import BacktestBusy, BacktestService
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.marketdata.types import Provider, TimeframeError, parse_timeframe
from app.models.research import Backtest, BacktestTrade
from app.strategies.registry import UnknownStrategy

router = APIRouter(prefix="/backtests", tags=["backtests"])

_RUN = Depends(require_permission(Permission.run_backtests))


class CostsIn(BaseModel):
    """No zero default. A caller states the spread or the request is refused.

    Cost drag is the one effect this project has measured that is large enough
    to see, so a backtest that assumed zero would be measuring something else.
    """

    spread_points: Decimal = Field(ge=0)
    commission_per_trade: Decimal = Field(default=Decimal("0"), ge=0)
    slippage_points: Decimal = Field(default=Decimal("0"), ge=0)
    swap_long_per_night: Decimal | None = None
    swap_short_per_night: Decimal | None = None


class BacktestIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    strategy_key: Annotated[str, Field(min_length=1, max_length=64)]
    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    timeframe: str = "H1"
    provider: str = "mt5"
    costs: CostsIn
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    start: datetime | None = None
    end: datetime | None = None
    initial_capital: Decimal = Field(default=Decimal("100000"), gt=0)
    quantity: Decimal = Field(default=Decimal("0.01"), gt=0)
    # Position sizing (L18). `fixed_quantity` uses `quantity` and is the
    # default, so an existing caller's body means exactly what it meant
    # before. The risk modes size every trade through `app.sizing` against
    # the equity as it stood before that trade.
    sizing_mode: str = Field(default="fixed_quantity")
    risk_amount: Decimal | None = Field(default=None, gt=0)
    risk_percent: Decimal | None = Field(default=None, gt=0, le=100)
    stop_atr: Decimal = Field(default=Decimal("1.5"), gt=0)
    target_atr: Decimal = Field(default=Decimal("1.5"), gt=0)
    max_hold_bars: int = Field(default=240, ge=1, le=10_000)
    max_bars: int = Field(default=3000, ge=100, le=200_000)


def _service(request: Request) -> BacktestService:
    service: BacktestService = request.app.state.backtests
    return service


def _config(body: BacktestIn) -> BacktestConfig:
    try:
        timeframe = parse_timeframe(body.timeframe)
    except TimeframeError as exc:
        raise ValidationFailed(str(exc)) from exc
    try:
        provider = Provider(body.provider)
    except ValueError as exc:
        raise ValidationFailed(f"unknown provider {body.provider!r}") from exc
    try:
        sizing_mode = SizingMode(body.sizing_mode)
    except ValueError as exc:
        known = ", ".join(m.value for m in SizingMode)
        raise ValidationFailed(
            f"unknown sizing_mode {body.sizing_mode!r}; known modes are {known}"
        ) from exc

    config = BacktestConfig(
        strategy_key=body.strategy_key,
        symbol=body.symbol.upper(),
        timeframe=timeframe,
        provider=provider,
        costs=CostModel(
            spread_points=body.costs.spread_points,
            commission_per_trade=body.costs.commission_per_trade,
            slippage_points=body.costs.slippage_points,
            swap_long_per_night=body.costs.swap_long_per_night,
            swap_short_per_night=body.costs.swap_short_per_night,
        ),
        strategy_config=body.strategy_config,
        start=body.start,
        end=body.end,
        initial_capital=body.initial_capital,
        sizing_mode=sizing_mode,
        quantity=body.quantity,
        risk_amount=body.risk_amount,
        risk_percent=body.risk_percent,
        stop_atr=body.stop_atr,
        target_atr=body.target_atr,
        max_hold_bars=body.max_hold_bars,
        max_bars=body.max_bars,
    )
    try:
        config.validate()
    except ConfigError as exc:
        raise ValidationFailed(str(exc)) from exc
    return config


async def _owned(db: AsyncSession, user: User, backtest_id: str) -> Backtest:
    row = await db.scalar(
        select(Backtest).where(Backtest.id == backtest_id, Backtest.requested_by_user_id == user.id)
    )
    if row is None:
        raise NotFound(f"no backtest {backtest_id} for this user")
    return row


@router.post(
    "",
    status_code=202,
    summary="Queue a backtest",
    description=(
        "Returns immediately with `status: queued`; the run happens in the "
        "background. **`spread_points` is required** and has no default: cost "
        "drag is the one effect this project has measured large enough to see, "
        "so a run that assumed zero would be measuring something else. Every "
        "assumption is echoed back in `params`, with a fingerprint that two "
        "identical configurations share."
    ),
)
async def create(
    body: BacktestIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _RUN,
) -> dict[str, object]:
    config = _config(body)
    service = _service(request)
    try:
        row = await service.queue(db, config, user.id, body.name)
    except UnknownStrategy as exc:
        raise NotFound(str(exc)) from exc
    except BacktestBusy as exc:
        raise Conflict(str(exc)) from exc
    except ConfigError as exc:
        raise ValidationFailed(str(exc)) from exc
    return {
        "backtest_id": row.id,
        "status": row.status,
        "params": row.params,
        "note": (
            "Queued. Poll this id for the result; a failed run reports `failed` and "
            "is never shown as finished."
        ),
    }


@router.get("", summary="Backtests requested by the signed-in user")
async def list_mine(
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    user: User = _RUN,
) -> Page[dict]:
    statement = (
        select(Backtest)
        .where(Backtest.requested_by_user_id == user.id)
        .order_by(Backtest.created_at.desc())
    )
    rows, page = await paginate(db, statement, params)
    items = [
        {
            "backtest_id": r.id,
            "name": r.name,
            "status": r.status,
            "engine": r.engine,
            "timeframe": r.timeframe,
            "universe": r.universe,
            "created_at": r.created_at.isoformat(),
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "trades": (r.summary or {}).get("metrics", {}).get("trades"),
        }
        for r in rows
    ]
    return Page[dict](items=items, page=page)


@router.get(
    "/{backtest_id}",
    summary="One backtest: status, assumptions and results if it finished",
    description=(
        "A run that has not finished carries no metrics. `NOT RUN`, `RUNNING` "
        "and `FAILED` are reported as themselves rather than as an empty "
        "result, because an empty result reads as 'no trades' and that is a "
        "different statement."
    ),
)
async def get_one(
    backtest_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _RUN,
) -> dict[str, object]:
    row = await _owned(db, user, backtest_id)
    summary = row.summary or {}
    return {
        "backtest_id": row.id,
        "name": row.name,
        "status": row.status,
        "engine": row.engine,
        "params": row.params,
        "error": row.error,
        "created_at": row.created_at.isoformat(),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "metrics": summary.get("metrics"),
        "quality": summary.get("quality"),
        "signals_generated": summary.get("signals_generated"),
        "bars_processed": summary.get("bars_processed"),
        "fingerprint": summary.get("fingerprint"),
        "note": (
            "A simulation under the stated assumptions, not a prediction. No broker "
            "was contacted and no order was submitted."
        ),
    }


@router.get(
    "/{backtest_id}/trades",
    summary="The simulated trades",
    description="Every trade the run produced, with its exit reason.",
)
async def trades(
    backtest_id: str,
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    user: User = _RUN,
) -> Page[dict]:
    await _owned(db, user, backtest_id)
    statement = (
        select(BacktestTrade)
        .where(BacktestTrade.backtest_id == backtest_id)
        .order_by(BacktestTrade.entry_time.asc())
    )
    rows, page = await paginate(db, statement, params)
    items = [
        {
            "side": r.side,
            "entry_time": r.entry_time.isoformat(),
            "exit_time": r.exit_time.isoformat(),
            "entry_price": str(r.entry_price),
            "exit_price": str(r.exit_price),
            "net_points": str(r.pnl_points) if r.pnl_points is not None else None,
            "exit_reason": r.exit_reason,
        }
        for r in rows
    ]
    return Page[dict](items=items, page=page)


@router.get(
    "/{backtest_id}/equity-curve",
    summary="Balance, equity and drawdown over the run",
)
async def equity_curve(
    backtest_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _RUN,
) -> dict[str, object]:
    row = await _owned(db, user, backtest_id)
    summary = row.summary or {}
    curve = summary.get("equity_curve")
    if curve is None:
        # Never an empty list: "no curve because it has not run" and "a flat
        # curve" are different statements.
        return {
            "backtest_id": backtest_id,
            "status": row.status,
            "curve": None,
            "reason": f"the run is {row.status}; no equity curve exists yet",
        }
    return {"backtest_id": backtest_id, "status": row.status, "curve": curve}


@router.post(
    "/{backtest_id}/cancel",
    summary="Cancel a queued or running backtest",
)
async def cancel(
    backtest_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _RUN,
) -> dict[str, object]:
    row = await _owned(db, user, backtest_id)
    if row.status not in ("queued", "running"):
        raise Conflict(f"backtest is {row.status} and cannot be cancelled")
    cancelled = await _service(request).cancel(backtest_id)
    return {"backtest_id": backtest_id, "cancelled": cancelled}


@router.get(
    "/engine/assumptions",
    summary="What this engine assumes, stated once",
    description=(
        "The execution semantics are fixed, not configurable: they are what "
        "`tools/rule_backtest.simulate` implements and what the toolkit's own "
        "tests pin. Reported so a reader knows what a number means."
    ),
)
async def assumptions(_: User = Depends(current_user)) -> dict[str, object]:
    from app.backtest.config import ENGINE_VERSION, ExecutionModel

    return {
        "engine_version": ENGINE_VERSION,
        "execution": ExecutionModel().describe(),
        "sizing": {
            "implemented": ["fixed_quantity", "fixed_risk", "percent_equity"],
            "engine": (
                "app.sizing.calculate — the same engine the paper pipeline uses. The "
                "backtester defines no sizing formula of its own, so a backtested size "
                "and a live size cannot drift apart."
            ),
            "equity": (
                "For the risk modes each trade is sized against the balance from trades "
                "that had already CLOSED, and the stop distance is stop_atr x the ATR of "
                "the bar BEFORE the entry — the same figure simulate() used to place the "
                "bracket. Neither input is knowable later than the entry, which is what "
                "keeps the walk free of look-ahead."
            ),
            "refusals": (
                "An entry the engine cannot size is not a trade: it is removed from the "
                "result and reported under `sizing_refusals`, because counting it would "
                "report a return the account could not have earned."
            ),
            "requires": (
                "The risk modes need the instrument's measured contract spec. A run "
                "without one is refused, never sized at a default lot."
            ),
        },
        "limitations": [
            "The bracket comes from ATR multiples, not from a strategy's suggested stop or target.",
            "Exit rules from a built strategy are not executed; positions exit on the "
            "stop, the target or the hold limit.",
            "One symbol per run. Multi-symbol and multi-timeframe are not implemented "
            "and are not faked.",
            "Bid/ask is not modelled: only OHLC is available, so the spread model "
            "stands in for the book.",
            "Sharpe and Sortino are per trade, not annualised: annualising needs a "
            "trades-per-year figure a fixed window does not supply.",
        ],
    }
