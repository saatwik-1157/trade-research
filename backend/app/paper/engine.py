"""The paper pipeline: one pass from a market event to a paper position.

    market data -> strategy -> signal -> AI -> RISK -> sizing -> OMS
                -> paper execution -> position -> portfolio -> trade -> journal

Every stage is an existing component. This module is the wiring, not a second
implementation of any of them:

| stage | who does it | built at |
|---|---|---|
| bars, validation, staleness | `app.marketdata` | L08 |
| the strategy | `app.strategies` (`Strategy`, registry) | L12 |
| the risk veto | `app.risk.RiskEngine` | L17's module, **first consumed here** |
| the volume | `app.sizing.calculate` | L18's module, **first consumed here** |
| the order | `app.paper.oms` | L16 |
| the fill | `app.paper.execution` | L16 |
| the money | `app.paper.portfolio` | L16 |

`app.risk` and `app.sizing` existed before this level as complete, tested-by-
nothing, imported-by-nothing modules. L16 is their first caller, which is why
this level also brings their tests.

Four properties this module is built around:

**Risk is not bypassable.** The only path to `oms.submit` is through
`RiskEngine.approve`, whose `Approval` token `submit` requires. There is no
branch that skips it -- not for manual orders, not when the AI is confident,
not when a kill switch is off.

**The AI layer can only subtract.** `AiFilter` returns a confidence and may
veto. It runs BEFORE risk and cannot see the risk engine, so there is no order
in which an AI opinion could overturn a veto. A test asserts an AI that
approves everything still cannot get a vetoed signal through.

**Stale data blocks new orders.** L08's `is_stale` is consulted on every pass,
and a stale series refuses the entry rather than trading on the last price it
happened to have. "The feed stopped" and "the market stopped" look identical
from one timestamp, which is why both `at` and `received_at` exist.

**The same signal produces one order.** The signal key is derived from
(account, strategy, symbol, bar time, signal type), so a bar processed twice
is idempotent all the way through to the OMS.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.ai.decision import SignalContext
from app.backtest.config import CostModel
from app.execution.ai import AiFilter as AiFilter
from app.execution.ai import AiVerdict as AiVerdict
from app.execution.outcome import NO_ORDER as NO_ORDER
from app.execution.outcome import Outcome as Outcome
from app.marketdata.types import Bar, Quote, Timeframe
from app.marketdata.validation import is_stale
from app.paper.clock import now_utc
from app.paper.execution import PaperExecution, Reference
from app.paper.oms import OrderStatus, PaperOMS, PaperOrder
from app.paper.portfolio import (
    AccountNotTradeable,
    ClosedTrade,
    PaperPortfolio,
    check_tradeable,
    value_per_price_unit,
)
from app.paper.router import ExecutionMode, assert_paper_execution
from app.risk.engine import (
    Approval,
    OrderProposal,
    PortfolioState,
    RiskDecision,
    RiskEngine,
    RiskVerdict,
)
from app.sizing.calculator import SizingMethod, SizingRequest, SizingResult
from app.sizing.calculator import calculate as size_order
from app.strategies.base import Candles, SignalType, Strategy, StrategySignal
from app.symbols.service import ContractSpec

log = logging.getLogger("app.paper.engine")

# A strategy needs history before it has an opinion; below this the pass is
# reported as warming up rather than as "no signal", which would be a claim.
MIN_BARS = 60

ENTRIES = {SignalType.entry_long: "buy", SignalType.entry_short: "sell"}
EXITS = {SignalType.exit_long, SignalType.exit_short, SignalType.close}


# The outcome vocabulary moved to `app/execution/outcome.py` at L20 and is
# imported rather than restated. It was defined here, worked, and was extracted
# so the signal orchestrator could report in THE SAME words: a paper bot
# reporting `risk_vetoed` and an orchestrator reporting something else cannot
# be added together, and the first dashboard built over them would silently
# under-count one. Both names stay importable from this module, because
# `app.paper.service`, the API and the paper tests already import them here.


# ================================================================= AI seat
#
# Moved to `app/execution/ai.py` at L22 and imported rather than restated. It
# lived here, was imported by `app/execution/pipeline.py`, and the paper engine
# imports from that package -- a cycle that only fired when
# `app.paper.service` was the first module imported, so the test suite's
# import order hid it. Both names stay importable from here, because the paper
# engine, the paper tests and the execution tests already take them from here.


# ================================================================= one pass


@dataclass(frozen=True)
class Pass:
    """The full record of one pipeline pass, refusals included."""

    outcome: Outcome
    at: datetime
    symbol: str
    detail: str = ""
    signal: StrategySignal | None = None
    signal_key: str | None = None
    ai: AiVerdict | None = None
    risk: RiskVerdict | None = None
    sizing: SizingResult | None = None
    order: PaperOrder | None = None
    closed: tuple[ClosedTrade, ...] = ()

    @property
    def created_order(self) -> bool:
        return self.order is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": str(self.outcome),
            "at": self.at.isoformat(),
            "symbol": self.symbol,
            "detail": self.detail,
            "execution_mode": "PAPER",
            "signal_key": self.signal_key,
            "signal": self.signal.as_dict() if self.signal is not None else None,
            "ai": self.ai.as_dict() if self.ai is not None else None,
            "risk": (
                {
                    "decision": str(self.risk.decision),
                    "reason": self.risk.reason,
                    "failed": [str(c.limit) for c in self.risk.failed],
                    "not_enforced": list(self.risk.not_enforced),
                }
                if self.risk is not None
                else None
            ),
            "sizing": (
                {
                    "method": str(self.sizing.method),
                    "volume": str(self.sizing.volume) if self.sizing.volume else None,
                    "reason": self.sizing.reason,
                    "gap": self.sizing.gap,
                }
                if self.sizing is not None
                else None
            ),
            "order": self.order.as_dict() if self.order is not None else None,
            "closed_trades": [t.as_dict() for t in self.closed],
        }


@dataclass
class Counters:
    passes: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)

    def record(self, outcome: Outcome) -> None:
        self.passes += 1
        self.by_outcome[str(outcome)] = self.by_outcome.get(str(outcome), 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {"passes": self.passes, "by_outcome": dict(self.by_outcome)}


# ================================================================== engine


class PaperEngine:
    """One account, one symbol, one strategy. All state on the instance.

    Two engines never share anything, so two bots cannot cross accounts. The
    concurrency test asserts it rather than assuming it.
    """

    def __init__(
        self,
        *,
        portfolio: PaperPortfolio,
        strategy: Strategy,
        spec: ContractSpec,
        risk: RiskEngine,
        costs: CostModel,
        timeframe: Timeframe,
        sizing_method: SizingMethod = SizingMethod.fixed_quantity,
        quantity: Decimal | None = None,
        risk_amount: Decimal | None = None,
        risk_percent: Decimal | None = None,
        stop_atr_multiple: Decimal = Decimal("1.5"),
        target_atr_multiple: Decimal = Decimal("1.5"),
        ai: AiFilter | None = None,
        bot_id: str | None = None,
        strategy_id: str | None = None,
        day_start: datetime | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.strategy = strategy
        self.spec = spec
        self.risk = risk
        self.costs = costs
        self.timeframe = timeframe
        self.execution = PaperExecution(costs)
        self.oms = PaperOMS(self.execution)
        self.sizing_method = sizing_method
        self.quantity = quantity
        self.risk_amount = risk_amount
        self.risk_percent = risk_percent
        self.stop_atr_multiple = stop_atr_multiple
        self.target_atr_multiple = target_atr_multiple
        self.ai = ai
        #: The closed bars of the pass in flight, set by `process` before the
        #: strategy runs. Read only by `_ai_context`.
        self._closed_bars: tuple[Bar, ...] = ()
        self.bot_id = bot_id
        self.strategy_id = strategy_id or strategy.metadata().key
        self.counters = Counters()
        # Set by the runner from `RiskService.limits_for`, so a decision can
        # be traced to the configuration version it was made under.
        self.configuration_version = 0
        # The verdicts this pass produced, for the runner to persist.
        self.pending_verdicts: list[RiskVerdict] = []
        self.passes: list[Pass] = []
        # Idempotency. A bar processed twice produces one order.
        self.seen_signals: set[str] = set()
        self._day_start = day_start
        self.last_price: Decimal | None = None
        # Everything this engine ever proposed to Risk, approvals included.
        self.risk_events: list[dict[str, object]] = []

    # ----------------------------------------------------------- day boundary

    def day_start(self, now: datetime) -> datetime:
        """The start of the trading day, for the daily-loss limit.

        Explicit, never the machine's local midnight. This repository has the
        bug already: `.replace(hour=0)` on a server-rendered stamp counted from
        18:30 the previous day on a UTC+5:30 laptop and from midnight on a UTC
        one, so a risk limit moved with the operator's timezone. UTC midnight
        is the documented default here and `day_start` overrides it.
        """
        if self._day_start is not None:
            return self._day_start
        return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    # ------------------------------------------------------------ signal key

    def signal_key(self, signal: StrategySignal) -> str:
        """Stable across restarts, so a replayed bar cannot double-order.

        Derived from the account, strategy, symbol, bar time and signal type --
        every part of what makes this decision this decision. A random id would
        make every restart look like new information.
        """
        raw = "|".join(
            [
                self.portfolio.account_id,
                self.strategy_id,
                signal.symbol,
                str(self.timeframe),
                signal.bar_time.isoformat(),
                str(signal.signal_type),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    # ------------------------------------------------------------- one pass

    def process(
        self,
        bars: list[Bar],
        *,
        quote: Quote | None = None,
        now: datetime | None = None,
        market_open: bool | None = None,
    ) -> Pass:
        """Run the pipeline once over the bars available so far.

        `bars` must end at the most recently CLOSED bar. The strategy sees this
        list and no more, which is the same no-look-ahead guarantee L14 and L15
        make -- here it costs nothing, because the future has not happened.
        """
        now = now or now_utc()
        symbol = self.spec.internal_symbol

        if len(bars) < MIN_BARS:
            return self._record(
                Pass(
                    Outcome.warming_up,
                    now,
                    symbol,
                    f"{len(bars)} bars; {MIN_BARS} are needed before a signal is trusted",
                )
            )

        newest = bars[-1]
        self.last_price = newest.close

        # 1. The feed. A stale series blocks NEW orders; it does not close
        #    anything, because closing on stale data is also trading on it.
        if is_stale(newest.bar_time, self.timeframe, now, market_open=market_open):
            age = (now - newest.bar_time).total_seconds()
            return self._record(
                Pass(
                    Outcome.market_data_stale,
                    now,
                    symbol,
                    f"newest bar is {age:.0f}s old at {self.timeframe}; refusing to "
                    "open on data this old rather than trading the last price we happen to have",
                )
            )

        # 2. The strategy. An exception stops this engine, never the platform.
        # The SAME closed-bar window the strategy sees is what the AI seat is
        # later given -- built once, here, so the two can never diverge and the
        # seat cannot reach for a bar the strategy did not have (L27 §16).
        candles = Candles.of(self.spec.internal_symbol, self.timeframe, bars)
        self._closed_bars = candles.bars
        try:
            signal = self.strategy.generate_signal(candles, now=now)
        except Exception as exc:  # noqa: BLE001 - isolated on purpose
            log.warning(
                "paper strategy raised",
                extra={
                    "event": "paper_strategy_error",
                    "paper_account_id": self.portfolio.account_id,
                    "bot_id": self.bot_id,
                    "strategy_id": self.strategy_id,
                    "error": type(exc).__name__,
                    "execution_mode": "PAPER",
                },
            )
            return self._record(
                Pass(Outcome.strategy_error, now, symbol, f"{type(exc).__name__}: {exc}"[:200])
            )

        if signal.signal_type is SignalType.no_signal:
            return self._record(Pass(Outcome.no_signal, now, symbol, signal.reasoning[:200]))
        if signal.signal_type is SignalType.hold:
            return self._record(
                Pass(Outcome.hold, now, symbol, signal.reasoning[:200], signal=signal)
            )

        key = self.signal_key(signal)
        if key in self.seen_signals:
            return self._record(
                Pass(
                    Outcome.duplicate_signal,
                    now,
                    symbol,
                    "this bar's signal has already been processed; one logical order",
                    signal=signal,
                    signal_key=key,
                )
            )

        reference = Reference.from_quote(quote) if quote is not None else Reference.from_bar(newest)

        # 3. Exits first. Closing an existing position is not a new order and
        #    is deliberately NOT gated on the account being tradeable: an
        #    account that has been paused must still be able to flatten.
        if signal.signal_type in EXITS:
            return self._close_position(signal, key, reference, now)

        # 4. Entries.
        return self._open_position(signal, key, reference, now)

    # --------------------------------------------------------------- closing

    def _close_position(
        self, signal: StrategySignal, key: str, reference: Reference, now: datetime
    ) -> Pass:
        symbol = self.spec.internal_symbol
        position = self.portfolio.positions.get(symbol)
        if position is None:
            return self._record(
                Pass(
                    Outcome.no_signal,
                    now,
                    symbol,
                    "an exit signal with nothing open; nothing to do",
                    signal=signal,
                    signal_key=key,
                )
            )
        self.seen_signals.add(key)
        side = "sell" if position.side == "long" else "buy"
        try:
            fill = self.execution.fill(
                mode=ExecutionMode.paper,
                side=side,
                quantity=position.quantity,
                reference=reference,
                tick_size=self.spec.tick_size,
                price_precision=self.spec.price_precision,
                at=now,
            )
        except Exception as exc:  # noqa: BLE001
            return self._record(
                Pass(
                    Outcome.execution_rejected,
                    now,
                    symbol,
                    f"{type(exc).__name__}: {exc}"[:200],
                    signal=signal,
                    signal_key=key,
                )
            )
        closed = self.portfolio.apply_fill(
            symbol=symbol,
            side=side,
            quantity=fill.quantity,
            price=fill.price,
            commission=fill.commission,
            at=now,
            value_per_unit=position.value_per_unit,
            exit_reason=str(signal.signal_type),
            strategy_id=self.strategy_id,
        )
        return self._record(
            Pass(
                Outcome.position_closed,
                now,
                symbol,
                f"closed {position.quantity} at {fill.price}",
                signal=signal,
                signal_key=key,
                closed=tuple(closed),
            )
        )

    # ------------------------------------------------------------- the AI seat

    def _ai_context(self, signal: StrategySignal) -> SignalContext:
        """The normalized context the AI layer is permitted to see. L27 §17.

        `bars` are the closed bars ending at the signal's own bar time -- the
        same window the strategy read. Nothing here fetches data, which is what
        makes look-ahead impossible rather than merely forbidden.
        """
        return SignalContext(
            strategy_key=signal.strategy_key,
            strategy_version=signal.strategy_version,
            symbol=self.spec.internal_symbol,
            timeframe=str(self.timeframe),
            bar_time=signal.bar_time,
            side=signal.direction,
            entry_price=signal.reference_price,
            stop_loss=signal.suggested_stop_loss,
            take_profit=signal.suggested_take_profit,
            strategy_score=(float(signal.confidence) if signal.confidence is not None else None),
            bars=self._closed_bars,
            metadata={"signal_type": str(signal.signal_type), "bot_id": self.bot_id},
        )

    # --------------------------------------------------------------- opening

    def _open_position(
        self, signal: StrategySignal, key: str, reference: Reference, now: datetime
    ) -> Pass:
        symbol = self.spec.internal_symbol
        side = ENTRIES[signal.signal_type]

        # The account must be able to accept an order at all.
        try:
            check_tradeable(self.portfolio.state)
        except AccountNotTradeable as exc:
            return self._record(
                Pass(Outcome.account_not_tradeable, now, symbol, str(exc), signal, key)
            )

        # The AI seat. It runs BEFORE risk and can only decline.
        #
        # It is handed a normalized `SignalContext` (L27 §17) rather than the
        # strategy signal: the AI layer sees what it is PERMITTED to use, and a
        # type carrying the whole signal would let a future model reach for a
        # field nobody meant it to have. The bars on it are the same closed
        # window the strategy was given.
        ai_verdict: AiVerdict | None = None
        if self.ai is not None:
            ai_verdict = self.ai.score(self._ai_context(signal))
            if not ai_verdict.accept:
                return self._record(
                    Pass(
                        Outcome.ai_rejected,
                        now,
                        symbol,
                        ai_verdict.reason[:200] or "the AI filter declined this signal",
                        signal,
                        key,
                        ai=ai_verdict,
                    )
                )

        # The value of one price unit. Missing spec fields refuse.
        try:
            per_unit = value_per_price_unit(self.spec.tick_value, self.spec.tick_size)
        except Exception as exc:  # noqa: BLE001
            return self._record(
                Pass(Outcome.spec_incomplete, now, symbol, str(exc)[:200], signal, key, ai_verdict)
            )

        entry = reference.ask if side == "buy" and reference.ask else reference.last
        entry = entry if entry is not None else signal.reference_price
        if entry is None:
            return self._record(
                Pass(
                    Outcome.spec_incomplete,
                    now,
                    symbol,
                    "no reference price for the entry; refusing to size against a guess",
                    signal,
                    key,
                    ai_verdict,
                )
            )

        stop, target = self._bracket(signal, side, Decimal(entry))
        stop_distance = abs(Decimal(entry) - stop) if stop is not None else None

        # Sizing. `app.sizing` computes the volume; nothing here duplicates it.
        sizing = size_order(
            SizingRequest(
                method=self.sizing_method,
                spec=self.spec,
                quantity=self.quantity,
                risk_amount=self.risk_amount,
                equity=self.portfolio.equity(self._prices()),
                risk_percent=self.risk_percent,
                stop_distance=stop_distance,
                # The bracket is handed over as levels as well as a distance
                # (L18). The engine validates stop placement against the side
                # and refuses a long stop above its entry, which a distance
                # alone cannot express -- `abs()` makes a target look like a
                # stop, and this pipeline would have sized one.
                side=side,
                entry_price=Decimal(entry),
                stop_loss=stop,
                take_profit=target,
            )
        )
        if sizing.refused or sizing.volume is None:
            return self._record(
                Pass(
                    Outcome.sizing_refused,
                    now,
                    symbol,
                    sizing.gap or "sizing refused",
                    signal,
                    key,
                    ai_verdict,
                    sizing=sizing,
                )
            )

        # RISK. The only path to an order runs through here.
        proposal = OrderProposal(
            symbol=symbol,
            side=side,
            mode="paper",
            volume=sizing.volume,
            entry_price=Decimal(entry),
            stop_loss=stop,
            take_profit=target,
            risk_amount=sizing.risk_actual,
            spread_points=reference.ask - reference.bid
            if reference.ask is not None and reference.bid is not None
            else None,
            account_id=self.portfolio.account_id,
            strategy_id=self.strategy_id,
            signal_id=key,
            signal_time=signal.bar_time,
            base_currency=self.portfolio.currency,
        )
        approval, verdict = self.risk.approve(
            proposal,
            self._portfolio_state(now),
            now=now,
            recent_signal_ids=tuple(self.seen_signals),
        )
        self._record_risk(verdict, now)
        self.pending_verdicts.append(verdict)

        if approval is None:
            outcome = (
                Outcome.risk_halted
                if verdict.decision is RiskDecision.halt
                else Outcome.risk_vetoed
            )
            if any(str(c.limit).startswith("kill_switch") for c in verdict.failed):
                outcome = Outcome.kill_switch
            # A vetoed signal is still consumed: re-proposing the identical
            # signal next pass would re-run the same veto forever.
            self.seen_signals.add(key)
            return self._record(
                Pass(outcome, now, symbol, verdict.reason[:300], signal, key, ai_verdict, verdict)
            )

        self.seen_signals.add(key)
        return self._submit_and_fill(
            approval, signal, key, reference, now, ai_verdict, verdict, sizing, per_unit
        )

    def _submit_and_fill(
        self,
        approval: Approval,
        signal: StrategySignal,
        key: str,
        reference: Reference,
        now: datetime,
        ai_verdict: AiVerdict | None,
        verdict: RiskVerdict,
        sizing: SizingResult,
        per_unit: Decimal,
    ) -> Pass:
        symbol = self.spec.internal_symbol
        submission = self.oms.submit(
            approval,
            order_id=str(uuid4()),
            # The intent id IS the signal key, so a duplicate signal and a
            # duplicate order are the same fact rather than two mechanisms.
            intent_id=key,
            account_id=self.portfolio.account_id,
            bot_id=self.bot_id,
            at=now,
        )
        order = submission.order
        order.sizing_snapshot = {
            "method": str(sizing.method),
            "volume": str(sizing.volume),
            "risk_requested": str(sizing.risk_requested) if sizing.risk_requested else None,
            "risk_actual": str(sizing.risk_actual) if sizing.risk_actual else None,
        }
        if submission.duplicate:
            return self._record(
                Pass(
                    Outcome.duplicate_signal,
                    now,
                    symbol,
                    "the same intent already has an order; one logical order",
                    signal,
                    key,
                    ai_verdict,
                    verdict,
                    sizing,
                    order,
                )
            )

        self.oms.execute(
            order,
            reference,
            tick_size=self.spec.tick_size,
            price_precision=self.spec.price_precision,
            at=now,
        )
        if order.status is not OrderStatus.filled or order.fill is None:
            return self._record(
                Pass(
                    Outcome.execution_rejected,
                    now,
                    symbol,
                    order.reason[:200],
                    signal,
                    key,
                    ai_verdict,
                    verdict,
                    sizing,
                    order,
                )
            )

        closed = self.portfolio.apply_fill(
            symbol=symbol,
            side=order.side,
            quantity=order.fill.quantity,
            price=order.fill.price,
            commission=order.fill.commission,
            at=now,
            value_per_unit=per_unit,
            exit_reason="reversal",
            strategy_id=self.strategy_id,
        )
        position = self.portfolio.positions.get(symbol)
        if position is not None:
            position.stop_loss = order.stop_loss
            position.take_profit = order.take_profit
        return self._record(
            Pass(
                Outcome.filled,
                now,
                symbol,
                f"filled {order.fill.quantity} at {order.fill.price}",
                signal,
                key,
                ai_verdict,
                verdict,
                sizing,
                order,
                tuple(closed),
            )
        )

    # ---------------------------------------------------------- the bracket

    def _bracket(
        self, signal: StrategySignal, side: str, entry: Decimal
    ) -> tuple[Decimal | None, Decimal | None]:
        """Stop and target, from the strategy's suggestion or an ATR multiple.

        A strategy's suggestion is used when it made one -- unlike L14, which
        owns the bracket because `simulate()` does. Where there is none, the
        ATR multiples are used, and where there is no ATR either the stop is
        None and the risk engine's `require_stop_loss` decides what that means.
        Nothing here invents a level.
        """
        if signal.suggested_stop_loss is not None:
            return signal.suggested_stop_loss, signal.suggested_take_profit
        atr = signal.metadata.get("atr")
        if atr is None:
            return None, None
        distance = Decimal(str(atr))
        if side == "buy":
            return (
                entry - distance * self.stop_atr_multiple,
                entry + distance * self.target_atr_multiple,
            )
        return (
            entry + distance * self.stop_atr_multiple,
            entry - distance * self.target_atr_multiple,
        )

    # ------------------------------------------------------- position checks

    def check_brackets(self, bar: Bar, now: datetime | None = None) -> Pass | None:
        """Close a position whose stop or target the bar covered.

        **The stop is checked before the target**, so a bar covering both books
        the loss. That is L14's rule and L15's rule, and it is here for the
        same reason: intrabar order is unknown, and resolving it favourably is
        how a simulation flatters itself.
        """
        now = now or now_utc()
        symbol = self.spec.internal_symbol
        position = self.portfolio.positions.get(symbol)
        if position is None:
            return None
        hit: tuple[Decimal, str] | None = None
        if position.side == "long":
            if position.stop_loss is not None and bar.low <= position.stop_loss:
                hit = (position.stop_loss, "stop_loss")
            elif position.take_profit is not None and bar.high >= position.take_profit:
                hit = (position.take_profit, "take_profit")
        else:
            if position.stop_loss is not None and bar.high >= position.stop_loss:
                hit = (position.stop_loss, "stop_loss")
            elif position.take_profit is not None and bar.low <= position.take_profit:
                hit = (position.take_profit, "take_profit")
        if hit is None:
            return None

        price, reason = hit
        side = "sell" if position.side == "long" else "buy"
        closed = self.portfolio.apply_fill(
            symbol=symbol,
            side=side,
            quantity=position.quantity,
            price=price,
            commission=Decimal(self.costs.commission_per_trade),
            at=now,
            value_per_unit=position.value_per_unit,
            exit_reason=reason,
            strategy_id=self.strategy_id,
        )
        return self._record(
            Pass(
                Outcome.position_closed,
                now,
                symbol,
                f"{reason} at {price}",
                closed=tuple(closed),
            )
        )

    # ------------------------------------------------------------- plumbing

    def _prices(self) -> dict[str, Decimal]:
        if self.last_price is None:
            return {}
        return {self.spec.internal_symbol: self.last_price}

    def _portfolio_state(self, now: datetime) -> PortfolioState:
        prices = self._prices()
        start = self.day_start(now)
        return PortfolioState(
            equity=self.portfolio.equity(prices),
            balance=self.portfolio.balance,
            open_positions=len(self.portfolio.positions),
            open_symbols=frozenset(self.portfolio.positions),
            realised_today=self.portfolio.realised_since(start),
            trades_today=self.portfolio.trades_since(start),
            peak_equity=self.portfolio.peak_equity,
            exposure_by_currency={self.portfolio.currency: self.portfolio.exposure(prices)},
        )

    def _record_risk(self, verdict: RiskVerdict, now: datetime) -> None:
        """Every decision, approvals included. A veto that leaves no trace is
        indistinguishable from a check that never ran."""
        self.risk_events.append(
            {
                "decision": str(verdict.decision),
                "reason": verdict.reason[:500],
                "occurred_at": now.isoformat(),
                "paper_account_id": self.portfolio.account_id,
                "signal_id": verdict.proposal.signal_id,
                "strategy_id": verdict.proposal.strategy_id,
                "symbol": verdict.proposal.symbol,
                "execution_mode": "PAPER",
                "failed": [str(c.limit) for c in verdict.failed],
                "not_enforced": list(verdict.not_enforced),
            }
        )

    def _record(self, result: Pass) -> Pass:
        self.counters.record(result.outcome)
        self.passes.append(result)
        del self.passes[:-500]
        return result

    # ------------------------------------------------------------ reporting

    def state(self, prices: dict[str, Decimal] | None = None) -> dict[str, object]:
        marks = prices if prices is not None else self._prices()
        return {
            **self.portfolio.snapshot(marks),
            "strategy_id": self.strategy_id,
            "bot_id": self.bot_id,
            "timeframe": str(self.timeframe),
            "counters": self.counters.as_dict(),
            "open_orders": len(self.oms.open_orders(self.portfolio.account_id)),
            "risk_events": len(self.risk_events),
            "positions": [p.as_dict(marks.get(s)) for s, p in self.portfolio.positions.items()],
        }


def assert_paper(engine: PaperEngine) -> None:
    """Belt and braces: the engine's execution provider must be the paper one."""
    assert_paper_execution(ExecutionMode.paper)
    if not isinstance(engine.execution, PaperExecution):
        raise TypeError("a PaperEngine must hold a PaperExecution and nothing else")
