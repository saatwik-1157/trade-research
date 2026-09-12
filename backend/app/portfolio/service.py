"""One portfolio view, assembled from the systems that own each piece.

Section 29 decides the shape of this module: **the Portfolio Engine aggregates,
it does not own.**

    live account state   -> the BROKER, through `app/brokers`
    paper account state  -> the PAPER ENGINE's own portfolio (L16)
    internal order state -> the OMS (L19)
    positions            -> `positions` (L21). No second representation (§9)
    completed trades     -> the trade journal (L19). No second history (§51)
    instrument metadata  -> `app/symbols` (L11). No invented contract sizes
    strategy/bot identity-> the strategy registry and Bot Manager (§17)

Nothing here recomputes what one of those owns, and nothing here decides
anything. Section 2: this says *what is held*; `RiskEngine` says *whether a new
action is allowed*, and `PositionSizing` says *how large*. Section 63 is the
strict version — the portfolio engine may never place, modify or bypass
anything — and it is structural: this module imports no order manager, no
broker adapter's write side, no sizer and no risk decision. A test parses it.

**Never fabricate.** Section 59. A broker that cannot be read produces an
`AccountState` with every figure `None` and a reason attached, not the last
values seen; a position whose notional cannot be computed is counted as
uncomputable rather than as zero; a portfolio with no reconciliation reports
that it has none rather than that it agrees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import BrokerAccount, PaperAccount
from app.models.execution import Position, Trade
from app.models.journal import PortfolioSnapshot
from app.models.market import Symbol
from app.portfolio import exposure as exposure_engine
from app.portfolio import pnl as pnl_engine
from app.portfolio import state as account_state
from app.portfolio.state import (
    AccountState,
    Freshness,
    PortfolioHealth,
    Reconciliation,
    Source,
)

log = logging.getLogger("app.portfolio")

ZERO = Decimal("0")

#: How old a broker reading may be before the view is STALE. Section 44.
#:
#: 60 seconds because that is the order of a polling interval, not because
#: anything measured it -- and it is a parameter rather than a constant so a
#: deployment polling every five minutes can say so instead of being permanently
#: stale.
DEFAULT_FRESHNESS = timedelta(seconds=60)

#: Section 24. Reported, never enforced -- the risk engine decides.
DEFAULT_MARGIN_WARNING = Decimal("0.50")


#: The `PortfolioState` fields the risk engine reads that this engine does NOT
#: own, and the system that does. Sections 2 and 29.
#:
#: Declared rather than omitted, for the reason L28's `DECLINED` table exists:
#: an absence looks the same whether it was reasoned about or forgotten, and a
#: test asserts that every field the risk engine reads is either supplied here
#: or named in this table. Adding a field to `PortfolioState` therefore forces
#: a decision instead of silently producing a `None` that L17 reads as a veto.
NOT_SUPPLIED: dict[str, str] = {
    "market_open": "market data (L08). Whether a venue is trading is not a portfolio fact.",
    "symbol_tradable": "the symbol registry (L11) and the broker adapter (L10).",
    "market_data_age_seconds": "market data (L08). This engine measures the age of the "
    "ACCOUNT reading, which is a different staleness.",
    "strategy_enabled": "the strategy registry (L12).",
    "bot_state": "the Bot Manager (L22).",
    "trades_last_minute": "the trade journal (L19). A rate limit counts orders, not holdings.",
    "trades_last_hour": "the trade journal (L19).",
    "last_trade_at": "the trade journal (L19).",
    "margin_required": "position sizing (L18) and the broker. It is what a PROPOSED "
    "trade would need; the portfolio holds what current positions already use.",
    "consecutive_losses": "the trade journal (L19). It is a property of a SEQUENCE of "
    "closed trades, not of what is held now -- `tools/risk_gate.py` counts it from "
    "closed outcomes, and a second count here would be a second answer.",
    "correlated_exposure": "market data (L08). Grouping exposure by correlation needs a "
    "common return window across two or more instruments; the portfolio knows what is "
    "held, not how those instruments move together.",
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class PortfolioView:
    """The read model §28 asks for. One account, one moment."""

    account: AccountState
    positions: list[exposure_engine.PositionView] = field(default_factory=list)
    exposure: exposure_engine.ExposureReport | None = None
    pnl: pnl_engine.PnL | None = None
    drawdown: pnl_engine.Drawdown | None = None
    margin: pnl_engine.MarginUtilisation | None = None
    open_risk: dict[str, Any] = field(default_factory=dict)
    reconciliation: Reconciliation = field(default_factory=lambda: account_state.not_checked(""))
    health: PortfolioHealth = PortfolioHealth.error
    health_reasons: list[str] = field(default_factory=list)
    order_count: int = 0
    at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "account": self.account.as_dict(),
            "health": str(self.health),
            "health_reasons": self.health_reasons,
            "freshness": str(self.account.freshness),
            "reconciliation": self.reconciliation.as_dict(),
            "pnl": self.pnl.as_dict() if self.pnl else None,
            "drawdown": self.drawdown.as_dict() if self.drawdown else None,
            "margin": self.margin.as_dict() if self.margin else None,
            "exposure": self.exposure.as_dict() if self.exposure else None,
            "open_risk": self.open_risk,
            "correlation": exposure_engine.correlation_note(),
            "position_count": len(self.positions),
            "order_count": self.order_count,
            "environment": self.account.environment,
            "authority": (
                "this reports state. The RISK ENGINE decides whether a new action is "
                "allowed, POSITION SIZING decides how large, and the OMS manages the "
                "order. Nothing here places, modifies or cancels anything."
            ),
            "sources": {
                "account": str(self.account.source),
                "positions": "positions (L21)",
                "realized_pnl": "trades, the journal (L19)",
                "instruments": "symbols and contract specs (L11)",
                "note": (
                    "the portfolio engine aggregates; it does not own. Each figure names "
                    "the system to go and ask."
                ),
            },
        }

    def to_risk_state(self) -> dict[str, Any]:
        """The fields `app.risk.PortfolioState` reads. Sections 25 and 47.

        Returned as a mapping rather than the dataclass so this module does not
        import the risk engine -- §63's boundary, and the direction that keeps
        risk able to read portfolio without portfolio being able to reach risk.

        **Every value may be None**, and L17 already treats a `None` a limit
        needs as a veto rather than an assumption. That is the correct
        behaviour and it is why a stale or unavailable portfolio makes trading
        MORE conservative rather than less.
        """
        gross = self.exposure.total.gross if self.exposure else None
        return {
            "equity": self.account.equity,
            "balance": self.account.balance,
            "margin_used": self.account.margin_used,
            "margin_free": self.account.margin_free,
            "open_positions": len(self.positions),
            "open_symbols": frozenset(p.symbol for p in self.positions),
            "realised_today": self.pnl.realized_today if self.pnl else None,
            "realised_week": self.pnl.realized_week if self.pnl else None,
            "trades_today": self.pnl.trades_today if self.pnl else None,
            "peak_equity": self.drawdown.peak_equity if self.drawdown else None,
            "exposure_by_currency": (
                dict(self.exposure.by_currency)
                if self.exposure and self.exposure.currency_available
                else {}
            ),
            "largest_position_value": _largest(self.exposure),
            "gross_exposure": gross,
            "account_state": None,
            "not_supplied": dict(NOT_SUPPLIED),
            "note": (
                "every field may be None, and L17 treats a None a limit needs as a VETO "
                "rather than an assumption -- so a stale or unreadable portfolio makes "
                "trading more conservative, never less."
            ),
        }


def _largest(report: exposure_engine.ExposureReport | None) -> Decimal | None:
    if report is None or not report.by_symbol:
        return None
    return max((b.gross for b in report.by_symbol.values()), default=None)


class PortfolioService:
    """Assembles a portfolio view. Owns nothing; decides nothing."""

    def __init__(
        self,
        *,
        freshness: timedelta = DEFAULT_FRESHNESS,
        margin_warning: Decimal = DEFAULT_MARGIN_WARNING,
    ) -> None:
        self.freshness = freshness
        self.margin_warning = margin_warning

    # ------------------------------------------------------------ positions

    async def positions_for(
        self,
        db: AsyncSession,
        *,
        account_id: str,
        environment: str,
        prices: dict[str, Decimal] | None = None,
    ) -> list[exposure_engine.PositionView]:
        """Open positions as the exposure engine reads them. Section 9.

        Built from the existing `Position` rows, joined to the symbol metadata
        that makes a notional computable. No second position representation.
        """
        # The account columns are `broker_account_id` and `paper_account_id`,
        # not one `account_id`. Kept apart at L05 for the reason §41 restates:
        # a paper account and a live one must never be pooled by omission, and
        # one nullable column would have made that a matter of remembering.
        column = Position.paper_account_id if environment == "paper" else Position.broker_account_id
        rows = list(
            (
                await db.scalars(
                    select(Position).where(
                        column == account_id,
                        Position.mode == environment,
                        Position.status.in_(("open", "partially_closed")),
                    )
                )
            ).all()
        )
        if not rows:
            return []

        # §17. No execution table carries a bot id -- the Bot Manager owns that
        # identity and links it to an ACCOUNT, so attribution runs that way
        # rather than through a second identifier this level would have had to
        # invent. One bot per paper account today; a broker account running
        # several would need the link L19 does not yet write, and the exposure
        # would show `unattributed` rather than guess.
        bot_id = await self._bot_for(db, account_id=account_id, environment=environment)

        symbols = {
            row.id: row
            for row in (
                await db.scalars(select(Symbol).where(Symbol.id.in_({r.symbol_id for r in rows})))
            ).all()
        }

        out: list[exposure_engine.PositionView] = []
        for row in rows:
            symbol = symbols.get(row.symbol_id)
            spec = await self._spec_for(db, symbol)
            out.append(
                exposure_engine.PositionView(
                    position_id=row.id,
                    symbol=symbol.code if symbol else row.symbol_id,
                    side="long" if row.side in ("buy", "long") else "short",
                    # `quantity` is what is OPEN now -- L21 keeps
                    # `initial_quantity` and `closed_quantity` separately, and
                    # exposure is about what is held, not what was opened.
                    quantity=row.quantity,
                    entry_price=row.entry_price,
                    # `current_price` is deliberately NOT a column. L21's own
                    # comment says why: unrealised P&L is a function of a price
                    # that changes every tick, and a stored one is wrong the
                    # moment it is written. So it is marked from a price map the
                    # caller supplies, or reported as unmarked.
                    current_price=(prices or {}).get(symbol.code if symbol else ""),
                    stop_loss=row.stop_loss,
                    take_profit=row.take_profit,
                    unrealized_pnl=None,
                    strategy_id=row.strategy_version_id,
                    bot_id=bot_id,
                    asset_class=symbol.asset_class if symbol else None,
                    base_currency=symbol.base_currency if symbol else None,
                    quote_currency=symbol.quote_currency if symbol else None,
                    contract_size=spec.get("contract_size"),
                    tick_size=spec.get("tick_size"),
                    tick_value=spec.get("tick_value"),
                )
            )
        return out

    async def _bot_for(self, db: AsyncSession, *, account_id: str, environment: str) -> str | None:
        """The bot that owns this account, if one does. Section 17.

        Through the Bot Manager's own identity -- `bots.paper_account_id` --
        rather than a second bot identifier on the execution tables. None is
        returned as None, and the exposure report shows it as `unattributed`:
        §59's rule applied to attribution, since a guessed owner is worse than
        an acknowledged gap.
        """
        if environment != "paper":
            return None
        from app.models.bots import Bot

        return await db.scalar(select(Bot.id).where(Bot.paper_account_id == account_id))

    async def _spec_for(self, db: AsyncSession, symbol: Symbol | None) -> dict[str, Any]:
        """The measured contract terms for one symbol, or an empty mapping.

        Empty rather than defaulted. §13 and the metals lesson: a notional built
        from an assumed contract size is a number in the wrong unit, and the
        exposure engine refuses one rather than pooling it.
        """
        if symbol is None:
            return {}
        from app.models.market import SymbolMapping

        mapping = await db.scalar(select(SymbolMapping).where(SymbolMapping.symbol_id == symbol.id))
        if mapping is None:
            return {}
        return {
            "contract_size": mapping.contract_size,
            "tick_size": mapping.tick_size,
            "tick_value": mapping.tick_value,
        }

    # ----------------------------------------------------------------- P&L

    async def pnl_for(
        self,
        db: AsyncSession,
        *,
        account_id: str,
        environment: str,
        positions: list[exposure_engine.PositionView],
        now: datetime,
    ) -> pnl_engine.PnL:
        """Realized from the journal, unrealized from the marks. Sections 20, 21."""
        from app.risk.state import day_start, week_start

        # `day_start` is L17's boundary and it works in AWARE UTC -- the risk
        # engine hands it `datetime.now(UTC)`. The database stores NAIVE UTC,
        # so the moment is made aware for the calculation and the answer made
        # naive again for the comparison.
        #
        # Doing neither is the bug `CLAUDE.md` records at length: a naive
        # datetime's `.astimezone(UTC)` is read as LOCAL time, so on this
        # UTC+5:30 machine the trading day would have started at 18:30 the
        # previous evening and the same code would have been correct on a UTC
        # machine. Converting here rather than defining a second boundary is
        # §21: one definition, used from both sides of the tz-awareness line.
        aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        boundary = day_start(aware).replace(tzinfo=None)

        # `trades` carries no account column -- it links to the POSITION, which
        # carries the account. Joined rather than filtered on `mode` alone,
        # because two paper accounts in one deployment would otherwise pool.
        column = Position.paper_account_id if environment == "paper" else Position.broker_account_id
        closed = list(
            (
                await db.scalars(
                    select(Trade)
                    .join(Position, Trade.position_id == Position.id)
                    .where(column == account_id, Trade.mode == environment)
                )
            ).all()
        )
        realized = sum((row.net_profit for row in closed), ZERO)
        today = [row for row in closed if row.closed_at and row.closed_at >= boundary]
        realized_today = sum((row.net_profit for row in today), ZERO)
        # The weekly veto reads this. Same closed set, same tz-naive frame,
        # one week boundary defined beside the daily one rather than a second
        # rule invented here.
        week_boundary = week_start(aware).replace(tzinfo=None)
        this_week = [row for row in closed if row.closed_at and row.closed_at >= week_boundary]
        realized_week = sum((row.net_profit for row in this_week), ZERO)

        marks = [p.unrealized_pnl for p in positions]
        unrealized, missing = pnl_engine.unrealized_of(marks)
        unavailable = tuple(p.symbol for p in positions if p.unrealized_pnl is None)

        return pnl_engine.PnL(
            realized=realized,
            unrealized=unrealized,
            realized_today=realized_today,
            realized_week=realized_week,
            trades_today=len(today),
            day_start=boundary,
            week_start=week_boundary,
            realized_trades=len(closed),
            open_positions=len(positions),
            unrealized_unavailable=unavailable if missing else (),
        )

    # ------------------------------------------------------------ drawdown

    async def drawdown_for(
        self, db: AsyncSession, *, account_id: str, environment: str, equity: Decimal | None
    ) -> pnl_engine.Drawdown:
        """Peak from the recorded snapshots, never from a rolling window. §22.

        `portfolio_snapshots` has existed since L05 and is the equity history
        §23 asks for. Reading the peak from it rather than from the last N days
        is what stops the peak falling when the window rolls past an old high.
        """
        rows = list(
            (
                await db.scalars(
                    select(PortfolioSnapshot)
                    .where(
                        PortfolioSnapshot.mode == environment,
                        (PortfolioSnapshot.broker_account_id == account_id)
                        | (PortfolioSnapshot.paper_account_id == account_id),
                    )
                    .order_by(PortfolioSnapshot.taken_at)
                )
            ).all()
        )
        drawdown = pnl_engine.from_curve([(r.taken_at, r.equity) for r in rows])
        if equity is not None:
            drawdown.observe(equity, _now())
        return drawdown

    # ----------------------------------------------------------- account

    async def account_state_for(
        self,
        db: AsyncSession,
        *,
        account_id: str,
        environment: str,
        adapter: Any | None = None,
    ) -> AccountState:
        """The account, from whichever system owns it. Sections 6, 29 and 41.

        Paper reads the paper engine's own row; live and demo read the BROKER
        through a connected adapter. Neither is derived from the other and the
        two are never pooled -- §41 -- because they are different tables, and a
        query that forgot to filter cannot reach across.

        A broker with no connected adapter produces `unavailable`, not the last
        values seen. §31: an uncertain broker state must be exposed as such.
        """
        if environment == "paper":
            row = await db.get(PaperAccount, account_id)
            if row is None:
                return account_state.unavailable(
                    account_id, environment, "no paper account with that id exists."
                )
            return from_paper_account(row)

        broker = await db.get(BrokerAccount, account_id)
        if broker is None:
            return account_state.unavailable(
                account_id, environment, "no broker account with that id exists."
            )
        if adapter is None:
            return account_state.unavailable(
                account_id,
                broker.account_mode,
                "no broker adapter is connected for this account, so its balance and "
                "equity cannot be read. Reported as unavailable rather than from the "
                "last snapshot, which would be a stale figure shown as a current one.",
            )
        try:
            found = await adapter.get_account()
        except Exception as exc:  # noqa: BLE001 - a read failure is a state, not a 500
            log.warning(
                "broker account read failed",
                extra={"event": "portfolio_account_unreadable", "account_id": account_id},
            )
            return account_state.unavailable(
                account_id,
                broker.account_mode,
                f"the broker adapter could not be read ({type(exc).__name__}).",
            )
        return from_broker_account(
            found, account_id=account_id, environment=broker.account_mode, as_of=_now()
        )

    # --------------------------------------------------------- reconciliation

    async def reconcile_for(
        self,
        db: AsyncSession,
        *,
        account_id: str,
        environment: str,
        adapter: Any | None = None,
    ) -> Reconciliation:
        """Compare internal positions to the broker's, and REPORT. Sections 30, 31.

        **Read-only, deliberately.** L21's `PositionReconciler.sweep` settles and
        repairs; §63 forbids this engine from modifying a position, so this
        compares and says what it found. Repair stays behind L21's own route,
        where an operator asks for it.

        No adapter means `not_checked`, never `agrees`: an unchecked portfolio
        and a checked one that matched must not look alike.
        """
        if environment == "paper":
            return account_state.not_checked(
                "a paper account has no external venue to reconcile against; the paper "
                "engine is itself the source of truth for it."
            )
        if adapter is None:
            return account_state.not_checked("no broker adapter is connected for this account.")
        try:
            held = await adapter.get_positions()
        except Exception as exc:  # noqa: BLE001
            return account_state.not_checked(
                f"the broker's positions could not be read ({type(exc).__name__})."
            )

        internal = await self.positions_for(db, account_id=account_id, environment=environment)
        ours = {p.position_id for p in internal}
        theirs = {str(getattr(p, "position_id", "")) for p in held}
        mismatches = tuple(
            sorted(
                [f"{pid}: held internally, absent at the broker" for pid in ours - theirs]
                + [f"{pid}: held at the broker, absent internally" for pid in theirs - ours]
            )
        )
        return Reconciliation(
            checked=True,
            agrees=not mismatches,
            internal_positions=len(ours),
            broker_positions=len(theirs),
            mismatches=mismatches,
            checked_at=_now(),
            note=(
                "compared, never repaired. Settling a discrepancy is L21's reconciler, "
                "behind its own route: the portfolio engine reports state and modifies "
                "nothing."
            ),
        )

    # --------------------------------------------------------------- marks

    async def marks_for(
        self,
        positions: list[exposure_engine.PositionView],
        *,
        adapter: Any | None = None,
    ) -> tuple[dict[str, Decimal], tuple[str, ...]]:
        """Current mid prices for the open symbols, and what could not be priced.

        **A live quote or nothing.** A stored bar close is a price from the
        past, and marking an open position at it would put a stale figure in
        an `equity` column -- the exact failure §31 names. So a symbol without
        a live quote is returned as unpriced, the position is unmarked, and
        `unrealized_of` refuses the whole total rather than reporting a partial
        one as though it were complete.

        A paper account has no adapter and needs none: the paper engine marks
        with the same prices its own fills used, and its unrealized figure
        comes from `paper_accounts` rather than from being recomputed here.
        """
        wanted = sorted({p.symbol for p in positions})
        if adapter is None or not wanted:
            return {}, tuple(wanted)
        prices: dict[str, Decimal] = {}
        unpriced: list[str] = []
        for symbol in wanted:
            try:
                quote = await adapter.get_quote(symbol)
            except Exception:  # noqa: BLE001 - an unquotable symbol is a gap, not a 500
                unpriced.append(symbol)
                continue
            bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
            if bid is None or ask is None:
                unpriced.append(symbol)
                continue
            prices[symbol] = (bid + ask) / 2
        return prices, tuple(unpriced)

    # ------------------------------------------------------------ the view

    async def build(
        self,
        db: AsyncSession,
        *,
        account: AccountState,
        reconciliation: Reconciliation | None = None,
        prices: dict[str, Decimal] | None = None,
        now: datetime | None = None,
    ) -> PortfolioView:
        """One complete portfolio view. Never raises; never fabricates."""
        at = now or _now()
        account = _with_freshness(account, now=at, tolerance=self.freshness)

        positions = await self.positions_for(
            db,
            account_id=account.account_id,
            environment=account.environment,
            prices=prices,
        )
        positions = [_marked(p) for p in positions]
        report = exposure_engine.compute(positions)
        risk = exposure_engine.open_risk(positions)
        profit = await self.pnl_for(
            db,
            account_id=account.account_id,
            environment=account.environment,
            positions=positions,
            now=at,
        )
        drawdown = await self.drawdown_for(
            db,
            account_id=account.account_id,
            environment=account.environment,
            equity=account.equity,
        )
        margin = pnl_engine.margin_utilisation(
            account.margin_used, account.equity, warning_at=self.margin_warning
        )
        checked = reconciliation or account_state.not_checked(
            "no broker reconciliation was supplied to this view."
        )
        health, reasons = account_state.health_of(
            account=account,
            reconciliation=checked,
            margin_utilisation=margin.ratio,
            margin_warning=self.margin_warning,
        )

        return PortfolioView(
            account=account,
            positions=positions,
            exposure=report,
            pnl=profit,
            drawdown=drawdown,
            margin=margin,
            open_risk=risk,
            reconciliation=checked,
            health=health,
            health_reasons=reasons,
            at=at,
        )

    async def publish(
        self,
        hub: Any | None,
        *,
        previous: PortfolioView | None,
        current: PortfolioView,
    ) -> list[str]:
        """Emit what CHANGED, through L07's hub. Section 32.

        Never a second realtime system, and never a periodic republish of the
        whole view: `changes_between` compares against the previous one, so a
        client learns about a movement rather than being told the same figures
        on a timer.

        A publish failure is logged and swallowed. A portfolio read does not
        fail because a socket did -- the same rule the monitoring service
        applies, and the same reason: the caller asked for state, not delivery.
        """
        if hub is None:
            return []
        from app.core.events import Event
        from app.portfolio import events as portfolio_events

        sent: list[str] = []
        for event_type, payload in portfolio_events.changes_between(previous, current):
            try:
                await hub.publish(
                    Event(
                        type=event_type,
                        payload=payload,
                        source="portfolio",
                        channel=f"account:{current.account.account_id}",
                    )
                )
            except Exception:  # noqa: BLE001 - a read does not fail because an event did
                log.warning(
                    "a portfolio event could not be published",
                    extra={
                        "event": "portfolio_event_failed",
                        "account_id": current.account.account_id,
                        "type": event_type,
                    },
                )
                continue
            sent.append(event_type)
        return sent

    async def record(
        self, db: AsyncSession, view: PortfolioView, *, user_id: str | None = None
    ) -> PortfolioSnapshot | None:
        """Persist the view as a `portfolio_snapshots` row. Section 23 and §34.

        Reuses the table L05 created rather than adding one -- §34 says not to
        duplicate what exists. A view whose account could not be read is NOT
        recorded: §59 forbids writing fabricated values, and a snapshot of
        nothing would enter the equity curve as a real point.
        """
        if view.account.balance is None or view.account.equity is None:
            log.info(
                "portfolio snapshot skipped",
                extra={
                    "event": "portfolio_snapshot_skipped",
                    "account_id": view.account.account_id,
                    "reason": "balance or equity unavailable",
                },
            )
            return None

        row = PortfolioSnapshot(
            user_id=user_id,
            broker_account_id=(
                view.account.account_id if view.account.environment != "paper" else None
            ),
            paper_account_id=(
                view.account.account_id if view.account.environment == "paper" else None
            ),
            mode=view.account.environment,
            taken_at=view.at,
            balance=view.account.balance,
            equity=view.account.equity,
            margin=view.account.margin_used,
            free_margin=view.account.margin_free,
            open_positions=len(view.positions),
            exposure=(
                {k: str(v) for k, v in view.exposure.by_currency.items()}
                if view.exposure and view.exposure.currency_available
                else None
            ),
            drawdown_pct=(
                view.drawdown.current_pct
                if view.drawdown and view.drawdown.current_pct is not None
                else None
            ),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


def _marked(position: exposure_engine.PositionView) -> exposure_engine.PositionView:
    """The same position, carrying its unrealized P&L when it can be computed."""
    from dataclasses import replace

    return replace(position, unrealized_pnl=exposure_engine.mark_to_market(position))


def _with_freshness(account: AccountState, *, now: datetime, tolerance: timedelta) -> AccountState:
    from dataclasses import replace

    return replace(
        account,
        freshness=account_state.freshness_of(account.as_of, now=now, tolerance=tolerance),
    )


def from_broker_account(
    account: Any, *, account_id: str, environment: str, as_of: datetime
) -> AccountState:
    """A `brokers.base.Account` as an `AccountState`. Sections 6, 7 and 8.

    **The broker's own balance and equity are preferred and are not
    recomputed.** §6 and §7: the broker's semantics are authoritative, and
    deriving equity as balance + unrealized here would double-count whenever the
    broker had already done so.
    """
    return AccountState(
        account_id=account_id,
        environment=environment,
        currency=account.currency,
        broker=getattr(account, "server", None),
        balance=account.balance,
        equity=account.equity,
        margin_used=account.margin,
        margin_free=account.free_margin,
        margin_level=((account.equity / account.margin) if account.margin else None),
        as_of=as_of,
        source=Source.broker,
        freshness=Freshness.fresh,
    )


def from_paper_account(row: PaperAccount) -> AccountState:
    """A `paper_accounts` row as an `AccountState`. Sections 6, 29 and 41.

    **The paper engine is the source of truth for a paper account**, exactly as
    the broker is for a live one, and `persist_account` is what writes these
    figures. Reading the row rather than replaying the trade history is L16's
    own decision, kept: two derivations of one fact can disagree, and then
    neither is trustworthy.

    `as_of` is the row's `updated_at`, so a paper account no bot has touched
    for an hour reports STALE rather than presenting an hour-old equity as
    current. That is §44 applied to the simulator, and it is deliberate that
    the paper side is held to the same freshness rule as the live one.
    """
    return AccountState(
        account_id=row.id,
        environment="paper",
        currency=row.currency,
        broker=None,
        balance=row.balance,
        equity=row.equity,
        # The simulator holds no margin: there is no counterparty to require
        # it. `None` rather than 0, because a zero here would read as "nothing
        # is committed" when the truth is "the question does not apply".
        margin_used=None,
        margin_free=None,
        margin_level=None,
        unrealized_pnl=row.unrealized_pnl,
        realized_pnl=row.realized_pnl,
        as_of=row.updated_at,
        source=Source.paper_engine,
    )


def from_paper_portfolio(
    portfolio: Any,
    *,
    account_id: str,
    prices: dict[str, Decimal] | None = None,
    as_of: datetime | None = None,
) -> AccountState:
    """A `PaperPortfolio` as an `AccountState`. Section 41.

    The paper engine is authoritative for a paper account exactly as the broker
    is for a live one, and the environment is recorded so the two can never be
    pooled: §41 says paper and live data must never mix, and this is where that
    would otherwise happen.

    A position that cannot be marked makes equity unavailable rather than wrong
    -- `marks_missing` is the paper engine's own answer to that question, and it
    is asked rather than assumed away.
    """
    marks = prices or {}
    missing = portfolio.marks_missing(marks)
    if missing:
        return account_state.unavailable(
            account_id,
            "paper",
            f"no mark for {', '.join(missing)}, so equity cannot be computed. Reported "
            "as unavailable rather than valued at entry, which would be a stale figure "
            "presented as a current one.",
        )
    return AccountState(
        account_id=account_id,
        environment="paper",
        currency=getattr(portfolio, "currency", None),
        balance=portfolio.balance,
        equity=portfolio.equity(marks),
        unrealized_pnl=portfolio.unrealized(marks),
        realized_pnl=portfolio.realized_pnl(),
        as_of=as_of or _now(),
        source=Source.paper_engine,
        freshness=Freshness.fresh,
    )
