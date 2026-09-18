"""Positions the platform has recorded.

Read the description on the list route carefully, because the honest answer
here is narrower than the route name suggests. This serves the `positions`
table -- what the platform itself opened and tracked. It is **not** a read of
what the broker currently holds: that comes from a connected broker adapter,
which is L10, and a cache of positions presented as the broker's book is
exactly the failure `tools/mt5_account.py` exists to avoid.

The table is empty today, and an empty page here means "the platform has
recorded no positions", not "the account is flat". The two are different
claims and the response says which one it is making.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, page_params
from app.api.v1.schemas import PositionOut
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.execution import Position, PositionEvent
from app.models.market import Symbol
from app.oms.registry import NoOrderManager, OrderManagerRegistry
from app.positions import wiring
from app.positions.manager import MANAGEABLE, NOT_ACTIONABLE, PositionManager
from app.positions.monitor import PositionMonitor
from app.positions.policies import ExitDecision, ExitReason, MarketState
from app.positions.reconciler import PositionReconciler
from app.services import execution as svc

router = APIRouter(prefix="/positions", tags=["positions"])

_READ = Depends(require_permission(Permission.view_portfolio))
_CLOSE = Depends(require_permission(Permission.submit_orders))


@router.get(
    "",
    response_model=Page[PositionOut],
    summary="Positions the platform has recorded",
    description=(
        "The platform's own position record, not the broker's book. An empty "
        "page means the platform has recorded no positions; it does not mean "
        "the account is flat. Live broker positions require a connected broker "
        "adapter (L10) and are served by /v1/brokers when that exists."
    ),
)
async def list_positions(
    params: PageParams = Depends(page_params),
    mode: str | None = Query(None, description="paper | demo | live"),
    status: str | None = Query(None, description="open | closed | unknown"),
    side: str | None = Query(None, description="long | short"),
    symbol: str | None = Query(None, max_length=32),
    sort: str | None = Query(None, description="opened_at | closed_at | quantity | status"),
    order: str | None = Query(None, description="asc | desc"),
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> Page[PositionOut]:
    rows, page, codes = await svc.list_positions(
        db,
        params,
        mode=mode,
        status=status,
        side=side,
        symbol=symbol,
        sort=sort,
        order=order,
    )
    items = [
        PositionOut.model_validate(r, from_attributes=True).model_copy(
            update={"symbol": codes.get(r.symbol_id)}
        )
        for r in rows
    ]
    return Page[PositionOut](items=items, page=page)


class CloseBody(BaseModel):
    """A manual close. `quantity` omitted means all of it."""

    #: How much to take off. A partial close is refused if it exceeds what is
    #: open, rather than clamped: the difference between "close 30 of 70" and
    #: "close 30 of 100" is a position size nobody chose.
    quantity: Decimal | None = Field(default=None, gt=0)
    reason: Annotated[str, Field(min_length=1, max_length=300)] = "closed by operator"


class ProtectBody(BaseModel):
    """A change to a position's protective levels.

    Both are optional and at least one must be given. `stop_loss = null` does
    NOT remove a stop: removing protection is a separate, explicit action, and
    an API where a missing field could delete a stop is an API where a typo
    does.
    """

    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    reason: Annotated[str, Field(min_length=1, max_length=300)] = "modified by operator"


def _managers(request: Request) -> OrderManagerRegistry:
    registry: OrderManagerRegistry = request.app.state.order_managers
    return registry


async def _owned(db: AsyncSession, position_id: str) -> Position:
    row = await db.get(Position, position_id)
    if row is None:
        raise NotFound(f"no position {position_id}")
    return row


def _executor(request: Request, mode: str):  # noqa: ANN202
    """The executor for this position's mode. One implementation, in
    `app.positions.wiring`, so this route and the monitor cannot drift."""
    return wiring.executor_for(
        managers=_managers(request),
        # L45 C-2. The engine that mints the close's Approval. The OMS creates
        # nothing without one, so passing the application's own engine here is
        # what puts this route on the same path as every other order rather
        # than beside it.
        risk=request.app.state.risk.engine_for_close(),
        # L45 C-1, for closes. An order the venue may hold and the database has
        # never heard of is the state no guard can reason about.
        sessions=request.app.state.session_factory,
        mode=mode,
    )


@router.post(
    "/{position_id}/close",
    summary="Close a position, or part of one, and believe only the venue",
    description=(
        "A close is an order: paper goes to the simulator, demo and live go "
        "through the account's order manager to the broker adapter. The "
        "position is marked closed ONLY on a confirmed fill carrying a price. "
        "A rejection leaves it open with the reason recorded; an unclear "
        "answer parks it as `unknown` for reconciliation and nothing retries "
        "it, because retrying an uncertain close is how a position gets "
        "closed twice."
    ),
)
async def close_position(
    position_id: str,
    body: CloseBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _CLOSE,
) -> dict[str, Any]:
    row = await _owned(db, position_id)
    if row.status in NOT_ACTIONABLE:
        raise Conflict(NOT_ACTIONABLE[row.status])
    if row.status not in MANAGEABLE:
        raise Conflict(f"position {position_id} is {row.status} and cannot be closed")
    if body.quantity is not None and body.quantity > row.quantity:
        raise ValidationFailed(
            f"a close of {body.quantity} exceeds the {row.quantity} open on this "
            "position; refusing rather than clamping"
        )

    quote = await _quote(request, db, row)
    if quote is None:
        raise Conflict(
            f"no usable quote for {row.symbol_id}; refusing to close against a price "
            "the platform does not have"
        )

    manager = PositionManager(db, _executor(request, row.mode))
    decision = ExitDecision(
        # A full manual close is recorded as a strategy exit (an operator
        # deciding the trade is over); a partial one as a scale-out, so the
        # journal can tell a finished trade from a reduced one.
        reason=(
            ExitReason.strategy_exit if body.quantity is None else ExitReason.partial_take_profit
        ),
        detail=f"{body.reason} (user {user.id})",
        reference_price=quote.exit_price(row.side == "long"),
        decided_at=quote.as_of,
        quantity=body.quantity,
    )
    result = await manager.close_now(row, quote, decision)
    await db.commit()
    return {
        "position_id": row.id,
        "status": row.status,
        "quantity": str(row.quantity),
        "closed_quantity": str(row.closed_quantity or 0),
        "realized_pnl": str(row.realized_pnl) if row.realized_pnl is not None else None,
        "outcome": str(result.outcome.status) if result.outcome else None,
        "detail": result.outcome.detail if result.outcome else None,
        "needs_reconciliation": result.needs_reconciliation,
    }


@router.post(
    "/{position_id}/protect",
    summary="Change a position's stop or target. Never removes protection",
    description=(
        "Sets the levels this platform intends. A null field leaves that "
        "level alone: removing a protective stop is a separate and explicit "
        "action, because an API where a missing field deletes a stop is an "
        "API where a typo does. The change is recorded in `position_events` "
        "with who made it."
    ),
)
async def protect_position(
    position_id: str,
    body: ProtectBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _CLOSE,
) -> dict[str, Any]:
    if body.stop_loss is None and body.take_profit is None:
        raise ValidationFailed("give a stop_loss, a take_profit, or both")
    row = await _owned(db, position_id)
    if row.status in NOT_ACTIONABLE:
        raise Conflict(NOT_ACTIONABLE[row.status])
    if row.status not in MANAGEABLE:
        raise Conflict(f"position {position_id} is {row.status} and cannot be modified")

    is_long = row.side == "long"
    if body.stop_loss is not None:
        # A stop on the wrong side of the entry is not a wide stop, it is a
        # target. Refused rather than corrected, the same rule position sizing
        # applies at L18.
        wrong = body.stop_loss >= row.entry_price if is_long else body.stop_loss <= row.entry_price
        if wrong:
            raise ValidationFailed(
                f"a {row.side} stop must sit "
                f"{'below' if is_long else 'above'} its entry {row.entry_price}; "
                f"got {body.stop_loss}. Refused rather than corrected: that is a target"
            )
    if body.take_profit is not None:
        wrong = (
            body.take_profit <= row.entry_price if is_long else body.take_profit >= row.entry_price
        )
        if wrong:
            raise ValidationFailed(
                f"a {row.side} target must sit "
                f"{'above' if is_long else 'below'} its entry {row.entry_price}; "
                f"got {body.take_profit}"
            )

    before = {"stop_loss": row.stop_loss, "take_profit": row.take_profit}
    if body.stop_loss is not None:
        row.stop_loss = body.stop_loss
    if body.take_profit is not None:
        row.take_profit = body.take_profit
    db.add(
        PositionEvent(
            position_id=row.id,
            event_type="protection_set",
            occurred_at=datetime.now(UTC).replace(tzinfo=None),
            payload={
                "by_user": user.id,
                "reason": body.reason,
                "old_stop": str(before["stop_loss"]) if before["stop_loss"] is not None else None,
                "new_stop": str(row.stop_loss) if row.stop_loss is not None else None,
                "old_target": (
                    str(before["take_profit"]) if before["take_profit"] is not None else None
                ),
                "new_target": str(row.take_profit) if row.take_profit is not None else None,
                "note": (
                    "this is what the platform INTENDS. Whether the venue is holding it "
                    "is a separate fact, read by reconciliation."
                ),
            },
        )
    )
    await db.commit()
    return {
        "position_id": row.id,
        "stop_loss": str(row.stop_loss) if row.stop_loss is not None else None,
        "take_profit": str(row.take_profit) if row.take_profit is not None else None,
        "broker_stop_loss": (
            str(row.broker_stop_loss) if row.broker_stop_loss is not None else None
        ),
        "note": (
            "The levels this platform intends. Reconciliation reads what the venue is "
            "actually holding, and the two are kept separately on purpose."
        ),
    }


@router.post(
    "/reconcile",
    summary="Compare local positions against the venue and record every disagreement",
    description=(
        "Reads the venue, compares, and writes the findings. It repairs only "
        "what is unambiguous: a position the venue does not hold is closed "
        "locally, with the exit price recorded as UNKNOWN rather than "
        "invented. Everything else — a size or a level the venue reports "
        "differently — is recorded beside what we intended, so the "
        "disagreement stays visible instead of being resolved by overwriting "
        "one side. It opens, closes, cancels and modifies nothing at the venue."
    ),
)
async def reconcile(
    request: Request,
    account_id: str = Query(..., description="The account whose positions to settle"),
    db: AsyncSession = Depends(get_db),
    user: User = _CLOSE,
) -> dict[str, Any]:
    try:
        manager = _managers(request).get(account_id)
    except NoOrderManager as exc:
        raise Conflict(str(exc)) from exc
    reconciler = PositionReconciler(db, manager.adapter, mode=manager.mode)
    report = await reconciler.sweep(account_id)
    await db.commit()
    if report.failed:
        raise Conflict(report.failed)
    return report.as_dict()


async def _quote(request: Request, db: AsyncSession, row: Position) -> MarketState | None:
    """The current quote for a position's instrument, or None.

    None is a refusal, not a default. Closing against a price the platform
    does not have would be a decision made on a number nobody measured.

    **The venue first for a broker position**, because that is where it is
    held and where it will be closed; the normalized feed otherwise, and as
    the fallback when no adapter is reachable.

    Both halves live in `app.positions.wiring` now, because the position
    monitor has to price a position exactly as this route does and a second
    copy is a second answer. The feed half also had never worked: it called
    `service.get_quote(db, code)` where the method is
    `get_quote(self, db, internal_symbol, provider)` -- a TypeError,
    swallowed by a bare `except Exception` and returned as None -- and then
    read `quote.bid` although the method returns a `QuoteResult` whose quote
    is at `.quote`. Either defect alone made the fallback dead, so a
    position with no reachable venue could not be closed at all and the
    refusal read as "no usable quote" rather than as a bug.
    """
    symbol = await db.get(Symbol, row.symbol_id)
    code = symbol.code if symbol is not None else None
    if code is None:
        return None

    venue = await wiring.venue_quote_for(
        db, row, code, brokers=getattr(request.app.state, "brokers", None)
    )
    if venue is not None:
        return venue
    return await wiring.feed_quote_for(
        db, code, market_data=getattr(request.app.state, "market_data", None)
    )


# ================================================= the monitor's control plane
#
# Modelled on `app/api/v1/execution.py`, which is the house pattern for a
# worker's surface: status, start, stop, and a run-one-pass route beside the
# loop. `POST /sweep` is the equivalent of `POST /v1/bots/supervise` and it is
# what makes position management usable before anybody starts the loop.


def _monitor(request: Request) -> PositionMonitor:
    monitor: PositionMonitor | None = getattr(request.app.state, "position_monitor", None)
    if monitor is None:  # pragma: no cover - registered in main.py at startup
        raise Conflict("the position monitor is not registered on this deployment")
    return monitor


@router.get(
    "/monitor",
    summary="What manages open positions, and which exits are configured",
    description=(
        "Reading the configured policies is the point: an inert trail and an "
        "absent one look identical from outside, and every exit here is OFF "
        "unless a setting turns it on. The defaults are a measurement, not "
        "caution -- a 3.0 ATR trail measured -217 median out-of-sample "
        "expectancy at D1 against -58 for the fixed bracket."
    ),
)
async def monitor_status(request: Request, _: User = _READ) -> dict[str, Any]:
    monitor = _monitor(request)
    return {
        "worker": monitor.name,
        "mode": monitor.mode,
        "supervisor": monitor.status.as_dict(),
        "policies": [p.name for p in _policy_set(request).policies],
        "stop_movers": [p.name for p in _policy_set(request).stop_movers()],
        "configured": wiring.configured(request.app.state.settings),
        "authority": (
            "This worker closes positions and moves stops. It opens nothing, and a "
            "risk halt is not a flatten: no account-level lock reaches it as an "
            "instruction to liquidate."
        ),
    }


def _policy_set(request: Request):  # noqa: ANN202
    return wiring.policy_set_for(request.app.state.settings)


@router.post(
    "/monitor/start",
    summary="Begin managing open positions in the background",
    description=(
        "The loop survives a closed browser and a dropped connection, which "
        "is the whole reason it is a worker. It closes positions: every exit "
        "it can apply is configured, and an unconfigured deployment starts a "
        "monitor that enforces stops and targets already on the position and "
        "moves no stop of its own."
    ),
)
async def monitor_start(request: Request, _: User = _CLOSE) -> dict[str, Any]:
    monitor = _monitor(request)
    if monitor.status.running:
        raise Conflict("the position monitor is already running")
    request.app.state.workers.start(monitor)
    return {"running": True, "worker": monitor.name, "mode": monitor.mode}


@router.post(
    "/monitor/stop",
    summary="Stop managing positions. Nothing already closed is reopened",
    description=(
        "Stops the loop. Positions stay exactly as they are, including any "
        "stop this monitor moved: a stop at the venue is the venue's, and "
        "stopping a worker does not put one back."
    ),
)
async def monitor_stop(request: Request, _: User = _CLOSE) -> dict[str, Any]:
    monitor = _monitor(request)
    monitor.stop()
    return {"running": False, "worker": monitor.name, "mode": monitor.mode}


@router.post(
    "/sweep",
    summary="Run exactly one management pass now",
    description=(
        "One pass of the same code the loop runs, so the mechanism can be "
        "exercised and read before anybody starts it. **It can close a "
        "position**, which is why it needs the same permission an order does."
    ),
)
async def monitor_sweep(request: Request, _: User = _CLOSE) -> dict[str, Any]:
    monitor = _monitor(request)
    await monitor.tick()
    return {
        "worker": monitor.name,
        "mode": monitor.mode,
        "results": [
            {
                "position_id": r.position_id,
                "closed": r.closed,
                "skipped": r.skipped,
                "needs_reconciliation": r.needs_reconciliation,
                "reason": str(r.decision.reason) if r.decision is not None else None,
            }
            for r in monitor.last_results
        ],
        "note": "A pass, not a flatten: a position with no reason to close is left open.",
    }
