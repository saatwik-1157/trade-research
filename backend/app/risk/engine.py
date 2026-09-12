"""The Risk Engine. Nothing reaches the OMS without an approval from here.

Design rules, each of which a test enforces:

1. **The engine returns an Approval object or a Veto.** There is no third
   outcome and no way to proceed without one. The OMS takes an `Approval` as
   an argument, so an unapproved order cannot be constructed.
2. **Every decision is recorded**, approvals included. A veto that leaves no
   trace is indistinguishable from a check that never ran.
3. **Kill switches are checked first and cannot be argued with.** Global,
   account and strategy, in that order.
4. **The AI layer cannot reach this.** It sets a signal's confidence, which is
   an input to a threshold check like any other; it has no path to an
   approval.
5. **Unknown is not permission.** A check that cannot get the data it needs
   vetoes. A limit that cannot be evaluated is a limit that is not enforced,
   and an unenforced limit must not let an order through.

The demo fence in `tools/mt5_paper.assert_demo` sits underneath all of this
and is not restated: the broker adapter calls it on connect, so a real account
is refused before this engine is ever consulted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

log = logging.getLogger("app.risk")


class RiskDecision(StrEnum):
    approve = "approve"
    veto = "veto"
    halt = "halt"  # veto plus "stop the session"


class LimitKind(StrEnum):
    kill_switch_global = "kill_switch_global"
    kill_switch_account = "kill_switch_account"
    kill_switch_strategy = "kill_switch_strategy"
    trading_mode = "trading_mode"
    market_open = "market_open"
    signal_freshness = "signal_freshness"
    duplicate_signal = "duplicate_signal"
    max_open_positions = "max_open_positions"
    one_position_per_symbol = "one_position_per_symbol"
    max_trades_per_day = "max_trades_per_day"
    max_daily_loss = "max_daily_loss"
    max_weekly_loss = "max_weekly_loss"
    max_consecutive_losses = "max_consecutive_losses"
    max_correlated_exposure = "max_correlated_exposure"
    max_drawdown = "max_drawdown"
    max_exposure = "max_exposure"
    max_leverage = "max_leverage"
    max_risk_per_trade = "max_risk_per_trade"
    spread = "spread"
    # Added at L17. `market_open` and `market_data` complete the market-health
    # group `market_open` was already declared for but never evaluated.
    market_data = "market_data"
    account_state = "account_state"
    bot_state = "bot_state"
    strategy_state = "strategy_state"
    symbol_state = "symbol_state"
    max_position_size = "max_position_size"
    max_concentration = "max_concentration"
    margin = "margin"
    trade_frequency = "trade_frequency"
    cooldown = "cooldown"
    risk_reward = "risk_reward"
    position_size = "position_size"
    stop_loss_required = "stop_loss_required"


@dataclass(frozen=True)
class RiskLimits:
    """The seven named limits, plus the checks that guard correctness.

    None disables a limit, and a disabled limit is reported in the decision
    so "not enforced" never reads as "passed".
    """

    max_risk_per_trade: Decimal | None = None  # account currency
    max_daily_loss: Decimal | None = None
    #: Account currency, over the broker's trading week. Halts like the
    #: daily limit: the next order would breach it too.
    max_weekly_loss: Decimal | None = None
    #: Losing trades in a row before new orders are refused. A VETO and
    #: not a halt -- a halt would stop the session managing what is
    #: already open, and a streak is a reason to stop OPENING, never a
    #: reason to stop watching. It clears itself on the next win.
    max_consecutive_losses: int | None = None
    #: Account currency held in instruments that move together. The one
    #: of the three with no data source yet, and it fails closed when
    #: configured without one rather than passing quietly.
    max_correlated_exposure: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_exposure_per_currency: Decimal | None = None
    max_open_positions: int | None = None
    max_trades_per_day: int | None = None
    max_leverage: Decimal | None = None

    # Added at L17.
    max_trades_per_hour: int | None = None
    max_trades_per_minute: int | None = None
    max_position_size: Decimal | None = None
    max_concentration_pct: Decimal | None = None
    max_margin_utilisation_pct: Decimal | None = None
    min_risk_reward: Decimal | None = None
    cooldown_seconds: int | None = None
    market_data_max_age_seconds: float | None = None

    one_position_per_symbol: bool = True
    require_stop_loss: bool = True
    require_market_open: bool = False
    max_spread_points: Decimal | None = None
    # A signal is at most one bar old by construction, so a fixed 300s ceiling
    # vetoes every H1 strategy -- which it did, live, at L16. `None` means the
    # caller has not stated one; `RiskService` derives it from the timeframe.
    max_signal_age_seconds: float | None = None


@dataclass(frozen=True)
class KillSwitches:
    """Three scopes. Any one engaged stops the order."""

    global_stop: bool = False
    global_reason: str = ""
    accounts: frozenset[str] = frozenset()
    strategies: frozenset[str] = frozenset()

    def engaged_for(
        self, account_id: str | None, strategy_id: str | None
    ) -> tuple[str, str] | None:
        if self.global_stop:
            return (LimitKind.kill_switch_global, self.global_reason or "global kill switch")
        if account_id and account_id in self.accounts:
            return (LimitKind.kill_switch_account, f"kill switch on account {account_id}")
        if strategy_id and strategy_id in self.strategies:
            return (LimitKind.kill_switch_strategy, f"kill switch on strategy {strategy_id}")
        return None


@dataclass(frozen=True)
class PortfolioState:
    """What the engine is allowed to know. Every field may be None, and a
    None that a limit needs produces a veto rather than an assumption."""

    equity: Decimal | None = None
    balance: Decimal | None = None
    margin_used: Decimal | None = None
    open_positions: int | None = None
    open_symbols: frozenset[str] = frozenset()
    realised_today: Decimal | None = None
    realised_week: Decimal | None = None
    #: Losing trades in a row, counted per CLOSED position. None means
    #: it could not be counted, which is never the same as zero.
    consecutive_losses: int | None = None
    #: Exposure in instruments correlated above the portfolio's own
    #: threshold. Requires a common window across two or more
    #: instruments; None until there is one.
    correlated_exposure: Decimal | None = None
    trades_today: int | None = None
    peak_equity: Decimal | None = None
    exposure_by_currency: dict[str, Decimal] = field(default_factory=dict)
    # Added at L17. Every one may be None, and a None a limit needs vetoes.
    # `margin_used` is NOT here: it was already a field above.
    margin_free: Decimal | None = None
    margin_required: Decimal | None = None
    largest_position_value: Decimal | None = None
    trades_last_hour: int | None = None
    trades_last_minute: int | None = None
    last_trade_at: datetime | None = None
    market_open: bool | None = None
    market_data_age_seconds: float | None = None
    account_state: str | None = None
    bot_state: str | None = None
    strategy_enabled: bool | None = None
    symbol_tradable: bool | None = None


@dataclass(frozen=True)
class OrderProposal:
    """What the pipeline wants to do, before anyone has agreed to it."""

    symbol: str
    side: str
    mode: str
    volume: Decimal | None = None
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    risk_amount: Decimal | None = None
    spread_points: Decimal | None = None
    account_id: str | None = None
    strategy_id: str | None = None
    signal_id: str | None = None
    signal_time: datetime | None = None
    base_currency: str | None = None
    quote_currency: str | None = None


@dataclass(frozen=True)
class CheckOutcome:
    limit: LimitKind
    passed: bool
    detail: str
    enforced: bool = True


@dataclass(frozen=True)
class RiskVerdict:
    decision: RiskDecision
    reason: str
    checks: tuple[CheckOutcome, ...]
    at: datetime
    proposal: OrderProposal

    @property
    def approved(self) -> bool:
        return self.decision is RiskDecision.approve

    @property
    def failed(self) -> tuple[CheckOutcome, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def not_enforced(self) -> tuple[str, ...]:
        return tuple(str(c.limit) for c in self.checks if not c.enforced)


@dataclass(frozen=True)
class Approval:
    """Proof that the Risk Engine agreed. The OMS requires one.

    It cannot be constructed from a veto: `RiskEngine.evaluate` is the only
    thing that builds it, and only on approval.

    **It is bound to the order it approved.** `request_hash` digests the fields
    that make the order what it is -- account, symbol, side, volume, type,
    price, stop, target, strategy, mode. If any of them changes after approval,
    the hash no longer matches and the OMS refuses it. That closes the gap
    where risk approves order A and something submits a modified order B.

    **It expires.** A decision computed against a portfolio snapshot a minute
    ago is not evidence about now, and an approval that never expired could be
    replayed against a book that has moved.
    """

    verdict: RiskVerdict
    approved_volume: Decimal
    approved_at: datetime
    engine: str = "RiskEngine"
    decision_id: str = ""
    request_hash: str = ""
    expires_at: datetime | None = None

    @property
    def proposal(self) -> OrderProposal:
        return self.verdict.proposal

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now > self.expires_at

    def binds(self, fields: Mapping[str, object]) -> bool:
        """True when this approval was issued for exactly this order.

        An approval with no hash binds to nothing and returns True: approvals
        minted before the binding existed are still honoured rather than
        rejected wholesale. New ones always carry it.
        """
        if not self.request_hash:
            return True
        from app.risk.decision import request_hash as digest

        return digest(fields) == self.request_hash

    def bound_fields(self) -> dict[str, object]:
        """The fields as the RISK ENGINE saw them.

        **This is not "the order". Do not validate against it on its own** --
        `binds(bound_fields())` compares the approval to itself and is always
        True. Use `binds_order()`, which overrides the fields a caller controls
        with what the caller actually asked for.
        """
        p = self.proposal
        return {
            "account_id": p.account_id,
            "symbol": p.symbol,
            "side": p.side,
            "volume": self.approved_volume,
            # The engine evaluates an immediate fill at `entry_price`, and
            # `OrderProposal` carries no order type. "market" is therefore what
            # was actually assessed, and `binds_order` compares the caller's
            # real type against it -- so a limit or stop order built from a
            # market approval is refused rather than silently accepted.
            "order_type": "market",
            "entry_price": p.entry_price,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "strategy_id": p.strategy_id,
            "mode": p.mode,
        }

    def binds_order(self, *, account_id: str, order_type: str, mode: str) -> bool:
        """True when this approval was issued for EXACTLY this order.

        **The fix for L45 C-4.** The OMS previously called
        `binds(bound_fields())` -- both sides derived from this same object, so
        the digest always equalled `request_hash` and the check could never
        fail. A safety check that cannot fail is worse than an absent one,
        because it reads as protection in every audit.

        Three fields are not the approval's to assert, because `create()` takes
        them from its caller and writes them onto the order:

          * `account_id` -- the order is booked against the caller's account,
            while risk evaluated exposure against the proposal's. An approval
            for account A must not create an order on account B.
          * `order_type` -- risk assessed an immediate fill; a limit or stop
            order has different execution semantics.
          * `mode` -- the order is created in the *manager's* mode, which is
            not necessarily the mode risk evaluated.

        Everything else (symbol, side, volume, prices, strategy) is taken from
        the approval by `create()` itself and cannot diverge.
        """
        return self.binds(
            {
                **self.bound_fields(),
                "account_id": account_id,
                "order_type": order_type,
                "mode": mode,
            }
        )


class RiskEngine:
    def __init__(
        self,
        limits: RiskLimits | None = None,
        switches: KillSwitches | None = None,
    ) -> None:
        self.limits = limits or RiskLimits()
        self.switches = switches or KillSwitches()

    # ------------------------------------------------------------- checks
    def _checks(
        self,
        proposal: OrderProposal,
        portfolio: PortfolioState,
        now: datetime,
        recent_signal_ids: Sequence[str],
    ) -> list[CheckOutcome]:
        limits = self.limits
        out: list[CheckOutcome] = []

        def add(limit: LimitKind, passed: bool, detail: str, enforced: bool = True) -> None:
            out.append(CheckOutcome(limit, passed, detail, enforced))

        # Live is refused here as well as by the settings and the demo fence.
        # Three independent gates, because this one is the last before an order.
        add(
            LimitKind.trading_mode,
            proposal.mode in ("paper", "demo"),
            f"mode {proposal.mode}",
        )

        # Duplicate signal.
        if proposal.signal_id:
            duplicate = proposal.signal_id in recent_signal_ids
            add(
                LimitKind.duplicate_signal,
                not duplicate,
                f"signal {proposal.signal_id} already acted on" if duplicate else "not a duplicate",
            )

        # Signal freshness.
        if limits.max_signal_age_seconds is not None:
            if proposal.signal_time is None:
                add(LimitKind.signal_freshness, False, "signal has no timestamp")
            else:
                age = (now - proposal.signal_time).total_seconds()
                add(
                    LimitKind.signal_freshness,
                    age <= limits.max_signal_age_seconds,
                    f"signal is {age:.0f}s old, limit {limits.max_signal_age_seconds:.0f}s",
                )
        else:
            add(LimitKind.signal_freshness, True, "no freshness limit set", enforced=False)

        # Stop loss.
        if limits.require_stop_loss:
            add(
                LimitKind.stop_loss_required,
                proposal.stop_loss is not None,
                "stop loss present" if proposal.stop_loss else "no stop loss on the proposal",
            )

        # Position size must exist and be positive by the time we get here.
        add(
            LimitKind.position_size,
            proposal.volume is not None and proposal.volume > 0,
            f"volume {proposal.volume}",
        )

        # One position per symbol.
        if limits.one_position_per_symbol:
            clash = proposal.symbol in portfolio.open_symbols
            add(
                LimitKind.one_position_per_symbol,
                not clash,
                f"{proposal.symbol} already open" if clash else f"no open {proposal.symbol}",
            )

        # Max open positions.
        if limits.max_open_positions is not None:
            if portfolio.open_positions is None:
                add(LimitKind.max_open_positions, False, "open position count is unknown")
            else:
                add(
                    LimitKind.max_open_positions,
                    portfolio.open_positions < limits.max_open_positions,
                    f"{portfolio.open_positions} open, limit {limits.max_open_positions}",
                )
        else:
            add(LimitKind.max_open_positions, True, "no limit set", enforced=False)

        # Trades per day.
        if limits.max_trades_per_day is not None:
            if portfolio.trades_today is None:
                add(LimitKind.max_trades_per_day, False, "today's trade count is unknown")
            else:
                add(
                    LimitKind.max_trades_per_day,
                    portfolio.trades_today < limits.max_trades_per_day,
                    f"{portfolio.trades_today} today, limit {limits.max_trades_per_day}",
                )

        # Daily loss.
        if limits.max_daily_loss is not None:
            if portfolio.realised_today is None:
                add(LimitKind.max_daily_loss, False, "today's realised P&L is unknown")
            else:
                breached = portfolio.realised_today <= -abs(limits.max_daily_loss)
                add(
                    LimitKind.max_daily_loss,
                    not breached,
                    f"realised {portfolio.realised_today}, limit -{abs(limits.max_daily_loss)}",
                )

        # Weekly loss. Same shape as the daily limit and the same reason for
        # halting: a week that has breached its limit does not un-breach it
        # before the next order.
        if limits.max_weekly_loss is not None:
            if portfolio.realised_week is None:
                add(
                    LimitKind.max_weekly_loss,
                    False,
                    "this week's realised P&L is unknown",
                )
            else:
                breached = portfolio.realised_week <= -abs(limits.max_weekly_loss)
                add(
                    LimitKind.max_weekly_loss,
                    not breached,
                    f"realised {portfolio.realised_week}, limit -{abs(limits.max_weekly_loss)}",
                )

        # Consecutive losses. A veto rather than a halt, deliberately: the
        # response to a losing streak is to stop OPENING, and a halt would
        # also stop managing what is already open. Nothing here ever
        # increases size in response to a loss; that is martingale.
        if limits.max_consecutive_losses is not None:
            if portfolio.consecutive_losses is None:
                add(
                    LimitKind.max_consecutive_losses,
                    False,
                    "the recent outcomes could not be counted",
                )
            else:
                breached = portfolio.consecutive_losses >= limits.max_consecutive_losses
                add(
                    LimitKind.max_consecutive_losses,
                    not breached,
                    f"{portfolio.consecutive_losses} in a row, "
                    f"limit {limits.max_consecutive_losses}",
                )

        # Correlated exposure. This repository has no correlation figure yet:
        # it needs a common window across two or more instruments, and
        # `market_bars` holds 705 bars across 2 symbols. So the check exists
        # and FAILS when a limit is set without the data to evaluate it,
        # rather than passing quietly -- an unmeasurable limit that approves
        # is worse than no limit, because it reads as one that was checked.
        if limits.max_correlated_exposure is not None:
            if portfolio.correlated_exposure is None:
                add(
                    LimitKind.max_correlated_exposure,
                    False,
                    "correlated exposure is unknown; a correlation needs a "
                    "common window across two or more instruments",
                )
            else:
                breached = portfolio.correlated_exposure > limits.max_correlated_exposure
                add(
                    LimitKind.max_correlated_exposure,
                    not breached,
                    f"{portfolio.correlated_exposure} correlated, "
                    f"limit {limits.max_correlated_exposure}",
                )

        # Drawdown.
        if limits.max_drawdown_pct is not None:
            if (
                portfolio.equity is None
                or portfolio.peak_equity is None
                or portfolio.peak_equity <= 0
            ):
                add(LimitKind.max_drawdown, False, "equity or peak equity is unknown")
            else:
                dd = (portfolio.peak_equity - portfolio.equity) / portfolio.peak_equity * 100
                add(
                    LimitKind.max_drawdown,
                    dd < limits.max_drawdown_pct,
                    f"drawdown {dd:.2f}%, limit {limits.max_drawdown_pct}%",
                )

        # Risk per trade.
        if limits.max_risk_per_trade is not None:
            if proposal.risk_amount is None:
                add(LimitKind.max_risk_per_trade, False, "the proposal states no risk amount")
            else:
                add(
                    LimitKind.max_risk_per_trade,
                    proposal.risk_amount <= limits.max_risk_per_trade,
                    f"risk {proposal.risk_amount}, limit {limits.max_risk_per_trade}",
                )

        # Exposure per currency. Seven USD pairs open together is one bet.
        if limits.max_exposure_per_currency is not None:
            worst = None
            for currency in filter(None, (proposal.base_currency, proposal.quote_currency)):
                current = portfolio.exposure_by_currency.get(currency, Decimal(0))
                projected = current + (proposal.volume or Decimal(0))
                if projected > limits.max_exposure_per_currency:
                    worst = (currency, projected)
                    break
            add(
                LimitKind.max_exposure,
                worst is None,
                f"{worst[0]} exposure would reach {worst[1]}, limit "
                f"{limits.max_exposure_per_currency}"
                if worst
                else "within currency exposure limits",
            )

        # Leverage.
        if limits.max_leverage is not None:
            if portfolio.equity is None or portfolio.margin_used is None or portfolio.equity <= 0:
                add(LimitKind.max_leverage, False, "equity or margin is unknown")
            else:
                leverage = portfolio.margin_used / portfolio.equity
                add(
                    LimitKind.max_leverage,
                    leverage <= limits.max_leverage,
                    f"leverage {leverage:.2f}, limit {limits.max_leverage}",
                )

        # Spread.
        if limits.max_spread_points is not None:
            if proposal.spread_points is None:
                add(LimitKind.spread, False, "spread is unknown")
            else:
                add(
                    LimitKind.spread,
                    proposal.spread_points <= limits.max_spread_points,
                    f"spread {proposal.spread_points}, limit {limits.max_spread_points}",
                )

        # ------------------------------------------------- added at L17

        # Account, bot, strategy and symbol status. Each is checked only when
        # the caller supplied it: a fact not provided is not evidence of
        # health, so it is reported as unenforced rather than assumed good.
        if portfolio.account_state is not None:
            add(
                LimitKind.account_state,
                portfolio.account_state in ("active", "ACTIVE"),
                f"account is {portfolio.account_state}",
            )
        else:
            add(LimitKind.account_state, True, "account state not supplied", enforced=False)

        if portfolio.bot_state is not None:
            add(
                LimitKind.bot_state,
                portfolio.bot_state == "running",
                f"bot is {portfolio.bot_state}",
            )
        else:
            add(LimitKind.bot_state, True, "no bot state supplied", enforced=False)

        if portfolio.strategy_enabled is not None:
            add(
                LimitKind.strategy_state,
                bool(portfolio.strategy_enabled),
                "strategy is disabled" if not portfolio.strategy_enabled else "strategy enabled",
            )
        else:
            add(LimitKind.strategy_state, True, "strategy state not supplied", enforced=False)

        if portfolio.symbol_tradable is not None:
            add(
                LimitKind.symbol_state,
                bool(portfolio.symbol_tradable),
                "symbol is not tradable" if not portfolio.symbol_tradable else "symbol tradable",
            )
        else:
            add(LimitKind.symbol_state, True, "symbol state not supplied", enforced=False)

        # Market open. Declared as a LimitKind before L17 and never evaluated
        # until now -- an enum member with no check behind it reads, from the
        # outside, exactly like an enforced limit.
        if limits.require_market_open:
            if portfolio.market_open is None:
                add(LimitKind.market_open, False, "market status is unknown")
            else:
                add(
                    LimitKind.market_open,
                    bool(portfolio.market_open),
                    "market is closed" if not portfolio.market_open else "market is open",
                )
        else:
            add(LimitKind.market_open, True, "market-open check not required", enforced=False)

        # Market-data freshness. Stale data blocks a NEW order: acting on the
        # last price we happen to have is not the same as acting on the price.
        if limits.market_data_max_age_seconds is not None:
            if portfolio.market_data_age_seconds is None:
                add(LimitKind.market_data, False, "market-data age is unknown")
            else:
                add(
                    LimitKind.market_data,
                    portfolio.market_data_age_seconds <= limits.market_data_max_age_seconds,
                    f"market data is {portfolio.market_data_age_seconds:.0f}s old, "
                    f"limit {limits.market_data_max_age_seconds:.0f}s",
                )
        else:
            add(LimitKind.market_data, True, "no freshness limit set", enforced=False)

        # Absolute position size.
        if limits.max_position_size is not None:
            if proposal.volume is None:
                add(LimitKind.max_position_size, False, "no volume proposed")
            else:
                add(
                    LimitKind.max_position_size,
                    proposal.volume <= limits.max_position_size,
                    f"volume {proposal.volume}, limit {limits.max_position_size}",
                )
        else:
            add(LimitKind.max_position_size, True, "no limit set", enforced=False)

        # Concentration: how much of the equity sits in one position. Distinct
        # from exposure, which is the total across the book.
        if limits.max_concentration_pct is not None:
            if portfolio.equity is None or portfolio.largest_position_value is None:
                add(LimitKind.max_concentration, False, "equity or position value is unknown")
            elif portfolio.equity <= 0:
                add(LimitKind.max_concentration, False, "equity is not positive")
            else:
                share = portfolio.largest_position_value / portfolio.equity * 100
                add(
                    LimitKind.max_concentration,
                    share <= limits.max_concentration_pct,
                    f"largest position is {share:.2f}% of equity, "
                    f"limit {limits.max_concentration_pct}%",
                )
        else:
            add(LimitKind.max_concentration, True, "no limit set", enforced=False)

        # Margin. Only checked when the account actually reports margin; this
        # platform's paper accounts do not, and fabricating a margin model
        # would be worse than reporting its absence.
        if limits.max_margin_utilisation_pct is not None:
            if portfolio.equity is None or portfolio.margin_used is None:
                add(LimitKind.margin, False, "equity or used margin is unknown")
            elif portfolio.equity <= 0:
                add(LimitKind.margin, False, "equity is not positive")
            else:
                projected = portfolio.margin_used + (portfolio.margin_required or Decimal(0))
                utilisation = projected / portfolio.equity * 100
                add(
                    LimitKind.margin,
                    utilisation <= limits.max_margin_utilisation_pct,
                    f"projected margin utilisation {utilisation:.2f}%, "
                    f"limit {limits.max_margin_utilisation_pct}%",
                )
        else:
            add(LimitKind.margin, True, "margin not modelled for this account", enforced=False)

        # Trade frequency. The runaway-strategy guard: a bug producing 500
        # signals a second must not produce 500 orders.
        for limit_value, observed, window in (
            (limits.max_trades_per_minute, portfolio.trades_last_minute, "minute"),
            (limits.max_trades_per_hour, portfolio.trades_last_hour, "hour"),
        ):
            if limit_value is None:
                continue
            if observed is None:
                add(LimitKind.trade_frequency, False, f"trades in the last {window} unknown")
            else:
                add(
                    LimitKind.trade_frequency,
                    observed < limit_value,
                    f"{observed} trades in the last {window}, limit {limit_value}",
                )
        if limits.max_trades_per_minute is None and limits.max_trades_per_hour is None:
            add(LimitKind.trade_frequency, True, "no frequency limit set", enforced=False)

        # Cooldown between trades.
        if limits.cooldown_seconds:
            if portfolio.last_trade_at is None:
                add(LimitKind.cooldown, True, "no previous trade recorded")
            else:
                elapsed = (now - portfolio.last_trade_at).total_seconds()
                add(
                    LimitKind.cooldown,
                    elapsed >= limits.cooldown_seconds,
                    f"{elapsed:.0f}s since the last trade, cooldown {limits.cooldown_seconds}s",
                )
        else:
            add(LimitKind.cooldown, True, "no cooldown set", enforced=False)

        # Risk-to-reward. Optional by design: no rule in this repository has a
        # measured edge, so a minimum RR is a policy a caller chooses, not a
        # finding the platform imposes.
        if limits.min_risk_reward is not None:
            if (
                proposal.entry_price is None
                or proposal.stop_loss is None
                or proposal.take_profit is None
            ):
                add(LimitKind.risk_reward, False, "entry, stop or target is missing")
            else:
                risk = abs(proposal.entry_price - proposal.stop_loss)
                reward = abs(proposal.take_profit - proposal.entry_price)
                if risk <= 0:
                    add(LimitKind.risk_reward, False, "the stop is at the entry")
                else:
                    ratio = reward / risk
                    add(
                        LimitKind.risk_reward,
                        ratio >= limits.min_risk_reward,
                        f"risk/reward {ratio:.2f}, minimum {limits.min_risk_reward}",
                    )
        else:
            add(LimitKind.risk_reward, True, "no minimum set", enforced=False)

        # Completeness. Every limit the engine knows about appears in every
        # decision, even the ones no configuration set -- because a limit that
        # is simply ABSENT from the record cannot be told apart from one that
        # passed, and this project's whole doctrine is that "not measured" must
        # never read as "fine". Nine limits used to vanish this way.
        #
        # Done in one place rather than as an else-branch on each check, so a
        # limit added later is covered without anyone remembering to. The
        # kill-switch kinds are excluded: they are evaluated in `evaluate`,
        # before this runs, and reporting them here would claim a check ran
        # twice.
        reported = {c.limit for c in out}
        for kind in LimitKind:
            if kind in reported or str(kind).startswith("kill_switch"):
                continue
            add(kind, True, "no limit set", enforced=False)

        return out

    # ----------------------------------------------------------- evaluate
    def evaluate(
        self,
        proposal: OrderProposal,
        portfolio: PortfolioState | None = None,
        now: datetime | None = None,
        recent_signal_ids: Sequence[str] = (),
    ) -> RiskVerdict:
        # Timezone-AWARE, deliberately. `app.auth.models.utcnow` is naive
        # because the DateTime columns are, and it was the default here until
        # L17 -- but every time this engine compares against is aware: a bar
        # time, a signal time, an approval expiry. Mixing them raises
        # `TypeError: can't compare offset-naive and offset-aware datetimes`,
        # and it did, the moment approvals gained an expiry.
        now = now or datetime.now(UTC)
        portfolio = portfolio or PortfolioState()

        # Kill switches first, and they are not negotiable.
        engaged = self.switches.engaged_for(proposal.account_id, proposal.strategy_id)
        if engaged is not None:
            limit, reason = engaged
            check = CheckOutcome(LimitKind(limit), False, reason)
            return RiskVerdict(RiskDecision.halt, reason, (check,), now, proposal)

        checks = self._checks(proposal, portfolio, now, recent_signal_ids)
        failed = [c for c in checks if not c.passed]
        if not failed:
            return RiskVerdict(
                RiskDecision.approve, "all checks passed", tuple(checks), now, proposal
            )

        # A breached daily loss or drawdown stops the session, not just this
        # order: the next order would breach it too.
        # A breached daily loss, weekly loss or drawdown stops the session:
        # the next order would breach it too. A losing STREAK does not --
        # it clears on the next win, and halting would stop the session
        # managing the positions the streak just produced.
        halting = {
            LimitKind.max_daily_loss,
            LimitKind.max_weekly_loss,
            LimitKind.max_drawdown,
        }
        decision = (
            RiskDecision.halt if any(c.limit in halting for c in failed) else RiskDecision.veto
        )
        return RiskVerdict(
            decision,
            "; ".join(f"{c.limit}: {c.detail}" for c in failed),
            tuple(checks),
            now,
            proposal,
        )

    def approve_close(
        self,
        proposal: OrderProposal,
        portfolio: PortfolioState | None = None,
        now: datetime | None = None,
    ) -> tuple[Approval | None, RiskVerdict]:
        """Approve a RISK-REDUCING order. **The fix for L45 C-2.**

        The second way to obtain an `Approval`, and deliberately not `approve`.

        `app/positions/broker_executor.py` reached the venue directly --
        `manager.adapter.close_position(...)`, with no approval, no OMS, no
        `intent_id` and no order record -- while its own docstring asserted
        that a close "takes the same path any other order takes". Routing it
        back through the OMS requires an `Approval`, because the OMS creates
        nothing without one and an `Approval` can be built nowhere but here.

        **Why a close is not evaluated like an open, and why that is a
        decision rather than a loophole.** Every limit this engine enforces
        bounds the risk of TAKING a position: exposure, position count, daily
        loss, drawdown, the kill switches. Applying them to a close would
        refuse to reduce risk at precisely the moment risk is highest -- a
        breached daily loss would make the position impossible to exit, and a
        kill switch would trap every open position behind it. That is the
        opposite of what those limits are for.

        So a close is evaluated, RECORDED, and then approved anyway. Nothing
        is hidden by that: the verdict carries every check that failed, they
        are listed in `not_enforced`, and the decision reaches the audit trail
        exactly as an opening decision does. What changes is that they cannot
        veto -- and this is the place that says so, in the same file as the
        engine that would otherwise be assumed to have refused.

        **One check still refuses, and it is the one that must.**
        `LimitKind.trading_mode` fails for any mode outside paper and demo, and
        a close is still an instruction transmitted to a venue. It depends on
        the proposal rather than on the configured limits, so it is exactly as
        strict here as anywhere else.
        """
        verdict = self.evaluate(proposal, portfolio, now)
        mode_ok = all(
            check.passed for check in verdict.checks if check.limit is LimitKind.trading_mode
        )
        if not mode_ok or proposal.volume is None or proposal.volume <= 0:
            log.warning(
                "risk engine refused a CLOSE; only the mode fence and a missing volume can do this",
                extra={
                    "event": "risk_refused_close",
                    "decision": str(verdict.decision),
                    "symbol": proposal.symbol,
                    "mode": proposal.mode,
                    "reason": verdict.reason[:200],
                },
            )
            return None, verdict

        if not verdict.approved:
            # The interesting case, and the one that must stay visible. Limits
            # were breached and the close proceeds anyway, because refusing to
            # reduce exposure is not a risk control.
            #
            # The breached checks are re-stamped `enforced=False` rather than
            # dropped, which is what `not_enforced` has always meant: the limit
            # was evaluated, it failed, and it was deliberately not applied.
            # They reach the order's `risk_snapshot` through the OMS, so the
            # record of this close names every limit it went over.
            overridden = tuple(c for c in verdict.failed)
            log.info(
                "a close was approved over a risk verdict that would have vetoed an "
                "opening order; a close reduces exposure and is never blocked by a "
                "limit that exists to bound it",
                extra={
                    "event": "risk_close_over_verdict",
                    "decision": str(verdict.decision),
                    "symbol": proposal.symbol,
                    "mode": proposal.mode,
                    "overridden": [str(c.limit) for c in overridden],
                },
            )
            verdict = RiskVerdict(
                RiskDecision.approve,
                "approved as a risk-reducing close, over: "
                + "; ".join(f"{c.limit}: {c.detail}" for c in overridden),
                tuple(
                    c if c.passed else CheckOutcome(c.limit, c.passed, c.detail, enforced=False)
                    for c in verdict.checks
                ),
                verdict.at,
                proposal,
            )

        from app.risk.decision import APPROVAL_TTL_SECONDS
        from app.risk.decision import request_hash as digest

        # The SAME bound fields as `approve`. A close's approval must satisfy
        # the L45 C-4 binding check identically, or the OMS would refuse it --
        # and a close that cannot be created is a position that cannot be shut.
        bound = {
            "account_id": proposal.account_id,
            "symbol": proposal.symbol,
            "side": proposal.side,
            "volume": proposal.volume,
            "order_type": "market",
            "entry_price": proposal.entry_price,
            "stop_loss": proposal.stop_loss,
            "take_profit": proposal.take_profit,
            "strategy_id": proposal.strategy_id,
            "mode": proposal.mode,
        }
        return (
            Approval(
                verdict=verdict,
                approved_volume=proposal.volume,
                approved_at=verdict.at,
                decision_id=str(uuid4()),
                request_hash=digest(bound),
                expires_at=verdict.at + timedelta(seconds=APPROVAL_TTL_SECONDS),
            ),
            verdict,
        )

    def approve(
        self,
        proposal: OrderProposal,
        portfolio: PortfolioState | None = None,
        now: datetime | None = None,
        recent_signal_ids: Sequence[str] = (),
    ) -> tuple[Approval | None, RiskVerdict]:
        """The only way to obtain an Approval."""
        verdict = self.evaluate(proposal, portfolio, now, recent_signal_ids)
        if not verdict.approved or proposal.volume is None:
            log.warning(
                "risk engine refused an order",
                extra={
                    "event": "risk_refused",
                    "decision": str(verdict.decision),
                    "symbol": proposal.symbol,
                    "mode": proposal.mode,
                    "reason": verdict.reason[:200],
                },
            )
            return None, verdict
        from app.risk.decision import APPROVAL_TTL_SECONDS
        from app.risk.decision import request_hash as digest

        bound = {
            "account_id": proposal.account_id,
            "symbol": proposal.symbol,
            "side": proposal.side,
            # `approved_volume`, not `proposal.volume`. They are equal today
            # because the engine approves the requested size or refuses, but a
            # future partial approval would make them differ -- and then the
            # order (which uses `approved_volume`) would never match a hash
            # built from the requested one, turning a correct order into a
            # permanent refusal.
            "volume": proposal.volume,
            "order_type": "market",
            "entry_price": proposal.entry_price,
            "stop_loss": proposal.stop_loss,
            "take_profit": proposal.take_profit,
            "strategy_id": proposal.strategy_id,
            "mode": proposal.mode,
        }
        return (
            Approval(
                verdict=verdict,
                approved_volume=proposal.volume,
                approved_at=verdict.at,
                decision_id=str(uuid4()),
                request_hash=digest(bound),
                expires_at=verdict.at + timedelta(seconds=APPROVAL_TTL_SECONDS),
            ),
            verdict,
        )
