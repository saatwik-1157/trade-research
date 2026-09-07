"""The PAPER venue: accounts, orders, bots, kill switches.

The path and the permission were fixed by L06's pending table
(`/paper-trading/accounts`, `start_paper_bots`, level 16). This module fills in
the body the table promised; it does not invent a second surface.

Accounts are owned. Someone else's answers exactly as one that does not exist,
because telling a caller that an id is taken by another user is a membership
oracle.

**Every order route runs through the Risk Engine**, manual ones included. There
is no branch that skips it: `PaperOMS.submit` requires a `risk.Approval`, and
the only thing that constructs one is `RiskEngine.approve`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.backtests import CostsIn
from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.backtest.config import CostModel
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.settings import Settings, get_settings
from app.marketdata.types import Provider, TimeframeError, parse_timeframe
from app.models.accounts import PaperAccount
from app.models.bots import Bot, BotRun
from app.models.execution import Order, Position, Trade
from app.models.market import Symbol
from app.paper.portfolio import AccountState, IllegalAccountTransition
from app.paper.router import ExecutionMode, provider_for
from app.paper.router import describe as describe_routing
from app.paper.service import BotNotRunnable, PaperBusy, PaperService
from app.risk.engine import RiskLimits
from app.security.enforce import require_step_up
from app.security.stepup import StepUpScope
from app.sizing.calculator import SizingMethod
from app.strategies.registry import UnknownStrategy
from app.symbols.errors import SymbolError

router = APIRouter(prefix="/paper-trading", tags=["paper-trading"])

_PAPER = Depends(require_permission(Permission.start_paper_bots))
_ADMIN = Depends(require_permission(Permission.manage_risk_settings))


# ==================================================================== bodies


class AccountIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    currency: str = "USD"
    # Configuration-driven, with no default that could be mistaken for a
    # measured account size.
    starting_balance: Decimal = Field(gt=0, le=Decimal("1000000000"))


class ResetIn(BaseModel):
    confirm: bool = Field(description="Must be true. A reset is never implicit.")
    starting_balance: Decimal | None = Field(default=None, gt=0)


class LimitsIn(BaseModel):
    """The seven named limits. None disables one, and a disabled limit is
    reported in every decision so `not enforced` never reads as `passed`."""

    max_risk_per_trade: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_exposure_per_currency: Decimal | None = None
    max_open_positions: int | None = None
    max_trades_per_day: int | None = None
    max_leverage: Decimal | None = None
    one_position_per_symbol: bool = True
    require_stop_loss: bool = True
    max_spread_points: Decimal | None = None
    max_signal_age_seconds: float | None = 300.0

    def to_limits(self) -> RiskLimits:
        return RiskLimits(**self.model_dump())


class BotIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    paper_account_id: str
    strategy_key: Annotated[str, Field(min_length=1, max_length=64)]
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    timeframe: str = "H1"
    provider: str = "mt5"
    # The same cost model the backtester and replay use, so a paper result and
    # a backtest of the same strategy are priced by one set of assumptions.
    costs: CostsIn
    limits: LimitsIn = Field(default_factory=LimitsIn)
    sizing_method: str = "fixed_quantity"
    quantity: Decimal | None = Field(default=Decimal("0.01"), gt=0)
    risk_amount: Decimal | None = Field(default=None, gt=0)
    risk_percent: Decimal | None = Field(default=None, gt=0, le=100)


class ManualOrderIn(BaseModel):
    """A hand-placed paper order. It passes the Risk Engine like any other."""

    paper_account_id: str
    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    side: str = Field(pattern="^(buy|sell)$")
    quantity: Decimal = Field(gt=0)
    order_type: str = Field(default="market", pattern="^(market|limit|stop)$")
    price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    provider: str = "mt5"
    costs: CostsIn
    limits: LimitsIn = Field(default_factory=LimitsIn)


class KillSwitchIn(BaseModel):
    scope: str = Field(pattern="^(global|account|strategy|bot)$")
    target: str | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class EmergencyStopIn(BaseModel):
    confirm: bool
    reason: Annotated[str, Field(min_length=1, max_length=500)]


# =================================================================== helpers


def _service(request: Request) -> PaperService:
    service: PaperService = request.app.state.paper
    return service


async def _account(request: Request, db: AsyncSession, account_id: str, user: User) -> PaperAccount:
    try:
        return await _service(request).get_account(db, account_id, user.id)
    except KeyError as exc:
        raise NotFound(f"no paper account {account_id} for this user") from exc


def _out(row: PaperAccount) -> dict[str, object]:
    return {
        "account_id": row.id,
        "name": row.name,
        "currency": row.currency,
        "status": row.status,
        "is_active": row.is_active,
        "starting_balance": str(row.starting_balance),
        "balance": str(row.balance),
        "equity": str(row.equity),
        "realized_pnl": str(row.realized_pnl),
        "unrealized_pnl": str(row.unrealized_pnl),
        "total_pnl": str(row.realized_pnl + row.unrealized_pnl),
        "commission_paid": str(row.commission_paid),
        "reset_at": row.reset_at.isoformat() if row.reset_at else None,
        "reset_count": row.reset_count,
        "created_at": row.created_at.isoformat(),
        "execution_mode": "PAPER",
        "execution_provider": str(provider_for(ExecutionMode.paper)),
        "note": "Virtual capital. No real money is involved at any point.",
    }


def _costs(body: CostsIn) -> CostModel:
    return CostModel(
        spread_points=body.spread_points,
        commission_per_trade=body.commission_per_trade,
        slippage_points=body.slippage_points,
        swap_long_per_night=body.swap_long_per_night,
        swap_short_per_night=body.swap_short_per_night,
    )


# ================================================================== accounts


@router.post(
    "/accounts",
    status_code=201,
    summary="Create a paper account",
    description=(
        "Starts at `created`, which is deliberately not tradeable: an account "
        "that has never been activated is not a venue. Activate it before "
        "starting a bot."
    ),
)
async def create_account(
    body: AccountIn, request: Request, db: AsyncSession = Depends(get_db), user: User = _PAPER
) -> dict[str, object]:
    try:
        row = await _service(request).create_account(
            db,
            user_id=user.id,
            name=body.name,
            currency=body.currency,
            starting_balance=body.starting_balance,
        )
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.get("/accounts", summary="Paper accounts belonging to this user")
async def list_accounts(
    db: AsyncSession = Depends(get_db),
    user: User = _PAPER,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    rows = (
        await db.scalars(
            select(PaperAccount)
            .where(PaperAccount.user_id == user.id)
            .order_by(PaperAccount.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {"accounts": [_out(r) for r in rows], "count": len(rows)}


@router.get("/accounts/{account_id}", summary="One paper account")
async def get_account(
    request: Request, account_id: str, db: AsyncSession = Depends(get_db), user: User = _PAPER
) -> dict[str, object]:
    return _out(await _account(request, db, account_id, user))


def _lifecycle(name: str, wanted: AccountState, summary: str, description: str = ""):  # noqa: ANN202
    @router.post(f"/accounts/{{account_id}}/{name}", summary=summary, description=description)
    async def handler(  # noqa: ANN202
        request: Request,
        account_id: str,
        db: AsyncSession = Depends(get_db),
        user: User = _PAPER,
    ) -> dict[str, object]:
        row = await _account(request, db, account_id, user)
        try:
            await _service(request).move_account(db, row, wanted)
        except IllegalAccountTransition as exc:
            raise Conflict(str(exc)) from exc
        await db.commit()
        await db.refresh(row)
        return _out(row)

    return handler


_lifecycle(
    "activate",
    AccountState.active,
    "Activate: the account may now accept orders",
)
_lifecycle(
    "pause",
    AccountState.paused,
    "Pause: no new orders",
    "Open positions are preserved and can still be closed. Any bot trading "
    "this account is stopped, because an account that cannot accept orders "
    "with a bot still running is a contradiction the next pass would have to "
    "resolve by accident.",
)
_lifecycle("resume", AccountState.active, "Resume a paused account")
_lifecycle("disable", AccountState.disabled, "Disable: only closure follows")
_lifecycle("close", AccountState.closed, "Close permanently. Terminal.")


@router.post(
    "/accounts/{account_id}/reset",
    summary="Reset the virtual capital. Explicit, never silent",
    description=(
        "Requires `confirm: true`. Stops any bot on the account, marks open "
        "positions closed-by-reset, and restores the configured initial "
        "capital. **Nothing is deleted**: prior orders, executions and trades "
        "are archived in place, because a trading record that can vanish "
        "cannot be audited."
    ),
)
async def reset_account(
    body: ResetIn,
    request: Request,
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _PAPER,
) -> dict[str, object]:
    if not body.confirm:
        raise ValidationFailed(
            "a reset discards the account's statistics and must be confirmed explicitly"
        )
    row = await _account(request, db, account_id, user)
    report = await _service(request).reset_account(db, row, starting_balance=body.starting_balance)
    await db.commit()
    await db.refresh(row)
    return {**_out(row), "reset": report}


# =============================================== the record, filtered by mode


async def _symbol_codes(db: AsyncSession, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = (await db.scalars(select(Symbol).where(Symbol.id.in_(ids)))).all()
    return {r.id: r.code for r in rows}


@router.get(
    "/accounts/{account_id}/orders",
    summary="Paper orders for this account",
    description="Every row is `mode='paper'`. A paper order cannot appear in a live query.",
)
async def account_orders(
    request: Request,
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _PAPER,
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, object]:
    await _account(request, db, account_id, user)
    rows = (
        await db.scalars(
            select(Order)
            .where(Order.paper_account_id == account_id, Order.mode == "paper")
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
    ).all()
    codes = await _symbol_codes(db, {r.symbol_id for r in rows})
    return {
        "execution_mode": "PAPER",
        "orders": [
            {
                "order_id": r.id,
                "intent_id": r.intent_id,
                "symbol": codes.get(r.symbol_id),
                "side": r.side,
                "order_type": r.order_type,
                "quantity": str(r.quantity),
                "requested_price": str(r.requested_price) if r.requested_price else None,
                "stop_loss": str(r.stop_loss) if r.stop_loss else None,
                "take_profit": str(r.take_profit) if r.take_profit else None,
                "status": r.status,
                "mode": r.mode,
                "sizing": r.sizing,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/accounts/{account_id}/positions", summary="Open paper positions")
async def account_positions(
    request: Request,
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _PAPER,
) -> dict[str, object]:
    await _account(request, db, account_id, user)
    rows = (
        await db.scalars(
            select(Position).where(
                Position.paper_account_id == account_id,
                Position.mode == "paper",
                Position.status == "open",
            )
        )
    ).all()
    codes = await _symbol_codes(db, {r.symbol_id for r in rows})
    return {
        "execution_mode": "PAPER",
        "positions": [
            {
                "position_id": r.id,
                "symbol": codes.get(r.symbol_id),
                "side": r.side,
                "quantity": str(r.quantity),
                "entry_price": str(r.entry_price),
                "stop_loss": str(r.stop_loss) if r.stop_loss else None,
                "take_profit": str(r.take_profit) if r.take_profit else None,
                "opened_at": r.opened_at.isoformat(),
                "mode": r.mode,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get(
    "/accounts/{account_id}/trades",
    summary="Closed paper trades — the journal's unit of account",
)
async def account_trades(
    request: Request,
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _PAPER,
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, object]:
    await _account(request, db, account_id, user)
    rows = (
        await db.scalars(
            select(Trade)
            .where(Trade.mode == "paper", Trade.source == "pipeline")
            .order_by(Trade.closed_at.desc())
            .limit(limit)
        )
    ).all()
    codes = await _symbol_codes(db, {r.symbol_id for r in rows})
    return {
        "execution_mode": "PAPER",
        "trades": [
            {
                "trade_id": r.id,
                "symbol": codes.get(r.symbol_id),
                "side": r.side,
                "volume": str(r.volume),
                "entry_price": str(r.entry_price),
                "exit_price": str(r.exit_price),
                "opened_at": r.opened_at.isoformat(),
                "closed_at": r.closed_at.isoformat(),
                "gross_profit": str(r.gross_profit),
                "commission": str(r.commission),
                "net_profit": str(r.net_profit),
                "mode": r.mode,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get(
    "/accounts/{account_id}/pnl",
    summary="Balance, equity, exposure and drawdown",
    description=(
        "From the account row, which the bot writes in the same transaction as "
        "the fill. It is never recomputed from the trade table on read: two "
        "derivations of one fact can disagree, and then neither is trustworthy."
    ),
)
async def account_pnl(
    request: Request,
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _PAPER,
) -> dict[str, object]:
    row = await _account(request, db, account_id, user)
    service = _service(request)
    live = [b for b in service.live.values() if b.account_id == account_id]
    return {
        **_out(row),
        "active_bots": [b.snapshot()["bot_id"] for b in live],
        "live_state": live[0].engine.state() if live else None,
    }


# ====================================================================== bots


@router.post(
    "/bots",
    status_code=201,
    summary="Create a paper bot",
    description=(
        "The bot's configuration is frozen when it starts. Editing it changes "
        "what the NEXT run uses; a running bot cannot change strategy, symbol "
        "or limits mid-flight, because that would make its own trade record "
        "unreadable."
    ),
)
async def create_bot(
    body: BotIn, request: Request, db: AsyncSession = Depends(get_db), user: User = _PAPER
) -> dict[str, object]:
    service = _service(request)
    try:
        await service.get_account(db, body.paper_account_id, user.id)
    except KeyError as exc:
        raise NotFound(f"no paper account {body.paper_account_id} for this user") from exc
    try:
        service.registry.implementation(body.strategy_key)  # type: ignore[attr-defined]
    except UnknownStrategy as exc:
        raise NotFound(str(exc)) from exc

    row = Bot(
        user_id=user.id,
        name=body.name,
        # Never anything but paper from this router. The mode is not a field a
        # caller can set, so a paper bot cannot be created as a live one.
        mode="paper",
        paper_account_id=body.paper_account_id,
        is_enabled=False,
        config={
            "strategy_key": body.strategy_key,
            "config": body.strategy_config,
            "symbol": body.symbol.upper(),
            "timeframe": body.timeframe,
            "provider": body.provider,
            "costs": body.costs.model_dump(mode="json"),
            "limits": body.limits.model_dump(mode="json"),
            "sizing_method": body.sizing_method,
            "quantity": str(body.quantity) if body.quantity is not None else None,
            "risk_amount": str(body.risk_amount) if body.risk_amount is not None else None,
            "risk_percent": str(body.risk_percent) if body.risk_percent is not None else None,
        },
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "bot_id": row.id,
        "name": row.name,
        "mode": row.mode,
        "execution_mode": "PAPER",
        "paper_account_id": row.paper_account_id,
        "config": row.config,
        "is_enabled": row.is_enabled,
        "status": "created",
    }


@router.get("/bots", summary="Paper bots belonging to this user")
async def list_bots(
    request: Request, db: AsyncSession = Depends(get_db), user: User = _PAPER
) -> dict[str, object]:
    rows = (await db.scalars(select(Bot).where(Bot.user_id == user.id, Bot.mode == "paper"))).all()
    live = _service(request).live
    return {
        "bots": [
            {
                "bot_id": r.id,
                "name": r.name,
                "mode": r.mode,
                "paper_account_id": r.paper_account_id,
                "is_enabled": r.is_enabled,
                "config": r.config,
                "status": live[r.id].status if r.id in live else "stopped",
            }
            for r in rows
        ],
        "count": len(rows),
        "execution_mode": "PAPER",
    }


@router.post(
    "/bots/{bot_id}/start",
    summary="Start a paper bot in the background",
    description=(
        "Runs in a backend task. Closing the browser does not stop it: the "
        "frontend controls a backend bot, it does not host one. On start the "
        "bot recovers any open positions and every order intent already "
        "recorded, so a restart cannot duplicate an order."
    ),
)
async def start_bot(
    request: Request, bot_id: str, db: AsyncSession = Depends(get_db), user: User = _PAPER
) -> dict[str, object]:
    row = await db.get(Bot, bot_id)
    if row is None or row.user_id != user.id:
        raise NotFound(f"no paper bot {bot_id} for this user")
    config = row.config or {}
    try:
        timeframe = parse_timeframe(str(config.get("timeframe", "H1")))
    except TimeframeError as exc:
        raise ValidationFailed(str(exc)) from exc
    try:
        provider = Provider(str(config.get("provider", "mt5")))
    except ValueError as exc:
        raise ValidationFailed(f"unknown provider {config.get('provider')!r}") from exc

    costs = _costs(CostsIn(**(config.get("costs") or {})))
    limits = LimitsIn(**(config.get("limits") or {})).to_limits()

    def _dec(key: str) -> Decimal | None:
        value = config.get(key)
        return Decimal(str(value)) if value is not None else None

    try:
        bot = await _service(request).start_bot(
            db,
            bot=row,
            user_id=user.id,
            symbol=str(config.get("symbol", "")),
            timeframe=timeframe,
            provider=provider,
            costs=costs,
            limits=limits,
            sizing_method=SizingMethod(str(config.get("sizing_method", "fixed_quantity"))),
            quantity=_dec("quantity"),
            risk_amount=_dec("risk_amount"),
            risk_percent=_dec("risk_percent"),
        )
    except PaperBusy as exc:
        raise Conflict(str(exc)) from exc
    except BotNotRunnable as exc:
        raise Conflict(str(exc)) from exc
    except UnknownStrategy as exc:
        raise NotFound(str(exc)) from exc
    except SymbolError as exc:
        raise ValidationFailed(str(exc)) from exc
    return bot.snapshot()


def _bot_control(name: str, summary: str):  # noqa: ANN202
    @router.post(f"/bots/{{bot_id}}/{name}", summary=summary)
    async def handler(request: Request, bot_id: str, user: User = _PAPER) -> dict[str, object]:  # noqa: ANN202
        service = _service(request)
        try:
            bot = service.get_bot(bot_id, user.id)
        except KeyError as exc:
            raise NotFound(f"no running paper bot {bot_id} for this user") from exc
        try:
            await getattr(service, f"{name}_bot")(bot)
        except BotNotRunnable as exc:
            raise Conflict(str(exc)) from exc
        return bot.snapshot()

    return handler


_bot_control("pause", "Pause a running bot. It stops before its next pass.")
_bot_control("resume", "Resume a paused bot from exactly where it stopped.")
_bot_control("stop", "Stop a bot. Open positions are preserved, never auto-closed.")


@router.get("/bots/{bot_id}", summary="Live bot state: account, positions, counters")
async def bot_state(request: Request, bot_id: str, user: User = _PAPER) -> dict[str, object]:
    try:
        return _service(request).get_bot(bot_id, user.id).snapshot()
    except KeyError as exc:
        raise NotFound(f"no running paper bot {bot_id} for this user") from exc


@router.get(
    "/bots/{bot_id}/passes",
    summary="Recent pipeline passes, refusals included",
    description=(
        "Every pass is recorded with its outcome, including the ones that "
        "created no order. A veto, a refusal from sizing and a stale feed are "
        "three different faults with three different fixes, so they are three "
        "different outcomes rather than one 'rejected'."
    ),
)
async def bot_passes(
    request: Request, bot_id: str, user: User = _PAPER, limit: int = Query(50, ge=1, le=200)
) -> dict[str, object]:
    try:
        bot = _service(request).get_bot(bot_id, user.id)
    except KeyError as exc:
        raise NotFound(f"no running paper bot {bot_id} for this user") from exc
    return {
        "bot_id": bot_id,
        "execution_mode": "PAPER",
        "passes": [p.as_dict() for p in bot.recent[-limit:]],
    }


@router.get(
    "/bots/{bot_id}/runs",
    summary="Run history for this bot",
)
async def bot_runs(
    bot_id: str, db: AsyncSession = Depends(get_db), user: User = _PAPER
) -> dict[str, object]:
    row = await db.get(Bot, bot_id)
    if row is None or row.user_id != user.id:
        raise NotFound(f"no paper bot {bot_id} for this user")
    runs = (
        await db.scalars(
            select(BotRun).where(BotRun.bot_id == bot_id).order_by(BotRun.started_at.desc())
        )
    ).all()
    return {
        "bot_id": bot_id,
        "runs": [
            {
                "run_id": r.id,
                "status": r.status,
                "started_at": r.started_at.isoformat(),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "stop_reason": r.stop_reason,
                "summary": r.summary,
            }
            for r in runs
        ],
        "count": len(runs),
    }


# =========================================================== kill switches


@router.post(
    "/kill-switch",
    summary="Engage a kill switch",
    description=(
        "Stops new orders in the given scope and stops the bots it covers. "
        "**Positions are preserved for review and never closed automatically** "
        "— closing on a kill switch would be trading a decision nobody made, "
        "at a price nobody chose, at the exact moment something is known to be "
        "wrong."
    ),
)
async def engage_kill_switch(
    body: KillSwitchIn, request: Request, user: User = _ADMIN
) -> dict[str, object]:
    try:
        report = await _service(request).engage_kill_switch(
            scope=body.scope, target=body.target, reason=body.reason
        )
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return {**report, "switches": _service(request).switch_state()}


@router.delete(
    "/kill-switch",
    summary="Release a kill switch",
    description=(
        "Requires re-authentication (L39): confirm at `POST /v1/security/step-up` "
        'with scope `KILL_SWITCH` and the subject `"{scope}:{target}"`, then '
        "repeat this request. Engaging a halt is cheap and releasing one lets "
        "orders flow again, so only the release is gated -- a control that is "
        "equally easy to set and to lift is one that gets lifted by whoever is "
        "in a hurry."
    ),
)
async def release_kill_switch(
    request: Request,
    scope: str = Query(pattern="^(global|account|strategy|bot)$"),
    target: str | None = None,
    user: User = _ADMIN,
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await require_step_up(
        request,
        scope=StepUpScope.kill_switch,
        subject=f"{scope}:{target or ''}",
        actor_id=user.id,
        settings=settings,
    )
    try:
        return await _service(request).release_kill_switch(scope=scope, target=target)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@router.get("/kill-switch", summary="Which kill switches are engaged")
async def kill_switch_state(request: Request, user: User = _PAPER) -> dict[str, object]:
    return _service(request).switch_state()


@router.post(
    "/emergency-stop",
    summary="Stop every paper bot immediately",
    description=(
        "Requires `confirm: true`. Prevents new orders and stops every running "
        "bot. Positions are preserved for review; nothing is closed."
    ),
)
async def emergency_stop(
    body: EmergencyStopIn, request: Request, user: User = _ADMIN
) -> dict[str, object]:
    if not body.confirm:
        raise ValidationFailed("an emergency stop must be confirmed explicitly")
    return await _service(request).engage_emergency_stop(body.reason)


# ============================================================== the model


@router.get(
    "/execution-model",
    summary="How paper execution works, and why it cannot reach a broker",
)
async def execution_model(request: Request, _: User = Depends(current_user)) -> dict[str, object]:
    from app.paper.execution import PaperExecution

    sample = PaperExecution(CostModel(spread_points=Decimal("0")))
    return {
        "execution_mode": "PAPER",
        "execution_provider": str(provider_for(ExecutionMode.paper)),
        "routing": describe_routing(),
        "fills": sample.describe(),
        "risk": (
            "Mandatory. `PaperOMS.submit` takes a `risk.Approval` as its first "
            "positional argument, and `RiskEngine.approve` is the only thing that "
            "constructs one. There is no branch that skips it, manual orders included."
        ),
        "ai": (
            "The AI seat runs BEFORE risk and can only decline. It cannot see the "
            "risk engine, the kill switches or the OMS, so there is no order in "
            "which an AI opinion could overturn a veto. No model is wired in yet; "
            "that is level 24."
        ),
        "day_boundary": (
            "UTC midnight by default, and configurable per engine. Never the "
            "machine's local midnight: this repository has already shipped a daily "
            "limit that counted from 18:30 on a UTC+5:30 laptop and from midnight "
            "on a UTC one."
        ),
        "market_data": (
            "L08's staleness check runs on every pass. A stale series blocks NEW "
            "orders and does not close anything, because closing on stale data is "
            "also trading on it."
        ),
        "idempotency": (
            "The signal key is a hash of (account, strategy, symbol, timeframe, bar "
            "time, signal type) and IS the order's intent id, which is UNIQUE on the "
            "orders table. A bar processed twice produces one order, and a restarted "
            "process rebuilds what it has already ordered from the database."
        ),
        "limitations": [
            "One symbol per bot. Multi-symbol bots are not implemented and are not faked.",
            "Market orders only; limit and stop orders are accepted by the schema "
            "but the engine submits market orders.",
            "Trailing stops are carried on the position but not yet advanced.",
            "Swap/financing is charged by the backtester but not yet by the paper "
            "engine, because a paper position's night count needs the bot to run "
            "across one.",
            "Notifications (L34) and monitoring (L37) are not wired; the events "
            "are published on the L07 bus and nothing subscribes yet.",
        ],
    }
