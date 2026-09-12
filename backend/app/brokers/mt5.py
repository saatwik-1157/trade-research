"""MT5Adapter: the DEMO venue, wrapping the existing toolkit.

This module **calls `tools/mt5_paper.py` and `tools/mt5_account.py` rather
than reimplementing them.** Those two carry three live defects' worth of
hard-won correctness — the filling-mode bitmask translation, the bracket
sanity check against the actual fill, the step-count rounding, and the server
clock helpers — and none of it is retyped here.

It also ends the repository's worst duplication. `connect()` existed four
times (`mt5_paper`, `mt5_account`, `rule_backtest`, `symbols/sync_mt5`); this
is now the one place the platform opens a terminal.

**The fence is unchanged and not re-implemented:** `assert_demo` is called on
the toolkit's own function, so a real or contest account aborts before an
order is constructed, exactly as it does on the command line. A setting
cannot override it, and neither can this adapter.

Live accounts: `mode` is `demo` and `connect()` refuses anything else. There
is no live adapter, and adding one is a separate, deliberate piece of work.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.auth.models import utcnow
from app.brokers.base import (
    Account,
    AccountMode,
    BrokerAdapter,
    BrokerOrder,
    BrokerPosition,
    ConnectionState,
    Deal,
    NotConnected,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Quote,
    RefuseToTrade,
    SymbolInfo,
)

log = logging.getLogger("app.brokers.mt5")

TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "tools",
)

#: The toolkit's tag. Read but never written by this adapter: it is how the
#: platform can SEE what `tools/` opened, which reconciliation needs.
TOOLKIT_MAGIC = 770315

#: What this adapter tags its own orders with. **Different from the toolkit's,
#: and that difference is the fix for a real incident.**
#:
#: They shared 770315 until 2026-09-07, deliberately, "so both see the same
#: positions". Seeing was never the problem -- ACTING was. `close_own` closes
#: every position carrying its magic, so the running harness harvested two
#: positions the platform had opened, and the platform's rows went stale
#: underneath it.
#:
#: With separate tags each system acts only on what it opened, and the platform
#: still sees everything: `get_positions` defaults to no filter, so an
#: unexpected position -- a hand trade, a harness trade -- is still reported by
#: reconciliation rather than hidden.
MAGIC = 770316


def _import_toolkit() -> tuple[Any, Any]:
    """Import the research toolkit's MT5 modules. Never vendored, never copied."""
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    import mt5_account
    import mt5_paper

    return mt5_paper, mt5_account


def _toolkit_risk_gate() -> Any:
    """`tools/risk_gate.py`, for its `APPROVED_UPSTREAM` marker only.

    The import direction looks backwards and is not: `risk_gate` imports
    `app.risk.engine`, which is pure stdlib and imports nothing from here. This
    module already reaches into `tools/` for order construction and reads the
    marker from the same place, rather than defining a second one that would
    have to be kept equal to it.
    """
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    import risk_gate

    return risk_gate


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class MT5Adapter(BrokerAdapter):
    """DEMO only. Blocking MT5 calls are run in a thread."""

    name = "mt5"
    mode = "demo"

    def __init__(self, terminal_path: str | None = None) -> None:
        self.terminal_path = terminal_path
        self._state = ConnectionState.disconnected
        self._mt5: Any = None
        self._paper: Any = None
        self._account_mod: Any = None
        self._last_error: str | None = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def _call(self, fn, *args, **kwargs):  # noqa: ANN001, ANN202
        """Run a blocking terminal call off the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ---------------------------------------------------------- connection
    async def connect(self) -> Account:
        self._state = ConnectionState.connecting
        try:
            paper, account_mod = _import_toolkit()
            self._paper, self._account_mod = paper, account_mod
            self._mt5 = await self._call(paper.connect, self.terminal_path)
            # The toolkit's fence, called rather than restated. It raises on
            # REAL, CONTEST and any value it does not recognise.
            info = await self._call(paper.assert_demo, self._mt5, False)
        except Exception as exc:  # noqa: BLE001 - reported, and the state says so
            self._state = ConnectionState.error
            self._last_error = f"{type(exc).__name__}: {exc}"[:300]
            # A refusal from the fence is not a connection problem; it is the
            # fence working, and it must not be retried into submission.
            if type(exc).__name__ == "RefuseToTrade":
                raise RefuseToTrade(str(exc)) from exc
            raise NotConnected(self._last_error) from exc

        self._state = ConnectionState.connected
        self._last_error = None
        log.info(
            "mt5 connected",
            extra={"event": "broker_connected", "login": info["login"], "server": info["server"]},
        )
        return await self.get_account()

    async def disconnect(self) -> None:
        if self._mt5 is not None:
            await self._call(self._mt5.shutdown)
        self._mt5 = None
        self._state = ConnectionState.disconnected

    def _require(self) -> Any:
        if self._state is not ConnectionState.connected or self._mt5 is None:
            raise NotConnected(f"mt5 adapter is {self._state.value}")
        return self._mt5

    # ---------------------------------------------------------------- read
    async def get_account(self) -> Account:
        mt5 = self._require()
        ai = await self._call(mt5.account_info)
        if ai is None:
            raise NotConnected("no account is logged in to the terminal")
        ti = await self._call(mt5.terminal_info)
        mode = {0: AccountMode.demo, 1: AccountMode.contest, 2: AccountMode.real}.get(
            getattr(ai, "trade_mode", -1), AccountMode.unknown
        )
        return Account(
            login=str(ai.login),
            server=str(ai.server),
            currency=getattr(ai, "currency", "?"),
            balance=Decimal(str(ai.balance)),
            equity=Decimal(str(ai.equity)),
            margin=_dec(getattr(ai, "margin", None)),
            free_margin=_dec(getattr(ai, "margin_free", None)),
            mode=mode,
            trade_allowed=bool(getattr(ti, "trade_allowed", False)),
        )

    async def get_symbols(self) -> list[SymbolInfo]:
        mt5 = self._require()
        rows = await self._call(mt5.symbols_get) or []
        out = []
        for info in rows:
            out.append(
                SymbolInfo(
                    symbol=info.name,
                    digits=int(getattr(info, "digits", 0)),
                    point=Decimal(str(getattr(info, "point", 0) or 0)),
                    contract_size=_dec(getattr(info, "trade_contract_size", None)),
                    tick_size=_dec(getattr(info, "trade_tick_size", None)),
                    tick_value=_dec(getattr(info, "trade_tick_value", None)),
                    volume_min=_dec(getattr(info, "volume_min", None)),
                    volume_max=_dec(getattr(info, "volume_max", None)),
                    volume_step=_dec(getattr(info, "volume_step", None)),
                )
            )
        return out

    async def get_quote(self, symbol: str) -> Quote:
        mt5 = self._require()
        tick = await self._call(mt5.symbol_info_tick, symbol)
        if not tick or not tick.bid or not tick.ask:
            raise NotConnected(f"no quote for {symbol}; the market is probably closed")
        return Quote(
            symbol=symbol,
            bid=Decimal(str(tick.bid)),
            ask=Decimal(str(tick.ask)),
            at=datetime.fromtimestamp(tick.time),
        )

    async def get_positions(self, magic: int | None = None) -> list[BrokerPosition]:
        """Every position the ACCOUNT holds, not only ours.

        No filter by default. Reconciliation has to be able to say "the venue
        holds something we have no record of" -- a hand trade, or the harness's
        -- and a default that filtered to our own tag would make that finding
        impossible to reach.
        """
        mt5 = self._require()
        rows = await self._call(mt5.positions_get) or []
        out = []
        for p in rows:
            if magic is not None and p.magic != magic:
                continue
            out.append(
                BrokerPosition(
                    position_id=str(p.ticket),
                    symbol=p.symbol,
                    side="long" if p.type == 0 else "short",
                    volume=Decimal(str(p.volume)),
                    entry_price=Decimal(str(p.price_open)),
                    opened_at=datetime.fromtimestamp(p.time),
                    stop_loss=_dec(p.sl) or None,
                    take_profit=_dec(p.tp) or None,
                    profit=_dec(p.profit),
                    swap=_dec(getattr(p, "swap", 0)),
                    magic=p.magic,
                )
            )
        return out

    async def get_orders(self, magic: int | None = None) -> list[BrokerOrder]:
        mt5 = self._require()
        rows = await self._call(mt5.orders_get) or []
        return [
            BrokerOrder(
                order_id=str(o.ticket),
                symbol=o.symbol,
                side="buy" if o.type in (0, 2, 4) else "sell",
                volume=Decimal(str(o.volume_current)),
                price=_dec(o.price_open),
                state=str(getattr(o, "state", "")),
                placed_at=datetime.fromtimestamp(o.time_setup),
                magic=o.magic,
            )
            for o in rows
            if magic is None or o.magic == magic
        ]

    async def get_order_history(
        self, since: datetime, until: datetime | None = None, magic: int | None = None
    ) -> list[Deal]:
        mt5 = self._require()
        # history_end() pads past both clocks. Bounding with a local
        # datetime.now() silently drops every deal the server stamped later,
        # and reads as "no trades" rather than as a fault.
        end = until or await self._call(self._paper.history_end, mt5)
        deals = await self._call(mt5.history_deals_get, since, end) or []
        out = []
        for d in deals:
            if magic is not None and d.magic != magic:
                continue
            entry = {0: "in", 1: "out", 2: "inout"}.get(getattr(d, "entry", -1), "unknown")
            out.append(
                Deal(
                    deal_id=str(d.ticket),
                    position_id=str(d.position_id),
                    symbol=d.symbol,
                    entry=entry,
                    volume=Decimal(str(d.volume)),
                    price=Decimal(str(d.price)),
                    at=datetime.fromtimestamp(d.time),
                    profit=Decimal(str(d.profit)),
                    commission=Decimal(str(d.commission)),
                    swap=Decimal(str(d.swap)),
                    magic=d.magic,
                )
            )
        return out

    # --------------------------------------------------------------- write
    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Send through the toolkit's `place()`, which owns order construction.

        `place()` picks the filling mode from the symbol's bitmask, records the
        fill rather than the quote, checks the bracket straddles the actual
        fill and repairs it when it does not. None of that is duplicated here.
        """
        mt5 = self._require()
        paper = self._paper
        # The toolkit derives sl/tp from ATR multiples; the platform supplies
        # absolute levels. Passing 0 multiples means "use what I give you",
        # so the levels are applied after the fill by modify, keeping one
        # implementation of order construction rather than two.
        try:
            raw = await self._call(
                paper.place,
                mt5,
                request.symbol,
                request.side,
                float(request.volume),
                0.0,
                0.0,
                True,
                None,
                # OUR tag, not the toolkit's. It is what stops the harness
                # harvesting a position this platform opened.
                MAGIC,
                # `place()` refuses a live order with no risk decision behind
                # it -- the P2 fence on Path A. This path is not ungated and
                # must not be evaluated twice: the caller is `app/oms/service`,
                # whose `create()` takes an `Approval` that only `RiskEngine`
                # can build, so the engine has already ruled on this order
                # against the platform's own limits and portfolio. A second
                # verdict here would be computed from the harness's limits and
                # a different snapshot, and could refuse an order already
                # approved, booked and recorded.
                gate=_toolkit_risk_gate().APPROVED_UPSTREAM,
            )
        except Exception as exc:  # noqa: BLE001
            # We do not know whether the venue received it. That is UNKNOWN,
            # never a rejection: retrying a rejection is safe, retrying this
            # would double-send.
            return OrderResult(
                OrderStatus.unknown,
                f"send raised after dispatch: {type(exc).__name__}: {exc}"[:300],
                fill_source="broker",
            )

        status_text = raw.get("status")
        if status_text == "SENT" and raw.get("fill_price"):
            result = OrderResult(
                OrderStatus.accepted,
                raw.get("comment") or "filled",
                order_id=str(raw.get("order")) if raw.get("order") else None,
                position_id=str(raw.get("order")) if raw.get("order") else None,
                deal_id=str(raw["deal"]) if raw.get("deal") else None,
                fill_price=_dec(raw.get("fill_price")),
                filled_volume=_dec(raw.get("lot")),
                filled_at=utcnow(),
                retcode=raw.get("retcode"),
                fill_source="broker",
                raw=raw,
            )
        elif status_text == "SENT":
            # Accepted with no fill price is not a confirmation.
            result = OrderResult(
                OrderStatus.unknown,
                "venue reported SENT without a fill price",
                retcode=raw.get("retcode"),
                fill_source="broker",
                raw=raw,
            )
        else:
            result = OrderResult(
                OrderStatus.rejected,
                raw.get("comment") or str(status_text),
                retcode=raw.get("retcode"),
                fill_source="broker",
                raw=raw,
            )

        if result.is_accepted and (request.stop_loss or request.take_profit):
            await self.modify_order(
                result.position_id or "", request.stop_loss, request.take_profit
            )
        return result

    async def modify_order(
        self,
        position_id: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> OrderResult:
        mt5 = self._require()
        position = next(
            (p for p in await self.get_positions(None) if p.position_id == position_id), None
        )
        if position is None:
            return OrderResult(
                OrderStatus.rejected, f"no position {position_id}", fill_source="broker"
            )
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": int(position_id),
            "sl": float(stop_loss) if stop_loss is not None else 0.0,
            "tp": float(take_profit) if take_profit is not None else 0.0,
        }
        try:
            res = await self._call(mt5.order_send, request)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(
                OrderStatus.unknown, f"modify raised: {exc}"[:200], fill_source="broker"
            )
        done = getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            OrderStatus.accepted if done else OrderStatus.rejected,
            getattr(res, "comment", "") or ("levels modified" if done else "modify refused"),
            position_id=position_id,
            retcode=getattr(res, "retcode", None),
            fill_source="broker",
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        mt5 = self._require()
        try:
            res = await self._call(
                mt5.order_send, {"action": mt5.TRADE_ACTION_REMOVE, "order": int(order_id)}
            )
        except Exception as exc:  # noqa: BLE001
            return OrderResult(
                OrderStatus.unknown, f"cancel raised: {exc}"[:200], fill_source="broker"
            )
        done = getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            OrderStatus.accepted if done else OrderStatus.rejected,
            getattr(res, "comment", "") or "",
            order_id=order_id,
            retcode=getattr(res, "retcode", None),
            fill_source="broker",
        )

    async def close_position(self, position_id: str, volume: Decimal | None = None) -> OrderResult:
        """Close through the toolkit's `close_own`, which owns the close request."""
        mt5 = self._require()
        results = await self._call(
            self._paper.close_own,
            mt5,
            True,
            lambda p: str(p.ticket) == position_id,
            MAGIC,
        )
        if not results:
            return OrderResult(
                OrderStatus.rejected,
                f"no own position {position_id} to close",
                fill_source="broker",
            )
        row = results[0]
        # The venue's ticket for the close ORDER. Carried on every branch
        # below, including the ones that did not work: an `unknown` close is
        # settled by asking the venue about that ticket, and a close order with
        # no venue identifier is a record nothing can be traced from. Every
        # close before 2026-09-07 left `orders.broker_order_id` empty, because
        # `close_own` reported a retcode and a fill but never the ticket.
        close_order_id = str(row["order"]) if row.get("order") else None
        if row.get("status") == "CLOSED":
            # A close is confirmed by a FILL, never by a retcode. Returning
            # `accepted` with no price and no volume is what the OMS reads as
            # "the venue left this in accepted", so it parks the position
            # `unknown` and asks for reconciliation -- correct behaviour on an
            # answer that says nothing, and the reason every close needed
            # settling by hand until `close_own` started reporting the fill.
            fill_price = _dec(row.get("fill_price"))
            closed_volume = _dec(row.get("closed_volume"))
            if fill_price is None or not closed_volume:
                return OrderResult(
                    OrderStatus.unknown,
                    "venue reported the close done without a fill price or volume",
                    order_id=close_order_id,
                    position_id=position_id,
                    retcode=row.get("retcode"),
                    fill_source="broker",
                    raw=row,
                )
            return OrderResult(
                OrderStatus.accepted,
                "closed",
                order_id=close_order_id,
                position_id=position_id,
                deal_id=str(row["deal"]) if row.get("deal") else None,
                retcode=row.get("retcode"),
                fill_price=fill_price,
                filled_volume=closed_volume,
                filled_at=utcnow(),
                fill_source="broker",
                # `net` is profit + swap + commission as the server booked it.
                # Absent when the deal was not yet in history, and left absent
                # rather than derived.
                realized_pnl=_dec(row.get("net")),
                raw=row,
            )
        if row.get("status") == "no_quote":
            return OrderResult(
                OrderStatus.rejected, "no quote to close against", fill_source="broker", raw=row
            )
        return OrderResult(
            OrderStatus.rejected,
            str(row.get("status")),
            order_id=close_order_id,
            retcode=row.get("retcode"),
            fill_source="broker",
            raw=row,
        )
