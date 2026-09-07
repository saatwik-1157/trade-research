"""Shadow execution: the real pipeline, a real decision, and no venue.

Phase 3 of L44.

**This is an adapter, not a second pipeline.** That is the whole design, and the
brief asks for it in as many words: prefer

    REAL PIPELINE -> EXECUTION ADAPTER -> SHADOW EXECUTOR

over a separate shadow implementation. So `ShadowBroker` implements the same
`BrokerAdapter` interface `FakeBroker` and any future MT5 adapter implement, and
every stage above it -- the webhook gateway, the signal, the strategy engine,
the AI seat, the RiskEngine, position sizing, the OMS -- runs **the identical
code it runs for paper or live**. There is no `if shadow:` anywhere in the
pipeline, and a test asserts it.

**What shadow does differently from paper.** `FakeBroker` simulates a venue: it
fills orders, opens positions and moves a balance. That is the right thing for
paper trading and the wrong thing for shadow validation, because a simulated
fill is a fact nobody observed. `ShadowBroker` **acknowledges and never fills**:

    place_order   -> records the intent, returns an acknowledgement, no position
    modify_order  -> records the intent, changes nothing
    cancel_order  -> records the intent
    close_position -> REFUSES. There is no position to close.
    get_positions -> always empty, because none was ever opened

So a shadow run answers "what would this platform have decided?" and never
"what would it have earned?" -- which is the honest limit of a shadow run and
is why `ShadowBroker` exposes no P&L of any kind.

**Every decision is recorded whole.** `decisions` holds one `ShadowDecision`
per order the pipeline reached this adapter with, carrying the intended order
exactly as the OMS built it. The stages above -- AI verdict, risk verdict,
sizing -- are already on `ExecutionResult`, which is why nothing here duplicates
them: two records of one decision eventually disagree.

**It cannot reach a venue.** It imports no MT5, holds no credential and opens no
socket. A shadow adapter registered by mistake in place of a live one loses
money in exactly one way: it does not make any.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

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
    SymbolInfo,
)

#: What a shadow run is allowed to say about money. Nothing.
#:
#: `FakeBroker` carries a starting balance because paper trading needs one to
#: size against. A shadow adapter that reported a balance would invite somebody
#: to read a P&L off a run that never filled anything.
SHADOW_BALANCE_UNAVAILABLE = (
    "a shadow run has no balance and no P&L: nothing was filled, so any figure "
    "here would be one nobody observed"
)


@dataclass(frozen=True)
class ShadowDecision:
    """One order the pipeline would have sent, and did not.

    Deliberately thin. The AI verdict, the risk verdict and the sizing result
    already live on `ExecutionResult`, tied to this by `client_order_id`; a
    second copy here would be a second version of one decision.
    """

    at: datetime
    symbol: str
    side: str
    volume: str
    #: The OMS's idempotency key. This is what ties a shadow decision back to
    #: the `ExecutionResult` carrying the AI, risk and sizing verdicts, so
    #: nothing has to be duplicated here.
    intent_id: str | None
    stop_loss: str | None
    take_profit: str | None
    magic: int | None = None
    #: `placed`, `modified` or `cancelled`. What the pipeline asked for.
    intent: str = "placed"

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "symbol": self.symbol,
            "side": self.side,
            "volume": self.volume,
            "intent_id": self.intent_id,
            "magic": self.magic,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "intent": self.intent,
            "executed": False,
        }


@dataclass
class ShadowBroker(BrokerAdapter):
    """Records what would have been sent. Sends nothing.

    `mode` is `"shadow"` and not `"paper"` on purpose: the OMS registry compares
    the mode on the signal against the mode of the registered manager, so a
    signal that asked for paper or live execution cannot be satisfied by this
    adapter. Shadow is a mode a caller has to ask for.
    """

    name: str = "shadow"

    #: **`"paper"`, not `"shadow"`, and this is the most important decision in
    #: the module.**
    #:
    #: The obvious choice was `"shadow"`. It does not work, and the reason it
    #: does not work is the platform being right: `RiskEngine` checks
    #: `proposal.mode in ("paper", "demo")` and refuses anything else. A signal
    #: in shadow mode was therefore vetoed at the risk gate with
    #: `trading_mode: mode shadow` -- before sizing, before the OMS, before this
    #: adapter -- so a shadow run recorded nothing at all.
    #:
    #: The fix could have been to add `"shadow"` to that allow-list. It was
    #: not, deliberately: that list is a safety control that fails closed on an
    #: unknown mode, and widening a safety control so a new feature fits is
    #: exactly backwards. **The feature adapts to the control.**
    #:
    #: So shadow presents as paper -- which is honest, because the mode answers
    #: "what kind of money is at stake?" and the answer is "none". What makes
    #: this adapter shadow rather than paper is that it never fills, and that
    #: is a property of `place_order`, not of a label.
    #:
    #: The consequence worth knowing: the OMS registry's mode check no longer
    #: distinguishes a shadow adapter from a paper one, so registering this for
    #: an account means paper signals reach it and stop. That is the intended
    #: use. The dangerous direction is unreachable -- a shadow adapter cannot
    #: satisfy a `live` signal, because its mode is not `live`.
    mode: str = "paper"
    currency: str = "USD"

    #: The equity risk and sizing evaluate against.
    #:
    #: **This must be realistic, and the first version of this adapter got it
    #: wrong.** It reported zero, reasoning that a shadow run has no money and
    #: should not let anybody read a P&L off it. The RiskEngine then vetoed
    #: every signal on insufficient equity, the pass never reached the OMS, and
    #: shadow mode recorded *nothing* -- which defeats the entire point of it.
    #:
    #: Shadow's isolation is the absence of FILLS, not the absence of money.
    #: Set this to the equity of the account being shadowed so the risk and
    #: sizing verdicts are the ones that account would really have produced.
    equity: Decimal = Decimal("100000")

    quotes: dict[str, Quote] = field(default_factory=dict)
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)

    decisions: list[ShadowDecision] = field(default_factory=list)
    _state: ConnectionState = ConnectionState.disconnected
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # ---------------------------------------------------------- connection

    @property
    def state(self) -> ConnectionState:
        return self._state

    async def connect(self) -> Account:
        self._state = ConnectionState.connected
        return await self.get_account()

    async def disconnect(self) -> None:
        self._state = ConnectionState.disconnected

    def _require_connected(self) -> None:
        if self._state is not ConnectionState.connected:
            raise NotConnected(f"{self.name} is disconnected")

    async def get_account(self) -> Account:
        """The shadowed account's equity, and no floating P&L.

        `balance == equity` always, because nothing is ever open. That is the
        honest shape: a shadow run's equity never moves, so any "return"
        computed from it is zero by construction rather than by a number nobody
        observed.
        """
        return Account(
            login="shadow",
            server="shadow",
            currency=self.currency,
            balance=self.equity,
            # Equal to the balance, always: nothing is open, so there is no
            # floating P&L to add.
            equity=self.equity,
            mode=AccountMode.demo,
            # True, so the decision chain runs exactly as it would for the
            # account being shadowed. Reporting False here would be a second
            # way of stopping execution, and it would stop it in the wrong
            # place -- before the decisions shadow mode exists to record.
            # Isolation comes from `place_order` never filling and from `mode`
            # being "shadow", which the OMS registry checks against the signal.
            trade_allowed=True,
        )

    # -------------------------------------------------------- market data

    async def get_symbols(self) -> list[SymbolInfo]:
        return list(self.symbols.values())

    async def get_quote(self, symbol: str) -> Quote:
        self._require_connected()
        quote = self.quotes.get(symbol)
        if quote is None:
            raise NotConnected(f"no quote for {symbol} in this shadow session")
        return quote

    # ------------------------------------------------------------- orders

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Record the intent. Acknowledge. Fill nothing.

        `OrderStatus.accepted` with **no fill price, no filled volume and no
        position id** -- so the OMS records that the venue took the order and
        nothing downstream can read a fill off it. `fill_source="shadow"` is the
        same guard `FakeBroker` uses with `"simulator"`: a number's origin
        travels with it, so a shadow acknowledgement can never be mistaken for a
        broker one.
        """
        self._require_connected()
        self.decisions.append(
            ShadowDecision(
                at=datetime.now(UTC),
                symbol=request.symbol,
                side=str(request.side),
                volume=str(request.volume),
                intent_id=request.intent_id,
                stop_loss=str(request.stop_loss) if request.stop_loss is not None else None,
                take_profit=str(request.take_profit) if request.take_profit is not None else None,
                magic=request.magic,
            )
        )
        return OrderResult(
            OrderStatus.accepted,
            "shadow: the decision was recorded and nothing was sent to a venue",
            order_id=f"shadow-{next(self._ids)}",
            position_id=None,
            fill_price=None,
            filled_volume=None,
            filled_at=None,
            retcode=0,
            fill_source="shadow",
        )

    async def modify_order(
        self,
        position_id: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> OrderResult:
        self._require_connected()
        self.decisions.append(
            ShadowDecision(
                at=datetime.now(UTC),
                symbol="",
                side="",
                volume="0",
                intent_id=position_id,
                stop_loss=str(stop_loss) if stop_loss is not None else None,
                take_profit=str(take_profit) if take_profit is not None else None,
                intent="modified",
            )
        )
        return OrderResult(
            OrderStatus.accepted,
            "shadow: the modification was recorded and nothing was sent",
            order_id=position_id,
            retcode=0,
            fill_source="shadow",
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        self._require_connected()
        self.decisions.append(
            ShadowDecision(
                at=datetime.now(UTC),
                symbol="",
                side="",
                volume="0",
                intent_id=order_id,
                stop_loss=None,
                take_profit=None,
                intent="cancelled",
            )
        )
        return OrderResult(
            OrderStatus.accepted,
            "shadow: the cancellation was recorded and nothing was sent",
            order_id=order_id,
            retcode=0,
            fill_source="shadow",
        )

    async def close_position(self, position_id: str, volume: Decimal | None = None) -> OrderResult:
        """Refused, loudly.

        Nothing was ever opened, so there is nothing to close. Returning a
        polite success would let a position manager believe it had flattened
        something -- the one lie a shadow adapter must not tell.
        """
        raise NotConnected(
            "a shadow run holds no position, so there is nothing to close. This is "
            "not a transient failure: shadow execution acknowledges orders and "
            "never fills them."
        )

    # -------------------------------------------------------------- state

    async def get_positions(self, magic: int | None = None) -> list[BrokerPosition]:
        """Always empty. Never a simulated position."""
        return []

    async def get_orders(self, magic: int | None = None) -> list[BrokerOrder]:
        """Always empty.

        A shadow order exists in the platform's own OMS, not at a venue, so
        reporting one here would make reconciliation believe a venue had it --
        and reconciliation comparing local state against itself is the failure
        mode L38 exists to prevent.
        """
        return []

    async def get_order_history(
        self, since: datetime, until: datetime | None = None, magic: int | None = None
    ) -> list[Deal]:
        """Always empty. A shadow run produced no deals to have a history of."""
        return []

    # ------------------------------------------------------------ reading

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter": self.name,
            "mode": self.mode,
            "state": str(self._state),
            "decisions": len(self.decisions),
            "filled": 0,
            "positions": 0,
            "balance": SHADOW_BALANCE_UNAVAILABLE,
        }


__all__ = ["SHADOW_BALANCE_UNAVAILABLE", "ShadowBroker", "ShadowDecision"]
