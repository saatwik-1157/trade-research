"""The replay engine: one bar at a time, with L14's execution semantics.

L14 hands `tools/rule_backtest.simulate()` a whole signal vector and gets a
list of trades back. Replay cannot do that -- it has to stop between bars so a
user can pause, step and watch. So this is an **incremental** implementation of
the same rules, and the risk that creates is the one the level has to manage:
two financial engines that disagree.

That risk is answered by a test rather than by a promise.
`test_replay_matches_the_backtest_exactly` runs the same strategy over the same
bars through L14 and through this, and asserts the trade lists are identical --
same entries, same exits, same reasons, same net points. If they ever diverge,
that test fails and the divergence is a bug, not a nuance.

The rules, taken from `simulate()` and reproduced here deliberately rather than
approximately:

  * a signal at bar i-1 opens at **bar i's open**;
  * the bracket is entry ± multiple × ATR(i-1);
  * on each subsequent bar the **stop is checked before the target**, so a bar
    covering both books the loss;
  * a position that reaches `max_hold` bars exits at that bar's close;
  * after an exit at bar j the next entry may open no earlier than **j+1** --
    flat before the next entry;
  * the first entry is at index 60, matching `simulate()`'s own warm-up;
  * the last bar cannot open a position, matching `while i < n - 1`.

**Nothing here can reach a broker.** The engine holds no adapter, and a test
asserts no module under `app.replay` imports `app.brokers`, `MetaTrader5` or
`mt5_paper`. Replay execution is simulated by construction, not by
configuration -- there is no setting that could point it at a venue, because
there is no venue reference to point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.backtest.config import BacktestConfig
from app.marketdata.types import Bar
from app.risk.engine import (
    OrderProposal,
    PortfolioState,
    RiskEngine,
    RiskLimits,
    RiskVerdict,
)
from app.strategies.base import Candles, SignalType, Strategy

log = logging.getLogger("app.replay")

# `simulate()` starts at index 60 whatever it is given. Reproduced so the two
# engines agree on which bars can open a position.
FIRST_ENTRY_INDEX = 60


class EventKind(StrEnum):
    """What happened at one step. Ordered within a bar by sequence number."""

    bar = "bar"
    signal = "signal"
    risk = "risk"
    order = "order"
    fill = "fill"
    position_opened = "position_opened"
    position_closed = "position_closed"
    equity = "equity"


@dataclass(frozen=True)
class ReplayEvent:
    kind: EventKind
    sequence: int
    # Simulated market time, never the wall clock.
    at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "sequence": self.sequence,
            "simulated_time": self.at.isoformat(),
            **self.payload,
        }


@dataclass
class OpenPosition:
    """A simulated position. Never a broker position."""

    side: str  # "long" | "short"
    entry_index: int
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float

    def unrealised(self, price: float) -> float:
        return (price - self.entry_price) if self.side == "long" else (self.entry_price - price)


@dataclass
class ReplayPortfolio:
    """Balance, equity and the trade record for one session.

    Isolated per session by construction: the engine owns one of these and
    nothing global is touched, so two concurrent sessions cannot see each
    other's positions or balance.
    """

    initial_balance: float
    quantity: float
    balance: float = 0.0
    peak: float = 0.0
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.balance = self.initial_balance
        self.peak = self.initial_balance

    def record(self, trade: dict) -> None:
        self.trades.append(trade)
        self.balance += float(trade["net_money"])
        self.peak = max(self.peak, self.balance)

    def mark(self, at: datetime, unrealised_points: float) -> dict:
        equity = self.balance + unrealised_points * self.quantity
        drawdown = max(0.0, self.peak - equity)
        point = {
            "at": at.isoformat(),
            "balance": round(self.balance, 6),
            "equity": round(equity, 6),
            "unrealised_points": round(unrealised_points, 6),
            "drawdown": round(drawdown, 6),
            "drawdown_pct": round(drawdown / self.peak * 100, 6) if self.peak > 0 else 0.0,
        }
        self.equity_curve.append(point)
        return point


class ReplayEngine:
    """Walks bars, evaluates the strategy, simulates execution.

    Holds all of its state on the instance. There is no module-level mutable
    state anywhere in this package, which is what makes concurrent sessions
    safe rather than merely untested.
    """

    def __init__(
        self,
        config: BacktestConfig,
        strategy: Strategy,
        bars: list[Bar],
        atr: list[float],
        risk: RiskEngine | None = None,
        account_id: str | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.bars = bars
        self.atr = atr
        self.portfolio = ReplayPortfolio(
            initial_balance=float(config.initial_capital), quantity=float(config.quantity)
        )
        self.position: OpenPosition | None = None
        # The bar index before which no new entry may open. `simulate()`'s
        # `i = j + 1` after an exit, kept as explicit state.
        self.next_entry_allowed_from = FIRST_ENTRY_INDEX
        self.signals: list[int] = []
        # Wired at L17. The DEFAULT is a permissive engine, and that is a
        # deliberate choice rather than a weak one: `test_replay_matches_the_
        # backtest_exactly` compares a replay against `simulate()`, which has
        # no risk limits at all, so a replay that imposed limits by default
        # would no longer be reproducing the backtest. Pass `risk=` to replay
        # a strategy under real limits and watch the vetoes.
        self.risk = risk or RiskEngine(
            RiskLimits(one_position_per_symbol=False, require_stop_loss=False)
        )
        self.account_id = account_id
        self.risk_verdicts: list[RiskVerdict] = []
        self.risk_rejections = 0
        self._spread = float(config.costs.spread_points)
        self._slippage = float(config.costs.slippage_points)
        self._commission = float(config.costs.commission_per_trade)
        # Financing, on exactly L14's terms: OFF unless a long figure is given,
        # and the short side falls back to it. Every figure in this repository
        # was measured without financing, so a default that silently restated
        # them would make the history unreadable.
        self._swap: tuple[float, float] | None = None
        if config.costs.swap_long_per_night is not None:
            self._swap = (
                float(config.costs.swap_long_per_night),
                float(config.costs.swap_short_per_night or config.costs.swap_long_per_night),
            )
        self._stop_atr = float(config.stop_atr)
        self._target_atr = float(config.target_atr)
        self._max_hold = int(config.max_hold_bars)

    # ------------------------------------------------------------ one step

    def step(self, index: int, next_sequence) -> list[ReplayEvent]:  # noqa: ANN001
        """Process exactly one bar. Returns the events it produced, in order."""
        bar = self.bars[index]
        events: list[ReplayEvent] = [
            ReplayEvent(
                EventKind.bar,
                next_sequence(),
                bar.bar_time,
                {
                    "index": index,
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                    "volume": str(bar.volume) if bar.volume is not None else None,
                },
            )
        ]

        # 1. The strategy sees only bars up to and including this one. The
        #    prefix is the look-ahead guarantee, exactly as in L14: bar
        #    index+1 is not in the list it is handed.
        signal = self._evaluate(index, bar)
        self.signals.append(signal)
        if signal != 0:
            events.append(
                ReplayEvent(
                    EventKind.signal,
                    next_sequence(),
                    bar.bar_time,
                    {"direction": "buy" if signal > 0 else "sell", "index": index},
                )
            )

        # 2. An open position is checked against THIS bar before anything new
        #    is opened, which is the order `simulate()` walks in.
        if self.position is not None:
            closed = self._check_exit(index, bar, next_sequence)
            events.extend(closed)

        # 3. A new entry uses the PREVIOUS bar's signal and this bar's open.
        if self.position is None:
            opened = self._maybe_open(index, bar, next_sequence)
            events.extend(opened)

        unrealised = (
            self.position.unrealised(float(bar.close)) if self.position is not None else 0.0
        )
        point = self.portfolio.mark(bar.bar_time, unrealised)
        events.append(ReplayEvent(EventKind.equity, next_sequence(), bar.bar_time, point))
        return events

    # ------------------------------------------------------------ internals

    def _evaluate(self, index: int, bar: Bar) -> int:
        """Ask the strategy about this bar, over a prefix ending at it."""
        window = Candles.of(self.config.symbol, self.config.timeframe, self.bars[: index + 1])
        try:
            # `now` is SIMULATED time. A strategy that read the wall clock
            # would see the real present and could act on it; passing the bar's
            # own time is what makes a replayed decision the decision that
            # would have been made then.
            produced = self.strategy.generate_signal(window, now=bar.bar_time)
        except Exception as exc:  # noqa: BLE001 - one bad bar does not end the session
            log.warning(
                "strategy raised during replay",
                extra={
                    "event": "replay_strategy_error",
                    "index": index,
                    "error": type(exc).__name__,
                },
            )
            return 0
        if produced.signal_type is SignalType.entry_long:
            return 1
        if produced.signal_type is SignalType.entry_short:
            return -1
        return 0

    def _maybe_open(self, index: int, bar: Bar, next_sequence) -> list[ReplayEvent]:  # noqa: ANN001
        # `simulate()`'s `while i < n - 1`: the last bar cannot open.
        if index >= len(self.bars) - 1:
            return []
        if index < self.next_entry_allowed_from:
            return []
        if index == 0:
            return []
        signal = self.signals[index - 1]
        if signal == 0:
            return []
        atr = self.atr[index - 1]
        if atr is None or atr <= 0 or atr != atr:  # NaN-safe
            return []

        entry = float(bar.open)
        if signal > 0:
            stop, target = entry - self._stop_atr * atr, entry + self._target_atr * atr
            side = "long"
        else:
            stop, target = entry + self._stop_atr * atr, entry - self._target_atr * atr
            side = "short"

        # The risk gate. Wired at L17: **simulation is not an exemption**, and
        # a replay whose risk rules differed from paper's would be testing a
        # strategy under a safety architecture that does not exist anywhere
        # else.
        #
        # `now` is the BAR'S time, not the wall clock. That is what makes the
        # freshness check meaningful here rather than absurd: measured against
        # the machine's clock, every signal in a 2019 replay is four years
        # stale and nothing would ever trade. Simulated time is the only time
        # the strategy sees, and it is the only time risk sees too.
        verdict = self.risk.evaluate(
            OrderProposal(
                symbol=bar.symbol,
                side="buy" if side == "long" else "sell",
                mode="paper",
                volume=Decimal(str(self.portfolio.quantity)),
                entry_price=Decimal(str(entry)),
                stop_loss=Decimal(str(stop)),
                take_profit=Decimal(str(target)),
                account_id=self.account_id,
                strategy_id=self.strategy.metadata().key,
                signal_time=bar.bar_time,
            ),
            PortfolioState(
                equity=Decimal(str(self.portfolio.balance)),
                balance=Decimal(str(self.portfolio.balance)),
                open_positions=0 if self.position is None else 1,
                peak_equity=Decimal(str(self.portfolio.peak)),
            ),
            now=bar.bar_time,
        )
        self.risk_verdicts.append(verdict)
        if not verdict.approved:
            # A veto is a veto in replay exactly as in paper. The bar is
            # consumed so the same signal is not re-proposed forever.
            self.next_entry_allowed_from = index + 1
            return [
                ReplayEvent(
                    EventKind.risk,
                    next_sequence(),
                    bar.bar_time,
                    {
                        "decision": str(verdict.decision),
                        "reason": verdict.reason[:300],
                        "failed": [str(c.limit) for c in verdict.failed],
                        "side": side,
                    },
                )
            ]

        events = [
            ReplayEvent(
                EventKind.risk,
                next_sequence(),
                bar.bar_time,
                {
                    "decision": str(verdict.decision),
                    "reason": verdict.reason[:300],
                    "not_enforced": list(verdict.not_enforced),
                    "side": side,
                },
            ),
            ReplayEvent(
                EventKind.order,
                next_sequence(),
                bar.bar_time,
                {
                    "state": "filled",
                    "side": side,
                    "quantity": self.portfolio.quantity,
                    "requested_price": entry,
                    "simulated": True,
                    "venue": "replay simulator; no broker was contacted",
                },
            ),
        ]
        self.position = OpenPosition(
            side=side,
            entry_index=index,
            entry_time=bar.bar_time,
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
        )
        events.append(
            ReplayEvent(
                EventKind.position_opened,
                next_sequence(),
                bar.bar_time,
                {
                    "side": side,
                    "entry_price": entry,
                    "stop_loss": stop,
                    "take_profit": target,
                },
            )
        )
        return events

    def _check_exit(self, index: int, bar: Bar, next_sequence) -> list[ReplayEvent]:  # noqa: ANN001
        position = self.position
        assert position is not None
        held = index - position.entry_index + 1
        high, low, close = float(bar.high), float(bar.low), float(bar.close)

        if position.side == "long":
            hit_stop, hit_target = low <= position.stop_loss, high >= position.take_profit
        else:
            hit_stop, hit_target = high >= position.stop_loss, low <= position.take_profit

        # The stop is checked FIRST, so a bar covering both books the loss.
        # Intrabar order is unknown and resolving it in the strategy's favour is
        # the classic way a backtest flatters itself.
        if hit_stop:
            return self._close(index, bar, position.stop_loss, "sl", held, next_sequence)
        if hit_target:
            return self._close(index, bar, position.take_profit, "tp", held, next_sequence)
        if held >= self._max_hold:
            return self._close(index, bar, close, "timeout", held, next_sequence)
        return []

    def _close(
        self,
        index: int,
        bar: Bar,
        exit_price: float,
        reason: str,
        held: int,
        next_sequence,  # noqa: ANN001
    ) -> list[ReplayEvent]:
        position = self.position
        assert position is not None
        gross = (
            exit_price - position.entry_price
            if position.side == "long"
            else position.entry_price - exit_price
        )
        # Spread once per round trip, slippage always adverse -- the same
        # charges L14 applies, in the same order.
        #
        # Financing uses `swap.nights_between`, **called** rather than
        # reimplemented. Counting nights across a weekend with one weekday
        # billed triple is exactly the kind of arithmetic that would drift if
        # written twice, and the divergence would show up as an unexplainable
        # gap between a replay and a backtest of the same trade.
        nights, financing = 0, 0.0
        if self._swap is not None:
            from swap import nights_between

            # Epoch seconds, exactly as L14's runner passes them:
            # `int(b.bar_time.timestamp())`. `nights_between` counts midnights
            # between two integers, not two datetimes.
            nights = nights_between(
                int(position.entry_time.timestamp()), int(bar.bar_time.timestamp()), None
            )
            financing = nights * (self._swap[0] if position.side == "long" else self._swap[1])
        net_points = gross - self._spread - self._slippage + financing
        trade = {
            "side": position.side,
            "entry_time": position.entry_time.isoformat(),
            "exit_time": bar.bar_time.isoformat(),
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "bars_held": held,
            "gross_points": round(gross, 6),
            "net_points": round(net_points, 6),
            "exit_reason": reason,
            "nights": nights,
            "financing": round(financing, 6),
            "net_money": round(net_points * self.portfolio.quantity - self._commission, 6),
            "execution_mode": "REPLAY",
        }
        self.portfolio.record(trade)
        self.position = None
        # Flat before the next entry: `simulate()`'s `i = j + 1`.
        self.next_entry_allowed_from = index + 1
        return [
            ReplayEvent(
                EventKind.fill,
                next_sequence(),
                bar.bar_time,
                {"price": exit_price, "reason": reason, "simulated": True},
            ),
            ReplayEvent(EventKind.position_closed, next_sequence(), bar.bar_time, dict(trade)),
        ]

    def finalise(self, next_sequence) -> list[ReplayEvent]:  # noqa: ANN001
        """Close any position still open when the data runs out.

        `simulate()` books it as a `timeout` at the last bar it examined --
        its inner loop ends at `min(i + max_hold, n)` and falls through to
        `exit_px = c[j]`. Leaving the position open instead would make replay
        report one fewer trade than a backtest over the same bars, and the
        difference would surface as an unexplainable discrepancy rather than as
        a design choice.

        The alternative reading -- that a position open at the end is still
        open -- is defensible for a live feed and wrong here: a replay is a
        finite dataset, and the backtester's answer is the one already measured.
        """
        if self.position is None or not self.bars:
            return []
        last_index = len(self.bars) - 1
        bar = self.bars[last_index]
        return self._close(
            last_index,
            bar,
            float(bar.close),
            "timeout",
            last_index - self.position.entry_index + 1,
            next_sequence,
        )

    # -------------------------------------------------------------- summary

    def metrics(self) -> dict[str, object]:
        """Reuses L14's metric implementation. There is one of those, not two."""
        from app.backtest.runner import compute_metrics

        curve = self.portfolio.equity_curve or [
            {"drawdown": 0.0, "drawdown_pct": 0.0, "balance": self.portfolio.initial_balance}
        ]
        return compute_metrics(self.portfolio.trades, curve, self.config)

    def state(self) -> dict[str, object]:
        return {
            "balance": round(self.portfolio.balance, 6),
            "equity": (
                self.portfolio.equity_curve[-1]["equity"]
                if self.portfolio.equity_curve
                else self.portfolio.initial_balance
            ),
            "open_position": (
                {
                    "side": self.position.side,
                    "entry_price": self.position.entry_price,
                    "stop_loss": self.position.stop_loss,
                    "take_profit": self.position.take_profit,
                    "entry_time": self.position.entry_time.isoformat(),
                }
                if self.position
                else None
            ),
            "closed_trades": len(self.portfolio.trades),
            "signals": sum(1 for s in self.signals if s != 0),
        }


def atr_for(bars: list[Bar], period: int = 14) -> list[float]:
    """ATR over the whole series, from the toolkit's own function.

    Computed once up front rather than per bar, and that is safe **because ATR
    at index i depends only on bars up to i**. The engine only ever reads
    `atr[index - 1]`, so no future value is reachable. A test modifies the tail
    and asserts an earlier ATR value is unchanged.
    """
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    tools = os.path.join(root, "tools")
    if os.path.isdir(tools) and tools not in sys.path:
        sys.path.insert(0, tools)
    import numpy as np
    import rule_backtest

    values = rule_backtest.atr_series(
        np.array([float(b.high) for b in bars]),
        np.array([float(b.low) for b in bars]),
        np.array([float(b.close) for b in bars]),
        period,
    )
    return [float(v) for v in values]


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 8)))
