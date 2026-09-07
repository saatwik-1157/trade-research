"""Orders: the record, and the one authorized path to a venue.

`POST /v1/orders` was a 501 naming L19 until this level. It is now built, and
what it builds is deliberately not a shortcut: a manual order runs the SAME
gates a bot signal runs, in the same order, through the same objects.

    request -> symbol + contract spec -> app.sizing -> app.risk -> OMS -> adapter

Three properties are worth stating plainly, because they are what make a
client-facing submit route safe at all.

**A client cannot bypass a gate by asking.** The quantity it sends is an
INPUT to position sizing, not the quantity that gets traded: it is validated
against the venue's step, minimum and maximum like any other, and the Risk
Engine then evaluates the result. A client that states a size the account may
not take is refused, never trimmed to what it may.

**A client cannot reach a venue that is not registered.** `OrderManagerRegistry`
is empty until an operator registers an adapter, so in the default deployment
this route refuses with that reason. `TRADING_MODE=paper` and all ten
`LIVE_GATES` false remain unchanged by anything here.

**Idempotency is the platform's, not this route's.** `Idempotency-Key` becomes
`orders.intent_id`, which is `unique=True`, and the OMS returns the existing
order for a repeat rather than creating a second. So a double-clicked button, a
retried fetch and a replayed TradingView alert are one mechanism.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdempotencyKey
from app.api.pagination import Page, PageParams, page_params
from app.api.v1.schemas import ExecutionOut, OrderEventOut, OrderOut
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.brokers.accounts import account_for as broker_account_for
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.oms.registry import NoOrderManager, OrderManagerRegistry
from app.oms.repository import OrderRepository
from app.oms.service import OrderRefused, ReconciliationRequired
from app.oms.state import NEEDS_RECONCILIATION, OrderStatus
from app.positions.ingest import record_fill
from app.risk.engine import OrderProposal, PortfolioState, RiskEngine
from app.risk.service import RiskService
from app.services import execution as svc
from app.sizing.calculator import SizingRequest, resolve_method
from app.sizing.service import SizingService
from app.symbols.errors import SymbolError
from app.symbols.service import contract_spec, get_symbol

router = APIRouter(prefix="/orders", tags=["orders"])

_READ = Depends(require_permission(Permission.view_portfolio))
_SUBMIT = Depends(require_permission(Permission.submit_orders))


def _out(row: object, codes: dict[str, str]) -> OrderOut:
    model = OrderOut.model_validate(row, from_attributes=True)
    return model.model_copy(update={"symbol": codes.get(getattr(row, "symbol_id", ""))})


@router.get(
    "",
    response_model=Page[OrderOut],
    summary="The order record",
    description=(
        "Every order the platform has recorded, newest first. This is a read of "
        "history: the rows currently present were imported from the MT5 demo "
        "account ledger and all carry mode='demo' and source='jsonl_import'. "
        "No mode filter is applied by default, and every row states its own, so "
        "simulator and broker orders cannot be pooled by omission."
    ),
)
async def list_orders(
    params: PageParams = Depends(page_params),
    mode: str | None = Query(None, description="paper | demo | live"),
    status: str | None = Query(None, max_length=20),
    side: str | None = Query(None, description="buy | sell"),
    symbol: str | None = Query(None, max_length=32, description="Internal symbol code."),
    source: str | None = Query(None, max_length=16),
    from_time: datetime | None = Query(None, description="created_at lower bound, UTC."),
    to_time: datetime | None = Query(None, description="created_at upper bound, UTC."),
    sort: str | None = Query(None, description="created_at | submitted_at | status | quantity"),
    order: str | None = Query(None, description="asc | desc"),
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> Page[OrderOut]:
    rows, page, codes = await svc.list_orders(
        db,
        params,
        mode=mode,
        status=status,
        side=side,
        symbol=symbol,
        source=source,
        from_time=from_time,
        to_time=to_time,
        sort=sort,
        order=order,
    )
    return Page[OrderOut](items=[_out(r, codes) for r in rows], page=page)


@router.get("/{order_id}", response_model=OrderOut, summary="One order")
async def get_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> OrderOut:
    row, code = await svc.get_order(db, order_id)
    model = OrderOut.model_validate(row, from_attributes=True)
    return model.model_copy(update={"symbol": code})


@router.get(
    "/{order_id}/events",
    response_model=list[OrderEventOut],
    summary="The state transitions recorded against one order",
)
async def get_order_events(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> list[OrderEventOut]:
    rows = await svc.order_events(db, order_id)
    return [OrderEventOut.model_validate(r, from_attributes=True) for r in rows]


@router.get(
    "/{order_id}/executions",
    response_model=list[ExecutionOut],
    summary="The fills recorded against one order",
    description=(
        "A fill is what the venue reported. `fill_source` says whether it came "
        "from a simulator or a broker, and the two are never merged."
    ),
)
async def get_order_executions(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> list[ExecutionOut]:
    rows = await svc.order_executions(db, order_id)
    return [ExecutionOut.model_validate(r, from_attributes=True) for r in rows]


class SubmitBody(BaseModel):
    """A manual order request. Every number here is an INPUT, not an instruction."""

    account_id: Annotated[str, Field(min_length=1, max_length=64)]
    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    side: Annotated[str, Field(pattern="^(buy|sell)$")]
    order_type: Annotated[str, Field(pattern="^(market|limit|stop)$")] = "market"
    time_in_force: Annotated[str, Field(pattern="^(gtc|ioc|fok|day)$")] = "gtc"

    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)

    # How much. `quantity` with the default mode is a request for that size,
    # checked against the venue's rules like any other; the risk modes size it
    # from a budget instead. Either way the engine decides the final figure.
    sizing_mode: str = "fixed_quantity"
    quantity: Decimal | None = Field(default=None, gt=0)
    risk_amount: Decimal | None = Field(default=None, gt=0)
    risk_percent: Decimal | None = Field(default=None, gt=0, le=100)
    equity: Decimal | None = Field(default=None, gt=0)

    strategy_id: str | None = None


def _registry(request: Request) -> OrderManagerRegistry:
    registry: OrderManagerRegistry = request.app.state.order_managers
    return registry


@router.post(
    "",
    status_code=201,
    summary="Submit an order through Risk, Sizing, the OMS and the Broker Adapter",
    description=(
        "Runs the same gates a bot signal runs, in the same order, through the "
        "same objects. The quantity sent is an input to position sizing, not "
        "the quantity traded. Send `Idempotency-Key`: it becomes "
        "`orders.intent_id`, which is unique, so a replayed submission returns "
        "a conflict naming the existing order instead of creating a second. "
        "Refuses when no order manager is registered for the account, which is "
        "the default state of a deployment not pointed at a venue."
    ),
)
async def submit_order(
    body: SubmitBody,
    request: Request,
    key: IdempotencyKey = None,
    db: AsyncSession = Depends(get_db),
    user: User = _SUBMIT,
) -> dict[str, Any]:
    if not key:
        raise ValidationFailed(
            "an Idempotency-Key header is required. It becomes the order's intent id, "
            "which is what stops a retried request from becoming a second order"
        )
    # L38. Safe mode, before anything else and able only to REFUSE. It is an
    # additional gate in front of risk, sizing and the OMS -- never a
    # replacement for one -- so a submission that gets past it still faces
    # every check it faced before. The refusal names the condition rather than
    # the mode: "new orders are blocked" is not an explanation.
    safe_mode = getattr(request.app.state, "safe_mode", None)
    if safe_mode is not None and safe_mode.engaged:
        reasons = "; ".join(f"{latch.reason}: {latch.detail}" for latch in safe_mode.reasons)
        raise Conflict(
            "the platform is in safe mode, so no new order is submitted. "
            f"{reasons}. Reconcile and release it at /v1/recovery."
        )

    registry = _registry(request)
    try:
        manager = registry.get(body.account_id)
    except NoOrderManager as exc:
        raise Conflict(str(exc)) from exc

    # The instrument's measured contract terms, from the platform's own symbol
    # mappings. Not a second metadata system.
    try:
        symbol = await get_symbol(db, body.symbol)
        spec = await contract_spec(db, body.symbol)
    except SymbolError as exc:
        raise ValidationFailed(str(exc)) from exc

    # SIZING. The client's figure is an input to this, never a bypass of it.
    sizing_service: SizingService = request.app.state.sizing
    try:
        method = resolve_method(body.sizing_mode)
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed(str(exc)) from exc
    sized = sizing_service.size(
        SizingRequest(
            method=method,
            spec=spec,
            quantity=body.quantity,
            risk_amount=body.risk_amount,
            equity=body.equity,
            risk_percent=body.risk_percent,
            side=body.side,
            entry_price=body.entry_price,
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
        ),
        context="api:orders",
    )
    if sized.refused or sized.volume is None:
        raise ValidationFailed(f"position sizing refused this order: {sized.gap}")

    # RISK. The final authority, reached exactly the way the paper pipeline
    # reaches it: the effective limits come from RiskService (so this route
    # cannot hold a looser copy), the pure engine mints the Approval the OMS
    # requires, and `record_verdict` turns the same verdict into the durable,
    # coded, versioned record. One evaluation, two representations.
    risk: RiskService = request.app.state.risk
    proposal = OrderProposal(
        symbol=symbol.code,
        side=body.side,
        mode=manager.mode,
        volume=sized.volume,
        entry_price=body.entry_price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        risk_amount=sized.risk_actual,
        account_id=body.account_id,
        strategy_id=body.strategy_id,
        # NOT the idempotency key. `proposal.signal_id` travels into
        # `ManagedOrder.signal_id` and then into `orders.signal_id`, which is a
        # FOREIGN KEY to `signals` -- so an idempotency key that is not also a
        # signal id makes the insert fail. This route is for a MANUAL order and
        # there is no signal behind one, so the column is null and the key lives
        # where it belongs: `orders.intent_id`, which is UNIQUE and is what
        # `guard_resend` and the duplicate check already read.
        #
        # Measured on PostgreSQL, 2026-09-07, the first time this route was
        # driven against a real venue:
        #   ForeignKeyViolationError: insert or update on table "orders"
        #   violates constraint "fk_orders_signal_id_signals"
        # It survived 174 API tests because they run on SQLite, which does not
        # enforce foreign keys unless `PRAGMA foreign_keys=ON` is set, and
        # nothing sets it.
        signal_id=None,
    )
    resolved = await risk.limits_for(
        db,
        account_id=body.account_id,
        strategy_id=body.strategy_id,
        symbol=symbol.code,
    )
    engine = RiskEngine(resolved.limits, risk.switches)
    approval, verdict = engine.approve(proposal, PortfolioState(equity=body.equity))
    decision = await risk.record_verdict(
        db,
        verdict,
        account_id=body.account_id,
        execution_mode=manager.mode,
        configuration_version=resolved.version,
        order_type=body.order_type,
    )
    await db.commit()
    if approval is None:
        # Being told no is the system working, so this is a conflict carrying
        # the engine's own reason, never a 500.
        raise Conflict(
            "the risk engine refused this order: " + (decision.reason or "no reason recorded")
        )

    # OMS. One order per intent, durable before the venue is called.
    repo = OrderRepository({symbol.code: symbol.id})
    async with registry.lock(body.account_id):
        try:
            # The in-memory guard FIRST. It is the stricter of the two and
            # gives the sharper message -- "a resolved intent may not be
            # re-sent at all" rather than "an order already exists" -- and this
            # route has advertised that wording since L06.
            manager.guard_resend(key)
            # Then the same question asked of the record. **L45 C-1.**
            #
            # `guard_resend` reads `OrderManager.by_intent`, a dict every new
            # process starts empty, so on a fresh process it passes for an
            # intent that already has an order. `orders.intent_id` is UNIQUE,
            # so the write would then fail -- but between `create` and
            # `submit`, which is the least recoverable moment there is.
            #
            # Raised as the OMS's own exceptions rather than a `Conflict`, so
            # the arms below classify a durable refusal exactly as they
            # classify an in-memory one. Same shape as
            # `ExecutionPipeline._guard_resend_durably`.
            recorded = await repo.order_for_intent(db, key)
            if recorded is not None:
                if OrderStatus(recorded.status) in NEEDS_RECONCILIATION:
                    raise ReconciliationRequired(
                        f"idempotency key {key} has order {recorded.id} recorded in "
                        f"`{recorded.status}`; the venue may be holding it. Reconcile "
                        "before sending anything for this intent"
                    )
                raise OrderRefused(
                    f"idempotency key {key} already produced order {recorded.id} in "
                    f"state {recorded.status}; a new order for it would be a second "
                    "order for one signal"
                )
            submission = manager.create(
                approval,
                intent_id=key,
                account_id=body.account_id,
                order_type=body.order_type,
                time_in_force=body.time_in_force,
                sizing_snapshot=sized.as_dict(),
            )
        except ReconciliationRequired as exc:
            raise Conflict(str(exc)) from exc
        except OrderRefused as exc:
            raise Conflict(str(exc)) from exc

        if submission.duplicate:
            # The contract this route has advertised since L06: a replayed
            # submission is a conflict naming the order it already made, not a
            # second order and not a silent success.
            raise Conflict(
                f"idempotency key {key} already produced order {submission.order.id} "
                f"in state {submission.order.status}"
            )

        order = submission.order
        # The row exists before anything is transmitted, and again after the
        # venue has spoken. Never "submit, save later".
        await repo.persist(db, order)
        await db.commit()

        await manager.submit(order)
        await repo.persist(db, order)
        # The position the fill implies, written in the SAME transaction as the
        # order it came from. Two commits could leave an order recorded filled
        # with no position behind it, which is the state reconciliation cannot
        # tell apart from a position the venue lost.
        #
        # Returns None for paper, for anything not confirmed filled, and for a
        # fill carrying no venue identifier. It is not a second execution path:
        # nothing is sent here, the fill already happened.
        # The durable account row registration wrote, looked up by the same
        # registry key -- NOT `body.account_id` itself. That key is an operator's
        # label; this column is a FOREIGN KEY to `broker_accounts`, and writing
        # the label there would fail on PostgreSQL exactly as `signal_id` did
        # and pass on SQLite exactly as `signal_id` did.
        #
        # None when there is no row, which is the simulator and any venue
        # registered before this existed. A position with no account is still
        # recorded and still reconciles; it just cannot be priced for a close.
        venue_account = await broker_account_for(db, name=body.account_id, mode=manager.mode)
        await record_fill(
            db,
            order,
            symbol_id=symbol.id,
            broker_account_id=venue_account.id if venue_account else None,
            source="manual",
        )
        await db.commit()

    await manager.flush_events()
    return {
        **order.as_dict(),
        "sizing": sized.as_dict(),
        "risk_decision": decision.as_dict(),
    }


@router.post(
    "/{order_id}/cancel",
    summary="Ask the venue to cancel an order, and believe only its answer",
    description=(
        "Moves the order to `cancel_requested` and asks the adapter. It becomes "
        "`cancelled` only when the venue confirms; if the venue refuses, the "
        "order goes back to being live, and if the answer is unclear it is "
        "parked as `unknown` for reconciliation. Assuming a cancellation "
        "succeeded is how a position nobody is watching stays open."
    ),
)
async def cancel_order(
    order_id: str,
    request: Request,
    account_id: str = Query(..., description="The account whose manager holds the order"),
    db: AsyncSession = Depends(get_db),
    user: User = _SUBMIT,
) -> dict[str, Any]:
    manager, order = _live_order(request, account_id, order_id)
    try:
        await manager.cancel(order, reason=f"cancelled by user {user.id}")
    except (ReconciliationRequired, OrderRefused) as exc:
        raise Conflict(str(exc)) from exc

    symbol = await get_symbol(db, order.symbol)
    await OrderRepository({order.symbol: symbol.id}).persist(db, order)
    await db.commit()
    await manager.flush_events()
    return order.as_dict()


@router.post(
    "/{order_id}/reconcile",
    summary="Settle an order whose outcome is unknown, by asking the venue",
    description=(
        "The ONLY exit from `unknown` and from a crashed `submitting`. It never "
        "re-sends: it reads what the venue holds and records that. An order the "
        "venue holds nothing for becomes `failed`, which is the one state a "
        "fresh order for the same intent may follow."
    ),
)
async def reconcile_order(
    order_id: str,
    request: Request,
    account_id: str = Query(..., description="The account whose manager holds the order"),
    db: AsyncSession = Depends(get_db),
    user: User = _SUBMIT,
) -> dict[str, Any]:
    manager, order = _live_order(request, account_id, order_id)
    try:
        await manager.reconcile(order)
    except ReconciliationRequired as exc:
        # The venue could not be read, so nothing is concluded. A reconciler
        # that decides an order is lost because the network was down is worse
        # than one that refuses to decide.
        raise Conflict(str(exc)) from exc

    symbol = await get_symbol(db, order.symbol)
    await OrderRepository({order.symbol: symbol.id}).persist(db, order)
    await db.commit()
    await manager.flush_events()
    return order.as_dict()


def _live_order(request: Request, account_id: str, order_id: str):  # noqa: ANN202
    try:
        manager = _registry(request).get(account_id)
    except NoOrderManager as exc:
        raise Conflict(str(exc)) from exc
    order = manager.orders.get(order_id)
    if order is None:
        raise NotFound(f"no live order {order_id} on account {account_id}")
    return manager, order


@router.get(
    "/oms/status",
    summary="OMS counters, unresolved orders and the stated retry policy",
)
async def oms_status(request: Request, _: User = _READ) -> dict[str, Any]:
    registry = _registry(request)
    return {
        **registry.describe(),
        "managers": {account: manager.status() for account, manager in registry.managers.items()},
    }
