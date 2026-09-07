"""The Risk Engine's surface: preview, status, configuration, decisions, switches.

The paths and gates were fixed by L06's pending table (`/risk/rules` and
`/risk/events`, `manage_risk_settings`, level 17). This module fills in the
body those rows promised.

Two properties this router is built around.

**`POST /risk/check` is side-effect free by construction.** It calls
`RiskService.check`, which never reserves, never latches a lock and never
writes an event. It is a different method from `evaluate` rather than the same
method with a flag, so a preview cannot become an execution by passing the
wrong argument.

**Nothing here executes.** There is no route that creates an order. The Risk
Engine answers "is this allowed?"; the OMS is what acts on the answer, and it
is reached through `/v1/paper-trading`, never from here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import Conflict, ValidationFailed
from app.models.risk import RiskEvent, RiskRule
from app.risk.config import ConfigurationInvalid, Scope, describe_precedence
from app.risk.decision import RejectionCode, RiskOutcome
from app.risk.engine import LimitKind, OrderProposal, PortfolioState, RiskLimits
from app.risk.service import CODE_FOR, RiskRequest, RiskService
from app.risk.state import NEEDS_AUTHORISED_RESET, ResetNotPermitted, RiskState

router = APIRouter(prefix="/risk", tags=["risk"])

_VIEW = Depends(current_user)
_MANAGE = Depends(require_permission(Permission.manage_risk_settings))


def _service(request: Request) -> RiskService:
    service: RiskService = request.app.state.risk
    return service


# ==================================================================== bodies


class LimitsBody(BaseModel):
    """Every limit is optional. `None` means this layer does not speak to it,
    and a limit no layer sets is reported as NOT ENFORCED on every decision."""

    max_risk_per_trade: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_exposure_per_currency: Decimal | None = None
    max_open_positions: int | None = None
    max_trades_per_day: int | None = None
    max_trades_per_hour: int | None = None
    max_trades_per_minute: int | None = None
    max_leverage: Decimal | None = None
    max_position_size: Decimal | None = None
    max_concentration_pct: Decimal | None = None
    max_margin_utilisation_pct: Decimal | None = None
    min_risk_reward: Decimal | None = None
    cooldown_seconds: int | None = None
    market_data_max_age_seconds: float | None = None
    max_spread_points: Decimal | None = None
    max_signal_age_seconds: float | None = None
    one_position_per_symbol: bool | None = None
    require_stop_loss: bool | None = None
    require_market_open: bool | None = None

    def stated(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}

    def to_limits(self) -> RiskLimits:
        return RiskLimits(**self.stated())


class ConfigBody(BaseModel):
    scope: Scope
    scope_ref: str | None = None
    name: Annotated[str, Field(min_length=1, max_length=100)]
    limits: LimitsBody


class CheckBody(BaseModel):
    """A preview request. Nothing here can create an order."""

    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    side: str = Field(pattern="^(buy|sell)$")
    quantity: Decimal = Field(gt=0)
    account_id: str | None = None
    strategy_id: str | None = None
    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    risk_amount: Decimal | None = Field(default=None, ge=0)
    spread_points: Decimal | None = Field(default=None, ge=0)
    order_type: str = Field(default="market", pattern="^(market|limit|stop)$")
    # The portfolio the caller wants the decision computed against. A field
    # left out is UNKNOWN, and a limit that needs an unknown fact vetoes rather
    # than assuming a comfortable value.
    equity: Decimal | None = None
    balance: Decimal | None = None
    open_positions: int | None = None
    realised_today: Decimal | None = None
    trades_today: int | None = None
    peak_equity: Decimal | None = None
    exposure: Decimal | None = None
    base_currency: str = "USD"


class KillSwitchBody(BaseModel):
    scope: Scope
    scope_ref: str | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ClearLockBody(BaseModel):
    account_id: str | None = None
    confirm: bool = Field(description="Must be true. A lock never clears implicitly.")
    reason: Annotated[str, Field(min_length=1, max_length=500)]


# =================================================================== preview


@router.post(
    "/check",
    summary="Preview a risk decision. Executes nothing",
    description=(
        "Side-effect free by construction: it calls a different method from the "
        "one that reserves and records, not the same method with a flag. It "
        "creates no order, reserves no exposure, latches no lock and writes no "
        "event."
    ),
)
async def check(
    body: CheckBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _VIEW,
) -> dict[str, object]:
    service = _service(request)
    proposal = OrderProposal(
        symbol=body.symbol.upper(),
        side=body.side,
        mode=service.trading_mode,
        volume=body.quantity,
        entry_price=body.entry_price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        risk_amount=body.risk_amount,
        spread_points=body.spread_points,
        account_id=body.account_id,
        strategy_id=body.strategy_id,
        base_currency=body.base_currency,
    )
    portfolio = PortfolioState(
        equity=body.equity,
        balance=body.balance,
        open_positions=body.open_positions,
        realised_today=body.realised_today,
        trades_today=body.trades_today,
        peak_equity=body.peak_equity,
        exposure_by_currency=(
            {body.base_currency: body.exposure} if body.exposure is not None else {}
        ),
    )
    decision = await service.check(
        db,
        RiskRequest(
            proposal=proposal,
            portfolio=portfolio,
            execution_mode=service.trading_mode,
            order_type=body.order_type,
        ),
    )
    return {
        **decision.as_dict(),
        "preview": True,
        "note": "No order was created. This endpoint evaluates and nothing else.",
    }


# ==================================================================== status


@router.get(
    "/status",
    summary="Kill switches, latched locks, outstanding approvals, failure mode",
)
async def status(request: Request, user: User = _VIEW) -> dict[str, object]:
    return _service(request).status()


@router.get(
    "/limits",
    summary="Every limit the engine can enforce, and the precedence rule",
    description=(
        "The catalogue is the engine's own `LimitKind`, so this cannot drift "
        "from what actually runs."
    ),
)
async def limits(request: Request, user: User = _VIEW) -> dict[str, object]:
    from dataclasses import fields as dc_fields

    return {
        "checks": [str(k) for k in LimitKind],
        "configurable_limits": [f.name for f in dc_fields(RiskLimits)],
        "rejection_codes": [str(c) for c in RejectionCode],
        "outcomes": [str(o) for o in RiskOutcome],
        "codes_by_check": {str(k): str(v) for k, v in CODE_FOR.items()},
        "precedence": describe_precedence(),
        "locks": {
            "states": [str(s) for s in RiskState],
            "authorised_reset_required": [str(s) for s in NEEDS_AUTHORISED_RESET],
            "daily_loss": "clears at the configured trading-day boundary and only then",
        },
    }


# ============================================================= configuration


@router.get("/config", summary="The stored risk configuration layers")
async def get_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
    account_id: str | None = None,
    strategy_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, object]:
    service = _service(request)
    rows = (
        await db.scalars(
            select(RiskRule).where(RiskRule.rule_type == "limits", RiskRule.is_enabled)
        )
    ).all()
    resolved = await service.configuration(
        db, account_id=account_id, strategy_id=strategy_id, symbol=symbol
    )
    return {
        "layers": [
            {
                "id": r.id,
                "name": r.name,
                "scope": r.scope,
                "scope_ref": r.scope_ref,
                "version": r.priority,
                "limits": r.params,
            }
            for r in rows
        ],
        "effective": resolved.as_dict(),
    }


@router.put(
    "/config",
    summary="Store one configuration layer",
    description=(
        "Validated before it is written, never after. A percentage above 100, a "
        "negative cap or a zero where zero would disable a check is refused "
        "rather than clamped: silently normalising an unsafe configuration "
        "produces a system trading under limits nobody chose. Storing a layer "
        "increments its version; historical decisions keep the version they "
        "were made under."
    ),
)
async def put_config(
    body: ConfigBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
) -> dict[str, object]:
    service = _service(request)
    try:
        row = await service.set_limits(
            db,
            scope=body.scope,
            scope_ref=body.scope_ref,
            limits=body.limits.stated(),
            name=body.name,
            actor=user.id,
        )
    except ConfigurationInvalid as exc:
        raise ValidationFailed(str(exc)) from exc
    await db.commit()
    return {
        "id": row.id,
        "scope": row.scope,
        "scope_ref": row.scope_ref,
        "version": row.priority,
        "limits": row.params,
    }


# ================================================================= decisions


@router.get(
    "/decisions",
    summary="Recorded risk decisions, approvals included",
    description=(
        "A veto that leaves no trace is indistinguishable from a check that "
        "never ran, so approvals are recorded too. Historical rows are never "
        "rewritten when the configuration changes."
    ),
)
async def decisions(
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
    account_id: str | None = None,
    decision: str | None = Query(default=None, pattern="^(approve|veto|halt)$"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    query = select(RiskEvent).order_by(RiskEvent.occurred_at.desc()).limit(limit)
    if account_id:
        query = query.where(RiskEvent.paper_account_id == account_id)
    if decision:
        query = query.where(RiskEvent.decision == decision)
    rows = (await db.scalars(query)).all()
    return {
        "decisions": [
            {
                "id": r.id,
                "decision_id": r.decision_id,
                "decision": r.decision,
                "reason": r.reason,
                "occurred_at": r.occurred_at.isoformat(),
                "paper_account_id": r.paper_account_id,
                "mode": r.mode,
                "configuration_version": r.configuration_version,
                "request_hash": r.request_hash,
                "snapshot": r.snapshot,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/events", summary="Alias of /risk/decisions, kept for the L06 contract")
async def events(
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    # `decision=None` explicitly: an unpassed FastAPI parameter is a Query
    # object, and binding one as a SQL parameter is a type error.
    return await decisions(db=db, user=user, account_id=None, decision=None, limit=limit)


@router.get("/rules", summary="Every stored rule: limits, kill switches, latched states")
async def rules(db: AsyncSession = Depends(get_db), user: User = _MANAGE) -> dict[str, object]:
    rows = (await db.scalars(select(RiskRule).order_by(RiskRule.priority))).all()
    return {
        "rules": [
            {
                "id": r.id,
                "name": r.name,
                "scope": r.scope,
                "scope_ref": r.scope_ref,
                "rule_type": r.rule_type,
                "params": r.params,
                "is_enabled": r.is_enabled,
                "version": r.priority,
            }
            for r in rows
        ],
        "count": len(rows),
    }


# ============================================================ kill switches


@router.post(
    "/kill-switch",
    summary="Engage a kill switch",
    description=(
        "Blocks new orders in the given scope. **Positions are preserved and "
        "never closed automatically** — closing on a kill switch would be "
        "trading a decision nobody made, at the moment something is known to be "
        "wrong. Persisted, so a restart comes back with it engaged."
    ),
)
async def engage(
    body: KillSwitchBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
) -> dict[str, object]:
    report = await _service(request).engage_kill_switch(
        db, scope=body.scope, scope_ref=body.scope_ref, reason=body.reason, actor=user.id
    )
    await db.commit()
    return {**report, "status": _service(request).status()}


@router.delete("/kill-switch", summary="Release a kill switch")
async def release(
    request: Request,
    scope: Scope,
    scope_ref: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
) -> dict[str, object]:
    report = await _service(request).release_kill_switch(
        db, scope=scope, scope_ref=scope_ref, actor=user.id
    )
    await db.commit()
    return {**report, "status": _service(request).status()}


@router.post(
    "/locks/clear",
    summary="Clear a latched risk lock. Authorised, named and recorded",
    description=(
        "A drawdown lock, an emergency stop and a disabled account do not clear "
        "on their own — a drawdown is still there tomorrow. A daily-loss lock "
        "clears at the trading-day boundary without anyone doing anything, so "
        "it rarely needs this. Every release records who authorised it."
    ),
)
async def clear_lock(
    body: ClearLockBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
) -> dict[str, object]:
    if not body.confirm:
        raise ValidationFailed(
            "clearing a risk lock re-enables trading and must be confirmed explicitly"
        )
    try:
        state = await _service(request).clear_lock(
            db, account_id=body.account_id, authorised_by=f"{user.id}:{body.reason[:100]}"
        )
    except ResetNotPermitted as exc:
        raise Conflict(str(exc)) from exc
    await db.commit()
    return state.as_dict()
