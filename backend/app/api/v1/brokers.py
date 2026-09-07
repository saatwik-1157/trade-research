"""Broker routes: read, reconcile, and — since L51 — register a venue.

**This module was read-only for eleven levels, deliberately.** Its original
docstring said so and said why:

> The prompt for this level is explicit that execution must run
> Risk Engine -> Position Sizing -> OMS -> BrokerAdapter, and three of those
> four do not exist. [...] A read-only broker route added now cannot grow a
> write later by accident, because **the write has to be added deliberately in
> the level that also adds the veto in front of it.**

All four now exist and are verified: the RiskEngine mints the only `Approval`
the OMS accepts, sizing produces the volume, the OMS owns the lifecycle and
writes the durable row before transmitting (L45 C-1), and the adapter is the
only boundary to a venue (`test_the_oms_is_the_only_module_that_reaches_a_venue`).
So the write is added here, deliberately, as that paragraph required.

**What the write does and does not do.** It registers an adapter for an
account, filling the two registries the execution path reads — `BrokerRegistry`
and `OrderManagerRegistry` — which were empty at startup by design and which
**no operator action could previously fill.** The execution worker was started
in paper at L51 and every routed signal stopped at `no_venue`, because the seat
existed and nobody could sit in it.

It still cannot place an order. `POST /v1/orders` is the one submission door
and everything behind it is unchanged: registering a venue means the platform
*can* reach one, not that anything has been sent.

**Two venues can be registered, and neither is live.** The simulator, and —
since L70b — the MT5 DEMO terminal through the existing `MT5Adapter`.

The demo venue is the step the platform was missing: `MT5Adapter` has existed
since L10, wraps `tools/mt5_paper` rather than reimplementing it, and had **no
runtime registration path**, so the platform's execution machinery had never
run against a real venue of any kind. Registering it changes that and changes
nothing else.

**A live venue still cannot be registered**, and that is not a temporary
limitation: it needs credentials, and credentials are a separate seat
(`BROKER_CREDENTIALS`) that this route does not open. `_ADAPTERS` has no entry
whose mode is `live`, a test asserts it, and underneath all of it
`tools/mt5_paper.assert_demo` refuses any account whose `trade_mode` is not 0 —
in code, where no setting reaches it.

`reconcile` is a read as well: it compares and reports. Nothing in the path
behind it opens, closes, cancels or rewrites anything.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.brokers.accounts import ensure_broker_account
from app.brokers.base import NotConnected, RefuseToTrade
from app.brokers.reconcile import InternalPosition
from app.brokers.registry import BrokerRegistry, UnknownAccount
from app.core import audit
from app.core.errors import (
    Conflict,
    DependencyUnavailable,
    NotFound,
    ValidationFailed,
    request_id_of,
)
from app.oms.registry import OrderManagerRegistry
from app.positions.ingest import open_internal_positions
from app.security.enforce import announce, require_step_up
from app.security.events import SecurityEvent
from app.security.events import record as security_record
from app.security.stepup import StepUpScope
from app.services.execution import symbol_codes

log = logging.getLogger("app.api.brokers")

router = APIRouter(prefix="/brokers", tags=["brokers"])

_BROKERS = Depends(require_permission(Permission.manage_brokers))


def _registry(request: Request) -> BrokerRegistry:
    registry: BrokerRegistry = request.app.state.brokers
    return registry


def _adapter(request: Request, account_id: str):  # noqa: ANN202 - BrokerAdapter
    try:
        return _registry(request).get(account_id)
    except UnknownAccount as exc:
        raise NotFound(str(exc)) from exc


#: Adapters an operator may register through this route, and the mode each one
#: is. **Neither is live, and there is no key that could be.**
#:
#: `simulator` is `FakeBroker`, the in-process paper venue.
#:
#: `mt5_demo` is the existing `MT5Adapter` against a DEMO terminal. It needs no
#: credentials stored here — the terminal is already logged in, and the adapter
#: reads the account it finds rather than authenticating one. That is exactly
#: why it can be added and a live venue cannot: a real account needs a login, a
#: password and a server, those are a different seat with a different step-up
#: scope, and a route that accepted them would be a route that stores them.
#: This one stores nothing.
#:
#: A test asserts no value here is `live`.
_ADAPTERS = {"simulator": "paper", "mt5_demo": "demo"}


async def _close_quietly(adapter: Any) -> None:
    """Release a half-open venue after a refused registration.

    `MT5Adapter.connect` opens the terminal and THEN runs the fence, so a
    refusal leaves a live handle behind. Failing to close it would leak a
    terminal connection per rejected attempt, and the errors that reach this
    path are exactly the ones an operator retries.
    """
    try:
        await adapter.disconnect()
    except Exception:  # noqa: BLE001 - the caller is already raising
        log.warning("could not close a refused venue", exc_info=True)


def _build_adapter(key: str, mode: str, settings: Any):  # noqa: ANN202 - BrokerAdapter
    """Construct the adapter for one `_ADAPTERS` key.

    Imported inside the function, not at module scope: importing the MT5
    adapter pulls in the toolkit loader, and an API process that never
    registers a terminal has no reason to carry it.
    """
    if key == "simulator":
        from app.brokers.fake import FakeBroker

        return FakeBroker(mode=mode)
    if key == "mt5_demo":
        from app.brokers.mt5 import MT5Adapter

        return MT5Adapter(terminal_path=settings.mt5_terminal_path or None)
    # Unreachable: `_ADAPTERS` is checked before this is called. Raising rather
    # than returning None so a future key added to the map without a branch
    # here fails loudly instead of registering nothing.
    raise ValidationFailed(f"no constructor for adapter {key!r}")


class RegisterAdapterBody(BaseModel):
    """Point one account at one venue."""

    account_id: Annotated[str, Field(min_length=1, max_length=64)]
    #: The key from `_ADAPTERS`. Not a class path: a route that took an import
    #: target would be a route that imports whatever it is handed.
    adapter: Annotated[str, Field(min_length=1, max_length=32)] = "simulator"
    #: Optional. When given, the registration REFUSES unless the terminal is
    #: logged in to exactly this account. Registering a venue is the moment the
    #: platform commits to which account it will trade, and "whichever one the
    #: terminal happens to hold" is not a commitment anybody made.
    expect_account: Annotated[str, Field(max_length=64)] = ""
    reason: Annotated[str, Field(min_length=8, max_length=300)]


class DeregisterBody(BaseModel):
    reason: Annotated[str, Field(min_length=8, max_length=300)]


def _managers(request: Request) -> OrderManagerRegistry:
    registry: OrderManagerRegistry = request.app.state.order_managers
    return registry


@router.post(
    "/adapters",
    status_code=201,
    summary="Register a venue for one account, so execution has somewhere to go",
    description=(
        "Fills the two registries the execution path reads. Until this exists "
        "every routed signal stops at `no_venue`: the registries are empty at "
        "startup by design and nothing else fills them.\n\n"
        "Registering a venue does NOT send anything. `POST /v1/orders` remains "
        "the one submission door and the RiskEngine still stands in front of "
        "it.\n\n"
        "Two venues can be registered and neither is live: `simulator` (the "
        "in-process paper venue) and `mt5_demo` (the existing MT5Adapter "
        "against a DEMO terminal). The adapter's mode must equal "
        "TRADING_MODE, so `mt5_demo` needs a platform running in demo.\n\n"
        "`mt5_demo` opens the terminal and runs the toolkit's own "
        "`assert_demo` fence before anything is registered. A non-demo "
        "account is REFUSED and nothing is stored. Pass `expect_account` to "
        "refuse unless the terminal holds exactly that login.\n\n"
        "A real venue needs credentials, and credentials are a separate seat "
        "this route does not open. Requires step-up re-authentication and a "
        "reason."
    ),
)
async def register_adapter(
    body: RegisterAdapterBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _BROKERS,
) -> dict[str, Any]:
    settings = request.app.state.settings
    mode = _ADAPTERS.get(body.adapter)
    if mode is None:
        raise ValidationFailed(
            f"unknown adapter {body.adapter!r}; this route registers "
            f"{', '.join(sorted(_ADAPTERS))} and nothing else. A real venue needs "
            "credentials, which are a separate seat"
        )

    # The mode fence, and it is the reason this route cannot become a live one.
    # The adapter's mode must equal the platform's, so a paper deployment
    # cannot be pointed at anything but a paper venue -- and `live` has no
    # entry in `_ADAPTERS` at all, so there is no value that reaches a live
    # venue even if `TRADING_MODE` were changed.
    if settings.trading_mode.value != mode:
        raise Conflict(
            f"the {body.adapter!r} adapter is a {mode} venue and this platform runs "
            f"{settings.trading_mode.value}; refusing to register a venue for an "
            "environment the platform is not in"
        )

    await require_step_up(
        request,
        scope=StepUpScope.broker_credentials,
        subject=body.account_id,
        actor_id=user.id,
        settings=request.app.state.settings,
    )

    brokers = _registry(request)
    managers = _managers(request)
    if body.account_id in brokers.adapters or body.account_id in managers.managers:
        # Never replaced. An adapter swapped underneath a manager holding live
        # orders is a manager whose orders belong to a venue it can no longer
        # ask about.
        raise Conflict(
            f"account {body.account_id} already has a venue registered; deregister it "
            "first, which refuses while any order is unresolved"
        )

    adapter = _build_adapter(body.adapter, mode, settings)

    # Connect BEFORE registering, and register nothing if it fails. A registry
    # entry for a venue that never answered is worse than an empty one: the
    # execution path stops at `no_venue` when the seat is empty, and tries when
    # it is filled.
    try:
        account = await adapter.connect()
    except RefuseToTrade as exc:
        await _close_quietly(adapter)
        # The toolkit raises RefuseToTrade for a MISSING MetaTrader5 package as
        # well as for the demo fence, and the two call for opposite responses:
        # one is "install a dependency", the other is "you pointed this at the
        # wrong account". Distinguished by a fact rather than by matching the
        # message -- if the module cannot be imported at all, the fence cannot
        # have been what refused.
        if importlib.util.find_spec("MetaTrader5") is None:
            raise DependencyUnavailable(
                f"this process has no MetaTrader5 package, so it cannot open a "
                f"terminal: {exc}. The package is Windows-only and the API image is "
                "Linux, so the demo venue can only be registered by an API process "
                "running on the host that holds the terminal. Nothing was registered"
            ) from exc
        raise Conflict(
            f"the terminal refused this registration: {exc}. Nothing was registered"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        await _close_quietly(adapter)
        raise DependencyUnavailable(
            f"could not open the {body.adapter!r} venue: {type(exc).__name__}: {exc}. "
            "Nothing was registered"
        ) from exc

    # The account the terminal actually holds, compared against the one the
    # caller said they expected. Registering is the moment the platform commits
    # to an account; "whichever one the terminal happens to be logged in to" is
    # not a commitment anybody made.
    if body.expect_account and str(account.login).strip() != body.expect_account.strip():
        await _close_quietly(adapter)
        raise Conflict(
            f"the terminal is logged in to account {account.login}, not "
            f"{body.expect_account!r}; refusing to register an account nobody named. "
            "Nothing was registered, and no account was switched"
        )

    brokers.register(body.account_id, adapter)
    managers.register(body.account_id, adapter, mode=mode, broker=body.adapter)

    # The durable record of WHICH account this venue is, written from what the
    # terminal reported rather than from what the caller typed. It is what lets
    # a position name the venue it is held at -- `positions.broker_account_id`
    # is a foreign key to this table, and without a row every recorded broker
    # position is an orphan the close route cannot price.
    #
    # None for the simulator: `account_mode` is constrained to demo or live, and
    # a fictional account does not belong in the same table as a real one.
    venue_account = await ensure_broker_account(
        db,
        user_id=user.id,
        name=body.account_id,
        adapter_key=body.adapter,
        mode=mode,
        account=account,
    )
    if venue_account is not None:
        # The row's id is flushed so it can be used as a key below.
        await db.flush()
        # **Registered under BOTH keys, deliberately.**
        #
        # The platform has two identifier spaces and they were never connected,
        # because no broker position had ever existed to need them connected:
        # the registries are keyed by whatever label an operator registers with,
        # while `positions.broker_account_id` -- and therefore
        # `PositionView.account_id`, and therefore the key `BrokerExitExecutor`
        # looks the order manager up by -- is the durable `broker_accounts.id`.
        #
        # Registering the same adapter object under its account id as well as
        # the operator's label makes the close path work without changing what
        # anybody already passes to this route or to `POST /v1/orders`. One
        # adapter, two names for it; `deregister` removes both.
        brokers.register(venue_account.id, adapter)
        managers.register(venue_account.id, adapter, mode=mode, broker=body.adapter)

    await announce(
        request,
        security_record(
            SecurityEvent.dangerous_action,
            f"a {mode} venue was registered for account {body.account_id}",
            user_id=user.id,
            action="register_adapter",
            resource="broker",
            outcome=body.adapter,
        ),
    )
    await audit.record_admin(
        db,
        audit.AuditAction.admin_action,
        "broker",
        actor_user_id=user.id,
        resource_id=body.account_id,
        reason=body.reason,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        environment=mode,
        # The venue's own login, not the caller's `account_id`. The audit trail
        # has to answer "which account did this platform commit to", and the
        # internal id is a label somebody chose -- it does not name a broker
        # account. No credential is recorded: a login is half of one, and the
        # half that is printed on every statement.
        extra={
            "action": "register_adapter",
            "adapter": body.adapter,
            "venue_login": account.login,
            "venue_server": account.server,
        },
    )
    await db.commit()
    return {
        "account_id": body.account_id,
        "adapter": body.adapter,
        "mode": mode,
        "state": adapter.state.value,
        # What the VENUE reported, not what was configured. A login echoed back
        # from the request would tell the operator nothing they did not type.
        # The durable id the platform uses internally -- what a position will
        # name, and the second key this adapter is registered under.
        "venue_account_id": venue_account.id if venue_account else None,
        "venue_account": {
            "login": account.login,
            "server": account.server,
            "currency": account.currency,
            "mode": account.mode.value,
            "trade_allowed": account.trade_allowed,
        },
        "note": (
            "The account now has a venue. Nothing has been sent: the RiskEngine "
            "still approves every order and the OMS still owns the lifecycle. "
            f"This is a {mode} venue -- live execution remains blocked by "
            "LIVE_GATES and by assert_demo, neither of which this route touches."
        ),
    }


@router.delete(
    "/adapters/{account_id}",
    summary="Remove an account's venue, unless it is holding an unresolved order",
    description=(
        "Refuses while the account has an order whose venue state was never "
        "established. Removing the adapter over one would discard the only "
        "thing that can reconcile it — the platform would still hold the "
        "record and would no longer be able to ask the venue about it."
    ),
)
async def deregister_adapter(
    account_id: str,
    body: DeregisterBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _BROKERS,
) -> dict[str, Any]:
    brokers = _registry(request)
    managers = _managers(request)
    if account_id not in brokers.adapters and account_id not in managers.managers:
        raise NotFound(f"no venue is registered for account {account_id}")

    unresolved = managers.unresolved().get(account_id) or []
    if unresolved:
        raise Conflict(
            f"account {account_id} has {len(unresolved)} order(s) whose venue state was "
            "never established; reconcile them before removing the venue, because "
            "afterwards nothing can ask the venue about them"
        )

    await require_step_up(
        request,
        scope=StepUpScope.broker_credentials,
        subject=account_id,
        actor_id=user.id,
        settings=request.app.state.settings,
    )

    adapter = brokers.adapters.get(account_id)
    if adapter is not None:
        try:
            await adapter.disconnect()
        except Exception:  # noqa: BLE001 - removal must not depend on a live venue
            pass
    # BOTH keys the registration created. An adapter left reachable under its
    # account id after being removed under its label is a venue the close path
    # can still route to and the operator believes is gone.
    keys = {account_id}
    for key, registered in list(brokers.adapters.items()):
        if registered is adapter:
            keys.add(key)
    for key, manager in list(managers.managers.items()):
        if getattr(manager, "adapter", None) is adapter:
            keys.add(key)
    for key in keys:
        brokers.remove(key)
        managers.managers.pop(key, None)

    await announce(
        request,
        security_record(
            SecurityEvent.dangerous_action,
            f"the venue for account {account_id} was removed",
            user_id=user.id,
            action="deregister_adapter",
            resource="broker",
            outcome="removed",
        ),
    )
    await audit.record_admin(
        db,
        audit.AuditAction.admin_action,
        "broker",
        actor_user_id=user.id,
        resource_id=account_id,
        reason=body.reason,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        extra={"action": "deregister_adapter"},
    )
    await db.commit()
    return {"account_id": account_id, "registered": False}


@router.get(
    "",
    summary="Registered broker adapters",
    description=(
        "One adapter per trading account, never a global connection. Carries "
        "no credential, because the platform holds none — encrypted broker "
        "credentials are L39."
    ),
)
async def list_adapters(request: Request, _: User = _BROKERS) -> dict[str, object]:
    registry = _registry(request)
    return {"adapters": registry.describe(), "connected": registry.connected}


@router.get(
    "/health",
    summary="Observed health for every registered account",
    description=(
        "`usable` means the account was read successfully and the venue "
        "reports trading is allowed. A connected terminal with trading "
        "disabled is `degraded`, not healthy: an order attempted there cannot "
        "succeed. Never reports a state it did not observe."
    ),
)
async def health(request: Request, _: User = _BROKERS) -> dict[str, object]:
    reported = await _registry(request).health()
    return {"accounts": {a: h.as_dict() for a, h in reported.items()}}


@router.get(
    "/{account_id}/account",
    summary="Balance, equity and margin as the venue reports them",
    description=(
        "`mode` is what the venue says the account is — demo, contest or real "
        "— not what a setting hopes. The account number is not returned."
    ),
)
async def account(request: Request, account_id: str, _: User = _BROKERS) -> dict[str, object]:
    adapter = _adapter(request, account_id)
    try:
        found = await adapter.get_account()
    except (NotConnected, RefuseToTrade) as exc:
        raise DependencyUnavailable(str(exc)) from exc
    return {
        "account_id": account_id,
        "adapter": adapter.name,
        "mode": adapter.mode,
        "account_mode": str(found.mode),
        "server": found.server,
        "currency": found.currency,
        "balance": str(found.balance),
        "equity": str(found.equity),
        "margin": str(found.margin) if found.margin is not None else None,
        "free_margin": str(found.free_margin) if found.free_margin is not None else None,
        "trade_allowed": found.trade_allowed,
        # `login` is deliberately absent: an account number is a
        # credential-shaped identifier and nothing in the UI needs it.
    }


@router.get(
    "/{account_id}/positions",
    summary="What the venue currently holds",
    description=(
        "Read from the venue on every call, never from a cache. A cached "
        "position presented as the broker's book is the failure "
        "`tools/mt5_account.py` exists to avoid."
    ),
)
async def positions(request: Request, account_id: str, _: User = _BROKERS) -> dict[str, object]:
    adapter = _adapter(request, account_id)
    try:
        held = await adapter.get_positions()
    except (NotConnected, RefuseToTrade) as exc:
        raise DependencyUnavailable(str(exc)) from exc
    return {
        "account_id": account_id,
        "source": "read from the venue, not a cache",
        "positions": [
            {
                "broker_position_id": p.position_id,
                "symbol": p.symbol,
                "side": p.side,
                "volume": str(p.volume),
                "entry_price": str(p.entry_price),
                "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
                "take_profit": str(p.take_profit) if p.take_profit is not None else None,
                "profit": str(p.profit) if p.profit is not None else None,
                "swap": str(p.swap) if p.swap is not None else None,
                "opened_at": p.opened_at.isoformat(),
                "magic": p.magic,
            }
            for p in held
        ],
        "count": len(held),
    }


@router.get(
    "/{account_id}/orders",
    summary="Pending orders at the venue",
)
async def orders(request: Request, account_id: str, _: User = _BROKERS) -> dict[str, object]:
    adapter = _adapter(request, account_id)
    try:
        pending = await adapter.get_orders()
    except (NotConnected, RefuseToTrade) as exc:
        raise DependencyUnavailable(str(exc)) from exc
    return {
        "account_id": account_id,
        "orders": [
            {
                "broker_order_id": o.order_id,
                "symbol": o.symbol,
                "side": o.side,
                "volume": str(o.volume),
                "price": str(o.price) if o.price is not None else None,
                "state": o.state,
                "placed_at": o.placed_at.isoformat(),
                "magic": o.magic,
            }
            for o in pending
        ],
        "count": len(pending),
    }


@router.get(
    "/{account_id}/quote",
    summary="A quote as this venue reports it",
    description=(
        "The broker's own book, distinct from the normalized market-data feed "
        "at /v1/market/quotes. They are different measurements and are never "
        "merged."
    ),
)
async def quote(
    request: Request,
    account_id: str,
    symbol: str = Query(..., max_length=64, description="The BROKER's symbol name."),
    _: User = _BROKERS,
) -> dict[str, object]:
    adapter = _adapter(request, account_id)
    try:
        found = await adapter.get_quote(symbol)
    except (NotConnected, RefuseToTrade) as exc:
        raise DependencyUnavailable(str(exc)) from exc
    return {
        "account_id": account_id,
        "symbol": found.symbol,
        "bid": str(found.bid),
        "ask": str(found.ask),
        "spread": str(found.spread),
        "at": found.at.isoformat(),
        "source": "broker book",
    }


@router.get(
    "/{account_id}/reconcile",
    summary="Compare the venue's positions against the platform's record",
    description=(
        "**A report, not a repair.** Nothing is opened, closed, cancelled or "
        "rewritten. `safe_to_trade` is false only while an order was sent and "
        "never resolved — that must be settled against the venue's deal "
        "history before another order is sent for the same intent, and it is "
        "never settled by retrying. An unexpected position at the venue is a "
        "fact to investigate, not a reason to halt: closing it would close "
        "somebody's manual trade."
    ),
)
async def reconcile(
    request: Request,
    account_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _BROKERS,
) -> dict[str, object]:
    adapter = _adapter(request, account_id)

    # What the platform believes it holds, in this adapter's mode.
    #
    # This used to pass a hard-coded empty list, with a comment saying the
    # `positions` table was empty and the OMS that fills it was L19. That was
    # true when written and STAYED true after L19 landed, because no broker
    # fill wrote a position row until `app.positions.ingest` existed. Comparing
    # against nothing made every venue position `unexpected_at_broker` forever
    # -- a clean-looking report of a dirty account that was really a comparison
    # with an empty set.
    # The MODE off the adapter, not the order manager. An account can have a
    # broker adapter registered without an order manager -- that is how several
    # tests and any read-only registration look -- and asking the manager
    # registry raises `NoOrderManager` for exactly that case, turning a read
    # into a 500.
    mode = str(getattr(adapter, "mode", "") or "")
    internal: list[InternalPosition] = []
    if mode:
        rows = await open_internal_positions(db, mode=mode)
        codes = await symbol_codes(db, rows)
        for row in rows:
            if not row.broker_position_id:
                # Nothing to match on, so it cannot be compared. Skipped rather
                # than sent through as an unmatchable entry that would always
                # report `missing_at_broker` and train an operator to ignore it.
                continue
            internal.append(
                InternalPosition(
                    position_id=str(row.broker_position_id),
                    symbol=codes.get(row.symbol_id, row.symbol_id),
                    side=str(row.side),
                    volume=row.quantity,
                    entry_price=row.entry_price,
                    stop_loss=row.stop_loss,
                    take_profit=row.take_profit,
                    status=str(row.status),
                )
            )

    try:
        report = await adapter.reconcile(internal)
    except (NotConnected, RefuseToTrade) as exc:
        raise DependencyUnavailable(str(exc)) from exc
    return {"account_id": account_id, **report.as_dict()}
