"""The Position Sizing Engine's surface: calculate, modes, status.

Three properties this router is built around.

**Nothing here executes, and nothing here approves.** There is no route that
creates an order, and the calculate route deliberately runs the Risk Engine's
*side-effect-free* preview afterwards, so a caller sees the same veto the
pipeline would apply without anything being reserved, latched or recorded. A
200 from this endpoint is a quantity and a risk opinion, never a permission.

**Broker metadata is read from the platform's own contract spec**, through
`app.symbols.contract_spec`, which is the same source the paper engine and the
broker layer use. This router does not talk to MT5 and holds no adapter, so it
cannot become a second symbol-metadata system.

**The caller's numbers are inputs, not instructions.** Entry, stop and risk
arrive from an untrusted client and are validated by the deterministic engine
exactly as a TradingView payload would be. The one thing a client cannot do is
raise its own ceiling: `max_risk_amount` is clamped to the account's
configured `max_risk_per_trade` when the Risk Engine states one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, get_db
from app.auth.models import User
from app.core.errors import ValidationFailed
from app.risk.engine import OrderProposal, PortfolioState
from app.risk.service import RiskRequest, RiskService
from app.sizing.calculator import (
    METHOD_ALIASES,
    RISK_METHODS,
    SizingError,
    SizingMethod,
    SizingRequest,
    resolve_method,
)
from app.sizing.service import SizingService
from app.symbols.errors import SymbolError
from app.symbols.service import contract_spec

router = APIRouter(prefix="/position-sizing", tags=["position-sizing"])

_VIEW = Depends(current_user)


def _sizing(request: Request) -> SizingService:
    service: SizingService | None = getattr(request.app.state, "sizing", None)
    if service is None:  # pragma: no cover - wired in main.py at startup
        service = SizingService()
        request.app.state.sizing = service
    return service


def _risk(request: Request) -> RiskService | None:
    return getattr(request.app.state, "risk", None)


# ==================================================================== bodies


class CalculateBody(BaseModel):
    """One sizing request. `sizing_mode` accepts the brief's aliases."""

    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    side: Annotated[str, Field(pattern="^(buy|sell|long|short)$")]
    sizing_mode: str = "percent_equity"

    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    # Accepted for callers that carry a distance rather than a level. When
    # both are given they must agree; the engine refuses a disagreement
    # rather than choosing one.
    stop_distance: Decimal | None = Field(default=None, gt=0)

    quantity: Decimal | None = Field(default=None, gt=0)
    risk_amount: Decimal | None = Field(default=None, gt=0)
    equity: Decimal | None = Field(default=None, gt=0)
    risk_percent: Decimal | None = Field(default=None, gt=0, le=100)

    account_id: str | None = None
    strategy_id: str | None = None
    # Advisory only. A client cannot raise its own ceiling: the Risk Engine's
    # configured max_risk_per_trade wins whenever it is lower.
    max_risk_amount: Decimal | None = Field(default=None, gt=0)


# ================================================================= calculate


@router.post(
    "/calculate",
    summary="Calculate a position size. Creates no order and grants no permission",
    description=(
        "Deterministic: the same body always returns the same quantity. The "
        "response also carries the Risk Engine's side-effect-free opinion of "
        "the sized order, so a caller sees the veto it would meet — but "
        "nothing is reserved, latched, recorded or executed here."
    ),
)
async def calculate_size(
    body: CalculateBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _VIEW,
) -> dict[str, Any]:
    try:
        method = resolve_method(body.sizing_mode)
    except SizingError as exc:
        raise ValidationFailed(str(exc)) from exc

    try:
        spec = await contract_spec(db, body.symbol)
    except SymbolError as exc:
        # A missing or unsynced spec is a refusal with a reason, not a 500.
        # Sizing against a defaulted tick value is the failure this whole
        # module exists to prevent.
        raise ValidationFailed(str(exc)) from exc

    risk = _risk(request)
    ceiling = await _ceiling(db, risk, body)

    result = _sizing(request).size(
        SizingRequest(
            method=method,
            spec=spec,
            quantity=body.quantity,
            risk_amount=body.risk_amount,
            equity=body.equity,
            risk_percent=body.risk_percent,
            stop_distance=body.stop_distance,
            side=body.side,
            entry_price=body.entry_price,
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
            max_risk_amount=ceiling,
        ),
        context="api",
    )

    payload: dict[str, Any] = {
        **result.as_dict(),
        "requested_mode": body.sizing_mode,
        "risk_ceiling_applied": str(ceiling) if ceiling is not None else None,
        "authority": (
            "This is a proposed quantity. It is not an approval and it is not an "
            "order. app.risk decides whether the order may exist."
        ),
    }

    # The Risk Engine's opinion of the sized order, previewed. Only meaningful
    # when there is a quantity to evaluate.
    if result.ok and risk is not None:
        payload["risk"] = await _preview(db, risk, body, result.volume)
    elif result.ok:  # pragma: no cover - risk is wired at startup
        payload["risk"] = {"available": False, "note": "the risk service is not loaded"}
    else:
        payload["risk"] = {
            "evaluated": False,
            "note": "no quantity was produced, so there was nothing to evaluate",
        }
    return payload


async def _ceiling(
    db: AsyncSession, risk: RiskService | None, body: CalculateBody
) -> Decimal | None:
    """The lower of what the caller asked for and what the account permits.

    The configured figure is resolved through `RiskService.limits_for`, which
    combines the account, strategy and symbol layers and returns the most
    restrictive. A client stating a larger `max_risk_amount` than its account
    allows therefore gets the account's figure, never its own — the request is
    an input, not an instruction.
    """
    configured: Decimal | None = None
    if risk is not None:
        try:
            resolved = await risk.limits_for(
                db,
                account_id=body.account_id,
                strategy_id=body.strategy_id,
                symbol=body.symbol.upper(),
            )
            configured = resolved.limits.max_risk_per_trade
        except Exception:  # noqa: BLE001
            # A configuration that will not load must not become a larger
            # permitted risk. The caller's own ceiling stands, and the Risk
            # Engine still vetoes below.
            configured = None
    stated = body.max_risk_amount
    if configured is None:
        return stated
    if stated is None:
        return configured
    return min(stated, configured)


async def _preview(
    db: AsyncSession, risk: RiskService, body: CalculateBody, volume: Decimal | None
) -> dict[str, Any]:
    proposal = OrderProposal(
        symbol=body.symbol.upper(),
        side="buy" if body.side in ("buy", "long") else "sell",
        mode=risk.trading_mode,
        volume=volume,
        entry_price=body.entry_price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        account_id=body.account_id,
        strategy_id=body.strategy_id,
    )
    decision = await risk.check(
        db,
        RiskRequest(
            proposal=proposal,
            portfolio=PortfolioState(equity=body.equity),
            execution_mode=risk.trading_mode,
            order_type="market",
        ),
    )
    return {
        **decision.as_dict(),
        "preview": True,
        "note": "Nothing was reserved, latched or recorded. This is not an approval.",
    }


# ===================================================================== modes


@router.get("/modes", summary="The sizing modes, their inputs, and the aliases accepted")
async def modes(user: User = _VIEW) -> dict[str, Any]:
    return {
        "modes": [
            {
                "mode": str(SizingMethod.fixed_quantity),
                "requires": ["quantity"],
                "what": "an explicit volume, checked against the venue's step and bounds",
                "note": (
                    "This is also 'fixed lot'. On every instrument this platform trades, "
                    "MT5 included, the quantity IS the lot, so a second mode would be a "
                    "second name for one calculation."
                ),
            },
            {
                "mode": str(SizingMethod.fixed_risk),
                "requires": ["risk_amount", "stop_loss or stop_distance"],
                "what": "a fixed sum of account currency at the stop",
            },
            {
                "mode": str(SizingMethod.percent_equity),
                "requires": ["equity", "risk_percent", "stop_loss or stop_distance"],
                "what": "a fraction of measured equity at the stop",
            },
        ],
        "aliases": {alias: str(method) for alias, method in sorted(METHOD_ALIASES.items())},
        "risk_modes": sorted(str(m) for m in RISK_METHODS),
        "atr_sizing": (
            "Not a separate mode. ATR sizing is stop-distance sizing over a stop the "
            "caller derived from ATR, which is what the paper engine's bracket and the "
            "backtester already produce. A separate mode would put that bracket "
            "calculation in two places."
        ),
        "broker_constraints": (
            "Applied to every result, not selected as a mode. The venue maximum binds "
            "downward and is applied with a warning because it reduces risk; the venue "
            "minimum binds upward and REFUSES, because raising a size to the minimum "
            "risks more than was budgeted."
        ),
        "rounding": (
            "Down, always, to the volume step — one implementation, shared with the "
            "broker layer and the OMS (app.symbols.precision.floor_to_step)."
        ),
    }


# ==================================================================== status


@router.get("/status", summary="Sizing counters, latency and the engine's stated authority")
async def status(request: Request, user: User = _VIEW) -> dict[str, Any]:
    return _sizing(request).status()
