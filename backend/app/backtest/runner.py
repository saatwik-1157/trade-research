"""The backtest runner. It wraps `tools/rule_backtest.simulate`; it is not a
second engine.

`simulate()` already implements the execution semantics this level asks for,
and `tests/test_rule_backtest.py` pins each of them:

  * entry at the **next bar's open**, from a signal that read bars up to i-1;
  * a bar covering both stop and target books the **loss**, because intrabar
    order is unknown and resolving it in the strategy's favour is the classic
    way a backtest flatters itself;
  * the spread charged **once** per round trip;
  * **flat before the next entry** -- no overlapping positions.

Rewriting that would mean re-deriving four constraints that took real trades to
learn. So this module does the part `simulate()` does not: it turns an
`app.strategies` Strategy into the signal vector `simulate()` consumes, and
turns the trades back into an equity curve, metrics and persisted rows.

**How the signal vector is built is where look-ahead would hide.** For each bar
index i, the strategy is given `Candles.of(bars[:i+1])` -- a prefix -- and asked
what it says. It cannot see bar i+1 because bar i+1 is not in the list it was
handed. `simulate()` then reads `sig[i-1]` and enters at `o[i]`, so the decision
at i is acted on at i+1's open. A test proves it: changing only the bars after T
leaves the decision at T identical.

That prefix walk is O(n * strategy cost) and it is the honest way to do this.
Computing indicators once over the whole array and slicing is faster and is
exactly how look-ahead gets introduced.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType
from typing import Any

from app.ai.decision import SignalContext
from app.analytics import metrics as _metrics
from app.backtest.config import ENGINE_VERSION, BacktestConfig, SizingMode
from app.core import toolkit
from app.marketdata.types import Bar
from app.marketdata.validation import find_duplicates, find_gaps, inspect_series
from app.sizing.calculator import SizingMethod, SizingRequest
from app.sizing.calculator import calculate as size_order
from app.strategies.base import Candles, SignalType, Strategy
from app.symbols.service import ContractSpec

log = logging.getLogger("app.backtest")

# `simulate()` starts at index 60 regardless of what it is given, so a series
# shorter than that produces nothing and would report "no trades" for a reason
# that has nothing to do with the strategy.
SIMULATE_WARMUP = 60


class BacktestError(Exception):
    """The run could not be performed. Never reported as an empty result."""


def _toolkit() -> ModuleType:
    """The simulator, from an installed tr_toolkit or the sibling tools/."""
    return toolkit.load("rule_backtest")


@dataclass(frozen=True)
class DataQuality:
    """What was wrong with the input, reported beside the result.

    A backtest over a gappy series is not invalid, but it is not the same
    measurement as one over a clean series, and a reader has to be able to tell.
    """

    bars: int
    duplicates: int
    gaps: int
    invalid_ohlc: int
    ohlc_trustworthy: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "bars": self.bars,
            "duplicates": self.duplicates,
            "gaps": self.gaps,
            "invalid_ohlc": self.invalid_ohlc,
            "ohlc_trustworthy": self.ohlc_trustworthy,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    trades: list[dict]
    equity_curve: list[dict]
    metrics: dict
    quality: DataQuality
    signals_generated: int
    bars_processed: int
    started_at: datetime
    finished_at: datetime
    # Entries the sizing engine refused. They are NOT trades -- a position that
    # could not be sized was never opened -- but a run that silently dropped
    # them would report a trade count nobody could reconcile against the
    # signal count, so they are carried and reported.
    sizing_refusals: list[dict] = field(default_factory=list)
    # L27. None means the run was AI_DISABLED -- which is the baseline, and is
    # byte-identical to a run from before the AI layer existed.
    ai: dict | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "config": self.config.describe(),
            "metrics": self.metrics,
            "quality": self.quality.as_dict(),
            "signals_generated": self.signals_generated,
            "bars_processed": self.bars_processed,
            "trades": self.trades,
            "equity_curve": self.equity_curve,
            "sizing_refusals": self.sizing_refusals,
            "ai": self.ai,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "engine_version": ENGINE_VERSION,
            "note": (
                "A simulation under the stated assumptions, not a prediction. No "
                "broker was contacted and no order was submitted."
            ),
        }


def build_signal_vector(
    strategy: Strategy, bars: list[Bar], symbol: str, timeframe: Any
) -> list[int]:
    """Ask the strategy about each bar, seeing only the bars up to it.

    Returns +1 / -1 / 0 per bar, the encoding `simulate()` expects.

    The prefix is the guarantee. At index i the strategy receives
    `bars[:i+1]` and physically cannot read further, so no indicator can be
    contaminated by a later value. This is slower than computing once over the
    whole array; computing once over the whole array is how look-ahead is
    introduced, so the cost is the point.
    """
    signals: list[int] = []
    for i in range(len(bars)):
        window = Candles.of(symbol, timeframe, bars[: i + 1])
        try:
            produced = strategy.generate_signal(window, now=bars[i].bar_time)
        except Exception as exc:  # noqa: BLE001 - one bad bar does not end the run
            log.warning(
                "strategy raised during a backtest bar",
                extra={
                    "event": "backtest_strategy_error",
                    "bar_index": i,
                    "error": type(exc).__name__,
                },
            )
            signals.append(0)
            continue
        if produced.signal_type is SignalType.entry_long:
            signals.append(1)
        elif produced.signal_type is SignalType.entry_short:
            signals.append(-1)
        else:
            # HOLD, NO_SIGNAL and every exit type are 0 here: `simulate()`
            # exits on the bracket, not on a strategy exit. That is a stated
            # limitation, not an oversight -- see the runner docstring.
            signals.append(0)
    return signals


def apply_ai_filter(
    signals: list[int],
    bars: list[Bar],
    strategy: Strategy,
    symbol: str,
    timeframe: Any,
    *,
    service: Any,
    config: Any,
) -> tuple[list[int], dict[str, Any]]:
    """The AI layer applied to a signal vector, bar by bar. L27 §31.

    **No future information reaches it.** At bar *i* the AI sees
    `bars[:i+1]` — the identical prefix `build_signal_vector` gave the
    strategy — because it is handed the same window rather than a slice
    computed separately. That is the whole reason this runs in a loop instead
    of vectorising: computing once over the whole array is how look-ahead is
    introduced, and the cost is the point.

    **It can only subtract.** A bar the strategy left flat stays flat; the AI
    layer is never asked, and there is no branch here that could turn a 0 into
    a ±1. A REJECT turns a ±1 into a 0. Section 2: AI enhances a decision, it
    does not originate one.

    Returns the filtered vector and the counters §31 asks for — accepted,
    rejected, the probability distribution and the failure modes — so a run
    can be reported beside its AI_DISABLED baseline.
    """
    if len(signals) != len(bars):
        raise BacktestError(f"{len(signals)} signals against {len(bars)} bars")

    filtered: list[int] = []
    accepted = rejected = neutral = errored = 0
    probabilities: list[float] = []
    statuses: dict[str, int] = {}
    decisions: list[dict[str, Any]] = []

    for index, raw in enumerate(signals):
        if raw == 0:
            # Never asked. A model that could turn a flat bar into a trade
            # would be originating one, which no mode in L27 permits.
            filtered.append(0)
            continue

        window = Candles.of(symbol, timeframe, bars[: index + 1])
        context = SignalContext(
            strategy_key=strategy.metadata().key,
            symbol=symbol,
            timeframe=str(timeframe),
            bar_time=bars[index].bar_time,
            side="buy" if raw > 0 else "sell",
            bars=window.bars,
        )
        decision = service.evaluate(context, config)
        statuses[decision.status] = statuses.get(decision.status, 0) + 1
        if decision.probability is not None:
            probabilities.append(decision.probability)

        if str(decision.decision) == "REJECT":
            rejected += 1
            filtered.append(0)
        else:
            if str(decision.decision) == "NEUTRAL":
                neutral += 1
            elif str(decision.decision) == "ERROR":
                errored += 1
            accepted += 1
            filtered.append(raw)

        if len(decisions) < 200:
            decisions.append(
                {
                    "bar_index": index,
                    "bar_time": bars[index].bar_time.isoformat(),
                    "side": context.side,
                    "decision": str(decision.decision),
                    "status": decision.status,
                    "probability": decision.probability,
                    "regime": decision.regime,
                    "reason": decision.reason[:200],
                }
            )

    offered = sum(1 for s in signals if s != 0)
    report: dict[str, Any] = {
        "mode": str(config.mode),
        "policy": str(config.policy),
        "signals_offered": offered,
        "accepted": accepted,
        "rejected": rejected,
        "neutral": neutral,
        "errors": errored,
        "acceptance_rate": round(accepted / offered, 6) if offered else None,
        "rejection_rate": round(rejected / offered, 6) if offered else None,
        "statuses": dict(sorted(statuses.items())),
        "probability_distribution": _distribution(probabilities),
        "decisions": decisions,
        "causality": (
            "at bar i the AI layer saw bars[:i+1] -- the same prefix the strategy saw. "
            "No future information reaches it, and a flat bar is never offered, so the "
            "layer can only remove trades and never add one."
        ),
        "comparison": (
            "run the same config at AI_DISABLED for the baseline. Fewer trades is the "
            "expected effect of a filter, and fewer trades is not by itself an "
            "improvement -- read the expectancy per trade beside the total."
        ),
    }
    return filtered, report


def _distribution(values: list[float], bins: int = 10) -> list[dict[str, Any]]:
    """Where the model's probabilities fell. Reported, never scored."""
    if not values:
        return []
    counts = [0] * bins
    for value in values:
        counts[min(int(value * bins), bins - 1)] += 1
    return [
        {"from": round(i / bins, 2), "to": round((i + 1) / bins, 2), "n": n}
        for i, n in enumerate(counts)
        if n
    ]


def run(
    config: BacktestConfig,
    strategy: Strategy,
    bars: list[Bar],
    spec: ContractSpec | None = None,
    *,
    ai_service: Any = None,
    ai_config: Any = None,
) -> BacktestResult:
    """Execute one backtest. Deterministic for a given config and dataset.

    `spec` is the instrument's measured contract terms and is required for the
    risk sizing modes: without a tick value there is no way to turn a stop
    distance into money, and sizing from a default is the failure `app.sizing`
    exists to prevent. `fixed_quantity` does not need it.
    """
    config.validate()
    started = datetime.now(UTC)
    toolkit = _toolkit()

    import numpy as np

    closed = [b for b in bars if b.complete]
    closed.sort(key=lambda b: b.bar_time)
    if len(closed) < SIMULATE_WARMUP + 2:
        raise BacktestError(
            f"{len(closed)} closed bars; the simulator needs more than "
            f"{SIMULATE_WARMUP} before it can open anything. This is a data "
            "shortfall, not a result"
        )

    quality_report = inspect_series(closed, config.timeframe)
    quality = DataQuality(
        bars=len(closed),
        duplicates=len(find_duplicates(closed)),
        gaps=find_gaps(closed, config.timeframe),
        invalid_ohlc=quality_report.invalid_ohlc,
        ohlc_trustworthy=quality_report.ohlc_trustworthy,
        notes=list(quality_report.notes),
    )
    if quality.invalid_ohlc:
        # Refused rather than silently repaired. A bar whose close sits outside
        # its own range produced a t-statistic of 28 in this repository's forex
        # pattern study -- an artefact that reads exactly like an edge.
        raise BacktestError(
            f"{quality.invalid_ohlc} of {len(closed)} bars have impossible OHLC. "
            "Refusing to run: a backtest over corrupt bars manufactures findings"
        )

    o = np.array([float(b.open) for b in closed])
    h = np.array([float(b.high) for b in closed])
    low = np.array([float(b.low) for b in closed])
    c = np.array([float(b.close) for b in closed])
    times = np.array([int(b.bar_time.timestamp()) for b in closed])

    signals = build_signal_vector(strategy, closed, config.symbol, config.timeframe)

    # L27 §31. Absent an AI service this is the AI_DISABLED baseline, and it is
    # byte-identical to every backtest run before this level -- `signals` is not
    # touched and `ai` stays None on the result.
    ai_report: dict[str, Any] | None = None
    if ai_service is not None and ai_config is not None:
        signals, ai_report = apply_ai_filter(
            signals,
            closed,
            strategy,
            config.symbol,
            config.timeframe,
            service=ai_service,
            config=ai_config,
        )

    atr = toolkit.atr_series(h, low, c, 14)

    swap = None
    if config.costs.swap_long_per_night is not None:
        swap = (
            float(config.costs.swap_long_per_night),
            float(config.costs.swap_short_per_night or config.costs.swap_long_per_night),
        )

    # The tested engine. Not reimplemented.
    raw_trades = toolkit.simulate(
        o,
        h,
        low,
        c,
        np.array(signals, dtype=int),
        atr,
        float(config.costs.spread_points),
        sl_atr=float(config.stop_atr),
        tp_atr=float(config.target_atr),
        max_hold=config.max_hold_bars,
        times=times,
        swap=swap,
    )

    trades, refusals = _enrich(raw_trades, closed, config, atr, spec)
    curve = _equity_curve(trades, config)
    metrics = compute_metrics(trades, curve, config)
    return BacktestResult(
        config=config,
        trades=trades,
        equity_curve=curve,
        metrics=metrics,
        quality=quality,
        signals_generated=sum(1 for s in signals if s != 0),
        bars_processed=len(closed),
        started_at=started,
        finished_at=datetime.now(UTC),
        sizing_refusals=refusals,
        ai=ai_report,
    )


def _enrich(
    raw: list[dict],
    bars: list[Bar],
    config: BacktestConfig,
    atr: Any,
    spec: ContractSpec | None,
) -> tuple[list[dict], list[dict]]:
    """Attach times, prices, size and per-trade money to the simulator's output.

    **Sizing runs here, trade by trade, and it is the same engine the paper
    pipeline uses.** `app.sizing.calculate` is called with the equity as it
    stood *before* the trade opened; there is no second formula, so a
    backtested size and a live size cannot drift apart.

    **No look-ahead, by construction.** Two facts enter each size and both
    predate the entry:

      * the stop distance is `stop_atr x atr[entry_idx - 1]`, which is the
        exact ATR `simulate()` itself used to place the bracket -- the bar
        BEFORE the entry bar, so it was knowable at the open that filled it;
      * the equity is the running balance from trades that had already closed.

    A trade whose size is refused is not a trade. It is removed from the
    result and recorded in the refusals list, because a position that could
    not be sized was never opened and counting it would report a return the
    account could not have earned.
    """
    out: list[dict] = []
    refusals: list[dict] = []
    commission = float(config.costs.commission_per_trade)
    slippage = float(config.costs.slippage_points)
    risk_sized = config.sizing_mode is not SizingMode.fixed_quantity

    if risk_sized and spec is None:
        raise BacktestError(
            f"sizing mode {config.sizing_mode} needs the instrument's contract spec "
            "(tick size, tick value, volume step) and none was supplied. Refusing "
            "rather than sizing from a default: a lot computed from an absent tick "
            "value is a real order for the wrong amount"
        )

    method = {
        SizingMode.fixed_quantity: SizingMethod.fixed_quantity,
        SizingMode.fixed_risk: SizingMethod.fixed_risk,
        SizingMode.percent_equity: SizingMethod.percent_equity,
    }[config.sizing_mode]

    # The account as it stands walking forward. Only closed trades move it.
    balance = config.initial_capital

    for trade in raw:
        entry_index = int(trade["entry_idx"])
        exit_index = min(entry_index + int(trade["bars"]) - 1, len(bars) - 1)
        # Slippage is charged here rather than inside `simulate()`, so the
        # tested engine is not modified. It is always adverse: a slippage model
        # that could help is a model that flatters.
        net_points = float(trade["net"]) - slippage
        side = "long" if trade["dir"] > 0 else "short"
        entry_price = bars[entry_index].open

        quantity = config.quantity
        sizing_detail: dict[str, object] | None = None

        if risk_sized:
            assert spec is not None  # guarded above
            # The ATR the simulator used for this trade's bracket: index i-1,
            # the bar before the entry. Reading `atr[entry_index]` instead
            # would be look-ahead, and it is the one line where that mistake
            # is easy to make.
            bar_atr = float(atr[entry_index - 1]) if entry_index >= 1 else float("nan")
            if not math.isfinite(bar_atr) or bar_atr <= 0:
                refusals.append(
                    {
                        "entry_time": bars[entry_index].bar_time.isoformat(),
                        "side": side,
                        "reason": "no usable ATR on the bar before the entry",
                    }
                )
                continue
            stop_distance = Decimal(str(config.stop_atr)) * Decimal(str(bar_atr))
            result = size_order(
                SizingRequest(
                    method=method,
                    spec=spec,
                    risk_amount=config.risk_amount,
                    equity=balance,
                    risk_percent=config.risk_percent,
                    stop_distance=stop_distance,
                )
            )
            if result.refused or result.volume is None:
                refusals.append(
                    {
                        "entry_time": bars[entry_index].bar_time.isoformat(),
                        "side": side,
                        "equity": str(balance),
                        "stop_distance": str(stop_distance),
                        "reason": (result.gap or "sizing refused")[:300],
                    }
                )
                continue
            quantity = result.volume
            sizing_detail = {
                "mode": str(result.method),
                "equity_before": str(balance),
                "stop_distance": str(stop_distance),
                "risk_requested": str(result.risk_requested),
                "risk_actual": str(result.risk_actual),
                "raw_quantity": str(result.raw_volume),
            }

        money = round(net_points * float(quantity) - commission, 6)
        # The SAME figure the equity curve accumulates, rounded once here.
        # Walking sizing on the unrounded value and the curve on the rounded
        # one gives two balances that drift apart, and the sizing input would
        # then not be the equity the report shows.
        balance += Decimal(str(money))

        out.append(
            {
                "side": side,
                "entry_time": bars[entry_index].bar_time.isoformat(),
                "exit_time": bars[exit_index].bar_time.isoformat(),
                "entry_price": str(entry_price),
                "exit_price": str(bars[exit_index].close),
                "bars_held": int(trade["bars"]),
                "gross_points": round(float(trade["gross"]), 6),
                "net_points": round(net_points, 6),
                "financing": round(float(trade.get("financing", 0.0)), 6),
                "nights": int(trade.get("nights", 0)),
                "slippage_points": slippage,
                "commission": commission,
                "exit_reason": trade["reason"],
                # The size actually traded. Constant under fixed_quantity and
                # varying under the risk modes, which is the whole point of
                # them: a wider stop buys fewer units for the same money.
                "quantity": str(quantity),
                "sizing": sizing_detail,
                # Money, at the size above. Points is the poolable figure;
                # currency depends on the size, which is why both are kept
                # rather than one derived silently from the other.
                "net_money": money,
            }
        )
    return out, refusals


def _equity_curve(trades: list[dict], config: BacktestConfig) -> list[dict]:
    balance = float(config.initial_capital)
    peak = balance
    curve = [
        {
            "at": None,
            "balance": round(balance, 4),
            "equity": round(balance, 4),
            "drawdown": 0.0,
            "drawdown_pct": 0.0,
        }
    ]
    for trade in trades:
        balance += float(trade["net_money"])
        peak = max(peak, balance)
        drawdown = peak - balance
        curve.append(
            {
                "at": trade["exit_time"],
                "balance": round(balance, 4),
                "equity": round(balance, 4),
                "drawdown": round(drawdown, 4),
                "drawdown_pct": round(drawdown / peak * 100, 4) if peak > 0 else 0.0,
            }
        )
    return curve


# A ratio computed from a handful of trades is noise wearing a decimal point.
# Below this the metric is reported as NOT_AVAILABLE with the count, which is
# the honest answer and the one `track_record.py` already gives.
#: Imported rather than redeclared at L32. The backtest and the live journal
#: must answer "is this ratio worth reading?" the same way, and two constants
#: with the same name in two modules is how they stop doing that.
MIN_TRADES_FOR_RATIO = _metrics.MIN_TRADES_FOR_RATIO
NOT_AVAILABLE = "NOT_AVAILABLE"


def compute_metrics(
    trades: list[dict], curve: list[dict], config: BacktestConfig
) -> dict[str, object]:
    """Performance metrics, with insufficient data reported rather than filled.

    Sharpe and Sortino are **per trade**, not annualised. Annualising requires
    a trades-per-year figure that a fixed historical window does not supply,
    and inventing one is how a Sharpe of 0.3 becomes a Sharpe of 2.
    """
    if not trades:
        return {
            "trades": 0,
            "note": (
                "no trades were generated. That is a result about the rule over this "
                "window, not a failure"
            ),
        }

    # L32. The arithmetic comes from `app.analytics.metrics`, which is now the
    # one implementation of these definitions -- section 2 of the L32 brief asks
    # that a correct existing calculation be reused rather than duplicated, and
    # the audit found three copies of win rate, profit factor and drawdown that
    # agreed only because nobody had changed one yet.
    #
    # The OUTPUT SHAPE below is unchanged, including the `NOT_AVAILABLE` token:
    # the backtest API has served it since L14 and a renamed sentinel would be a
    # breaking change for a cosmetic gain.
    net = [float(t["net_points"]) for t in trades]
    money = [float(t["net_money"]) for t in trades]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    gross_profit = _metrics.gross_profit(net)
    gross_loss = _metrics.gross_loss(net)
    count = len(net)

    drawdowns = [float(p["drawdown"]) for p in curve]
    max_drawdown = max(drawdowns) if drawdowns else 0.0
    max_drawdown_pct = max((float(p["drawdown_pct"]) for p in curve), default=0.0)
    final = float(curve[-1]["balance"])
    initial = float(config.initial_capital)

    enough = count >= MIN_TRADES_FOR_RATIO
    sharpe_or_flag = _metrics.sharpe_per_trade(net)
    sortino_or_flag = _metrics.sortino_per_trade(net)
    sharpe = sharpe_or_flag if isinstance(sharpe_or_flag, float) else None
    sortino = sortino_or_flag if isinstance(sortino_or_flag, float) else None
    t_or_flag = _metrics.t_statistic(net)
    t_stat = t_or_flag if isinstance(t_or_flag, float) else 0.0

    return {
        "trades": count,
        "longs": sum(1 for t in trades if t["side"] == "long"),
        "shorts": sum(1 for t in trades if t["side"] == "short"),
        "win_rate": round(float(_metrics.win_rate(net)), 4),
        "wins": len(wins),
        "losses": len(losses),
        "net_points": round(sum(net), 4),
        "net_money": round(sum(money), 4),
        "gross_profit_points": round(gross_profit, 4),
        "gross_loss_points": round(gross_loss, 4),
        "profit_factor": (
            round(float(_metrics.profit_factor(net)), 4)
            if isinstance(_metrics.profit_factor(net), float)
            else None
        ),
        "expectancy_points": round(float(_metrics.expectancy(net)), 6),
        "average_win_points": round(sum(wins) / len(wins), 4) if wins else None,
        "average_loss_points": round(sum(losses) / len(losses), 4) if losses else None,
        "best_trade_points": round(max(net), 4),
        "worst_trade_points": round(min(net), 4),
        "average_bars_held": round(sum(t["bars_held"] for t in trades) / count, 2),
        "total_return_pct": round((final - initial) / initial * 100, 4) if initial else None,
        "max_drawdown_money": round(max_drawdown, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        # Reported as NOT_AVAILABLE rather than as a number computed from too
        # few trades. A ratio from 6 trades is noise wearing a decimal point.
        "sharpe_per_trade": round(sharpe, 4) if sharpe is not None else NOT_AVAILABLE,
        "sortino_per_trade": round(sortino, 4) if sortino is not None else NOT_AVAILABLE,
        "ratio_note": (
            None
            if enough
            else f"{count} trades is below the {MIN_TRADES_FOR_RATIO} needed for a "
            "ratio to mean anything"
        ),
        "t_stat": round(t_stat, 4),
        "significant_at_95": bool(abs(t_stat) > 1.96),
        "exit_mix": {
            reason: sum(1 for t in trades if t["exit_reason"] == reason)
            for reason in ("tp", "sl", "timeout")
        },
        "significance_note": (
            "A single t-statistic on one window is the weakest of this project's "
            "three gates. Era blocks, walk-forward and a permutation null are in "
            "tools/rule_search.py and none of this replaces them."
        ),
    }


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 8)))
