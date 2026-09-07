"""The analytics service. It reads; it owns nothing and decides nothing.

Section 3's hierarchy, kept literally:

    TRADE level     -> the trade journal (L31). Realized P&L, win/loss,
                       expectancy, profit factor, duration, streaks.
    POSITION level  -> the position manager (L21), through the portfolio engine.
    ACCOUNT level   -> the portfolio engine (L30). Balance, equity, margin,
                       exposure. **Never a second portfolio state** -- §54.
    ORDER level     -> the OMS (L19). Counts, states, slippage, latency.
    AI level        -> `ai_decisions` (L27) and the registry (L28).
    BACKTEST level  -> the existing backtest engine (L14). **Never a second
                       simulator** -- §22.

**One trade row is one trade.** Section 41. Every aggregate here counts rows of
`trades` and joins only through 1:1 foreign keys, so a trade with three fills and
two partial closes is counted once. The journal already made that true by writing
one row per position episode; analytics must not undo it with a fan-out join.

**Nothing is fabricated and nothing is hidden.** Sections 25 and 48. A filter
matching no trades returns an empty metric block that says so; a ratio from too
small a sample returns `INSUFFICIENT_DATA`; and no losing trade, extreme value or
unprofitable period is dropped anywhere in this module.

**Analytics never acts.** Section 49. This module imports no order manager, no
sizer, no risk decision and no broker write path, and a parsed test keeps that
true.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import equity as equity_engine
from app.analytics import metrics as metric_engine
from app.analytics import windows as window_engine
from app.analytics.metrics import INSUFFICIENT_DATA, Observation, Series, Unit
from app.analytics.windows import Bucket, Window
from app.models.ai_integration import AiDecisionRecord
from app.models.execution import Execution, Order, Position, Trade
from app.models.journal import PortfolioSnapshot
from app.models.market import Symbol

log = logging.getLogger("app.analytics")

#: Environments this platform distinguishes. `backtest` is not a `trades.mode`
#: -- backtests live in their own tables -- and it is listed so a caller asking
#: for one is answered rather than silently given paper.
ENVIRONMENTS = ("paper", "demo", "live")

#: Section 24 and §25. Below this a comparison is reported WITH the sample size
#: and a warning rather than as a finding.
MIN_TRADES_FOR_COMPARISON = 30

#: The dimensions §11 to §15 ask for, and the column each reads.
DIMENSIONS: dict[str, Any] = {
    "strategy": Trade.strategy_version_id,
    "symbol": Trade.symbol_id,
    "bot": Trade.bot_id,
    "exit_reason": Trade.exit_reason,
    "mode": Trade.mode,
    "side": Trade.side,
}

#: Dimensions that live on `ai_decisions` and need its 1:1 join.
AI_DIMENSIONS: dict[str, Any] = {
    "model": AiDecisionRecord.model_key,
    "model_version": AiDecisionRecord.model_version,
    "ai_mode": AiDecisionRecord.policy,
    "regime": AiDecisionRecord.regime,
}


class AnalyticsError(Exception):
    """A request analytics cannot answer, and the message says why."""


@dataclass(frozen=True)
class Scope:
    """What a question is about. Section 31.

    `environment` is separate from every other filter and is never defaulted:
    §23 requires the separation to be enforced, and a default would decide it
    silently for whoever forgot to ask.
    """

    window: Window
    environment: str | None = None
    account_id: str | None = None
    strategy_version_id: str | None = None
    symbol: str | None = None
    bot_id: str | None = None
    model_key: str | None = None
    model_version: str | None = None
    ai_mode: str | None = None
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "account_id": self.account_id,
            "strategy_version_id": self.strategy_version_id,
            "symbol": self.symbol,
            "bot_id": self.bot_id,
            "model_key": self.model_key,
            "model_version": self.model_version,
            "ai_mode": self.ai_mode,
            "period": self.window.as_dict(),
            "label": self.label,
            "note": (
                "environment is never defaulted. A request that does not name one "
                "gets every environment, with each row carrying its own -- silently "
                "choosing paper would hide live trades from somebody who asked for all."
            ),
        }

    @property
    def needs_ai_join(self) -> bool:
        return bool(self.model_key or self.model_version or self.ai_mode)


@dataclass
class Timed:
    """Section 61. How long a calculation took, without a log line per metric."""

    started: float = field(default_factory=time.perf_counter)

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000


class AnalyticsService:
    """Reads the systems that own each fact. Owns none of them."""

    # ------------------------------------------------------------- scoping

    def _scoped(self, stmt: Select, scope: Scope, *, symbol_id: str | None) -> Select:
        """Every filter, applied once, in one place.

        One function rather than a copy per endpoint: a summary computed over
        one filter set beside a table computed over a slightly different one is
        a discrepancy nobody notices until they add the table up by hand, and
        §41 asks that analytics agree with itself and with the journal.
        """
        if scope.environment:
            stmt = stmt.where(Trade.mode == scope.environment)
        if scope.account_id:
            stmt = stmt.where(
                (Trade.paper_account_id == scope.account_id)
                | (Trade.broker_account_id == scope.account_id)
            )
        if scope.strategy_version_id:
            stmt = stmt.where(Trade.strategy_version_id == scope.strategy_version_id)
        if symbol_id:
            stmt = stmt.where(Trade.symbol_id == symbol_id)
        if scope.bot_id:
            stmt = stmt.where(Trade.bot_id == scope.bot_id)
        if scope.window.start:
            stmt = stmt.where(Trade.closed_at >= scope.window.start)
        if scope.window.end:
            # Half-open: `<` rather than `<=`, so summing adjacent windows
            # cannot count a boundary trade twice.
            stmt = stmt.where(Trade.closed_at < scope.window.end)
        if scope.needs_ai_join:
            # A 1:1 join through the trade's own foreign key. It cannot fan out,
            # so the trade count is unchanged -- which §41 requires.
            stmt = stmt.join(AiDecisionRecord, Trade.ai_decision_id == AiDecisionRecord.id)
            if scope.model_key:
                stmt = stmt.where(AiDecisionRecord.model_key == scope.model_key)
            if scope.model_version:
                stmt = stmt.where(AiDecisionRecord.model_version == scope.model_version)
            if scope.ai_mode:
                stmt = stmt.where(AiDecisionRecord.policy == scope.ai_mode)
        return stmt

    async def _symbol_id(self, db: AsyncSession, code: str | None) -> str | None:
        if not code:
            return None
        found = await db.scalar(select(Symbol.id).where(Symbol.code == code.upper()))
        if found is None:
            # A filter that silently stops applying returns the wrong set as
            # though it were the right one.
            raise AnalyticsError(f"no symbol {code!r} is registered")
        return found

    async def trades_for(self, db: AsyncSession, scope: Scope) -> list[Trade]:
        """The completed trades a scope selects, oldest close first."""
        symbol_id = await self._symbol_id(db, scope.symbol)
        stmt = self._scoped(select(Trade), scope, symbol_id=symbol_id).order_by(Trade.closed_at)
        return list((await db.scalars(stmt)).all())

    # ------------------------------------------------------------- series

    def series_of(self, rows: list[Trade], unit: Unit) -> Series:
        """One unit's series from the journal rows. Nothing derived.

        `r` is read from `r_multiple` and a trade without one is EXCLUDED rather
        than given a value -- a trade whose planned risk was never recorded has
        no R, and inventing one from the realised loss would make every loser
        exactly -1R by construction.
        """
        observations: list[Observation] = []
        for row in rows:
            if unit is Unit.r:
                if row.r_multiple is None:
                    continue
                value = float(row.r_multiple)
            elif unit is Unit.currency:
                value = float(row.net_profit)
            else:  # pragma: no cover - points are a backtest unit, not a journal one
                raise AnalyticsError(f"the trade journal records no {unit} figure")
            observations.append(
                Observation(
                    value=value,
                    at=row.closed_at,
                    opened_at=row.opened_at,
                    costs=float(row.commission or 0) + float(row.swap or 0) + float(row.fees or 0),
                )
            )
        return Series.of(observations, unit)

    # ------------------------------------------------------------ summary

    async def summary(self, db: AsyncSession, scope: Scope) -> dict[str, Any]:
        """Section 29's contract, in BOTH units, each labelled.

        Two blocks rather than one, because they answer different questions and
        only one of them pools. `CLAUDE.md`: net currency cannot be pooled across
        trades sized by different stop distances -- so `currency` is the figure
        for one account's actual result and `r` is the figure for judging a rule.
        """
        clock = Timed()
        rows = await self.trades_for(db, scope)
        currency = self.series_of(rows, Unit.currency)
        r = self.series_of(rows, Unit.r)

        block = {
            "scope": scope.as_dict(),
            "trade_count": len(rows),
            "currency": (
                metric_engine.core(currency) if rows else metric_engine.empty(Unit.currency)
            ),
            "r_multiple": (
                metric_engine.core(r)
                if len(r)
                else metric_engine.empty(
                    Unit.r,
                    why=(
                        "no trade in this set records an R-multiple. R needs the planned "
                        "risk at entry, and a trade without one is excluded rather than "
                        "assigned a value -- deriving R from the realised loss would make "
                        "every loser exactly -1R by construction."
                    ),
                )
            ),
            "costs": self._costs(rows),
            "environments": self._environments(rows),
            "which_to_read": (
                "R pools across trades and net currency does not. A trade's size is set "
                "by its stop distance, so pooling currency adds numbers that are not the "
                "same quantity -- structurally the metals-points error wearing a lot size."
            ),
            "calculation_ms": round(clock.elapsed_ms(), 2),
        }
        if len(rows) < MIN_TRADES_FOR_COMPARISON:
            block["sample_warning"] = (
                f"{len(rows)} trades. Below {MIN_TRADES_FOR_COMPARISON} these figures "
                "describe the sample and support no claim about the rule. This "
                "repository's own live record reached a t of 9.33 on 14 trades and it "
                "meant nothing."
            )
        return block

    def _costs(self, rows: list[Trade]) -> dict[str, Any]:
        """Section 6. Each cost separately, and gross against net.

        Never assumed to be zero when the data exists, and never folded: MT5
        reports commission and fees apart, and adding one into the other makes
        the total right and each part wrong.
        """
        commission = sum(float(r.commission or 0) for r in rows)
        swap = sum(float(r.swap or 0) for r in rows)
        fees = sum(float(r.fees or 0) for r in rows)
        gross = sum(float(r.gross_profit) for r in rows)
        net = sum(float(r.net_profit) for r in rows)
        return {
            "gross_profit": gross,
            "commission": commission,
            "swap": swap,
            "fees": fees,
            "total_costs": commission + swap + fees,
            "net_profit": net,
            "reconciles": abs((gross - commission - swap - fees) - net) < 1e-4,
            "note": (
                "each cost is recorded separately and none is folded into another. "
                "`reconciles` is false when a cost was booked twice or not at all -- "
                "the journal's own data-quality check, aggregated."
            ),
            # `slippage` is deliberately absent: it is recorded per FILL in
            # `executions`, not per trade, and summing a per-fill points figure
            # into an account-currency total would be a unit error.
            "slippage": (
                "see /v1/analytics/execution -- it is a per-fill figure in points, "
                "not a per-trade one in account currency"
            ),
        }

    def _environments(self, rows: list[Trade]) -> dict[str, int]:
        """Section 23. Which environments this set actually spans, always shown."""
        found: dict[str, int] = {}
        for row in rows:
            found[row.mode] = found.get(row.mode, 0) + 1
        return found

    # ------------------------------------------------------ equity, drawdown

    async def equity(self, db: AsyncSession, scope: Scope) -> dict[str, Any]:
        """Both curves, each labelled. Section 7."""
        rows = await self.trades_for(db, scope)
        environment = scope.environment or "mixed"
        realized = equity_engine.Curve.realized_from(
            [(row.closed_at, float(row.net_profit), row.id) for row in rows],
            environment=environment,
        )
        account = await self._account_curve(db, scope)
        return {
            "scope": scope.as_dict(),
            "realized": realized.as_dict(),
            "account": account,
            "note": (
                "two curves, never merged. This platform records no cash movements, so "
                "the account curve cannot tell a deposit from a profit -- read the "
                "realized curve for performance. Section 7."
            ),
        }

    async def _account_curve(self, db: AsyncSession, scope: Scope) -> dict[str, Any]:
        """Account equity from `portfolio_snapshots`. The portfolio owns it."""
        if not scope.account_id:
            return {
                "available": False,
                "why": (
                    "an account curve is per account. Name one, or read the realized "
                    "curve, which is the performance figure."
                ),
            }
        stmt = select(PortfolioSnapshot).where(
            (PortfolioSnapshot.paper_account_id == scope.account_id)
            | (PortfolioSnapshot.broker_account_id == scope.account_id)
        )
        if scope.environment:
            stmt = stmt.where(PortfolioSnapshot.mode == scope.environment)
        if scope.window.start:
            stmt = stmt.where(PortfolioSnapshot.taken_at >= scope.window.start)
        if scope.window.end:
            stmt = stmt.where(PortfolioSnapshot.taken_at < scope.window.end)
        rows = list((await db.scalars(stmt.order_by(PortfolioSnapshot.taken_at))).all())
        if not rows:
            return {
                "available": False,
                "why": "no portfolio snapshot has been recorded for this account in this window",
            }
        curve = equity_engine.Curve.account_from(
            [(row.taken_at, float(row.equity), row.id) for row in rows],
            environment=scope.environment or rows[0].mode,
        )
        block = curve.as_dict()
        block["available"] = True
        return block

    async def drawdown(self, db: AsyncSession, scope: Scope) -> dict[str, Any]:
        """Section 8, over the realized curve by default."""
        rows = await self.trades_for(db, scope)
        curve = equity_engine.Curve.realized_from(
            [(row.closed_at, float(row.net_profit), row.id) for row in rows],
            environment=scope.environment or "mixed",
        )
        analysis = equity_engine.analyse(curve)
        analysis["scope"] = scope.as_dict()
        analysis["calmar"] = (
            equity_engine.calmar(
                curve.points[-1].value if curve.points else 0.0,
                curve.starting_value,
                float(analysis.get("max_drawdown") or 0.0),
            )
            if analysis.get("available")
            else INSUFFICIENT_DATA
        )
        return analysis

    # --------------------------------------------------------- breakdowns

    async def by(self, db: AsyncSession, scope: Scope, dimension: str) -> dict[str, Any]:
        """Performance grouped by one dimension. Sections 11 to 15.

        Grouped in Python over the rows the scope already selected, rather than
        with a second set of SQL aggregates. Two reasons: the metric definitions
        stay in `metrics.py` where §5 wants them, and the grouped totals cannot
        disagree with the ungrouped summary because they are the same rows.
        """
        if dimension not in DIMENSIONS and dimension not in AI_DIMENSIONS:
            raise AnalyticsError(
                f"{dimension!r} is not a dimension. Known: "
                f"{sorted(set(DIMENSIONS) | set(AI_DIMENSIONS))}"
            )
        rows = await self.trades_for(db, scope)
        keys = await self._keys_for(db, rows, dimension)

        grouped: dict[str, list[Trade]] = {}
        for row in rows:
            grouped.setdefault(keys.get(row.id) or "unattributed", []).append(row)

        out: dict[str, Any] = {}
        for key, subset in sorted(grouped.items()):
            currency = self.series_of(subset, Unit.currency)
            r = self.series_of(subset, Unit.r)
            out[key] = {
                "trades": len(subset),
                "currency": metric_engine.core(currency),
                "r_multiple": (metric_engine.core(r) if len(r) else metric_engine.empty(Unit.r)),
                "below_comparison_floor": len(subset) < MIN_TRADES_FOR_COMPARISON,
            }
        return {
            "scope": scope.as_dict(),
            "dimension": dimension,
            "groups": out,
            "group_count": len(out),
            "total_trades": len(rows),
            "note": (
                "sample sizes are on every group. Section 24: a comparison without them "
                "invites reading a difference between 43 trades and 11 as a finding."
            ),
        }

    async def _keys_for(
        self, db: AsyncSession, rows: list[Trade], dimension: str
    ) -> dict[str, str | None]:
        """The grouping key per trade id."""
        if dimension == "symbol":
            found = await db.execute(
                select(Symbol.id, Symbol.code).where(Symbol.id.in_({r.symbol_id for r in rows}))
            )
            codes: dict[str, str] = {row[0]: row[1] for row in found.all()}
            return {r.id: codes.get(r.symbol_id) for r in rows}
        if dimension in AI_DIMENSIONS:
            ids = {r.ai_decision_id for r in rows if r.ai_decision_id}
            if not ids:
                return {r.id: None for r in rows}
            column = AI_DIMENSIONS[dimension]
            result = await db.execute(
                select(AiDecisionRecord.id, column).where(AiDecisionRecord.id.in_(ids))
            )
            labels: dict[str, str | None] = {row[0]: row[1] for row in result.all()}
            return {
                r.id: (labels.get(r.ai_decision_id) if r.ai_decision_id else None) for r in rows
            }
        attribute = {
            "strategy": "strategy_version_id",
            "bot": "bot_id",
            "exit_reason": "exit_reason",
            "mode": "mode",
            "side": "side",
        }[dimension]
        return {r.id: getattr(r, attribute) for r in rows}

    async def time_breakdown(
        self, db: AsyncSession, scope: Scope, bucket: Bucket
    ) -> dict[str, Any]:
        """Section 10. Bucketed in Python from UTC, never with `date_trunc`."""
        rows = await self.trades_for(db, scope)
        grouped: dict[str, list[Trade]] = {}
        for row in rows:
            grouped.setdefault(window_engine.key_for(row.closed_at, bucket), []).append(row)
        return {
            "scope": scope.as_dict(),
            "bucket": str(bucket),
            "buckets": {
                key: {
                    "trades": len(subset),
                    "currency": metric_engine.core(self.series_of(subset, Unit.currency)),
                }
                for key, subset in sorted(grouped.items())
            },
            "timezone": window_engine.TIMEZONE_POLICY,
        }

    # ------------------------------------------------------ execution, §16

    async def execution(self, db: AsyncSession, scope: Scope) -> dict[str, Any]:
        """Order and fill statistics from the OMS. Section 16.

        Latency is computed ONLY between timestamps that both exist. §16: do not
        invent latency if timestamps are missing, so a missing stamp excludes the
        order from that figure and the count of what was measurable is reported
        beside it.
        """
        stmt = select(Order)
        if scope.environment:
            stmt = stmt.where(Order.mode == scope.environment)
        if scope.account_id:
            stmt = stmt.where(
                (Order.paper_account_id == scope.account_id)
                | (Order.broker_account_id == scope.account_id)
            )
        if scope.window.start:
            stmt = stmt.where(Order.created_at >= scope.window.start)
        if scope.window.end:
            stmt = stmt.where(Order.created_at < scope.window.end)
        orders = list((await db.scalars(stmt)).all())

        by_status: dict[str, int] = {}
        for order in orders:
            by_status[order.status] = by_status.get(order.status, 0) + 1

        submit_latency = [
            (o.submitted_at - o.created_at).total_seconds()
            for o in orders
            if o.submitted_at and o.created_at
        ]
        fill_latency = [
            (o.filled_at - o.submitted_at).total_seconds()
            for o in orders
            if o.filled_at and o.submitted_at
        ]

        # An empty order set reads NO fills rather than every fill. `IN ()` is
        # not portable and a bare `False` is not a SQLAlchemy clause, so the
        # empty case short-circuits in Python.
        fills: list[Execution] = []
        if orders:
            fills = list(
                (
                    await db.scalars(
                        select(Execution).where(Execution.order_id.in_([o.id for o in orders]))
                    )
                ).all()
            )
        slippage = [float(f.slippage_points) for f in fills if f.slippage_points is not None]

        total = len(orders)
        filled = by_status.get("filled", 0)
        return {
            "scope": scope.as_dict(),
            "orders": total,
            "by_status": by_status,
            "fill_ratio": (filled / total) if total else INSUFFICIENT_DATA,
            "rejection_ratio": (by_status.get("rejected", 0) / total)
            if total
            else INSUFFICIENT_DATA,
            "partial_fills": sum(
                1
                for o in orders
                if o.filled_quantity and o.quantity and o.filled_quantity < o.quantity
            ),
            "latency_seconds": {
                "create_to_submit": metric_engine.distribution(submit_latency)
                if submit_latency
                else {"count": 0, "note": "no order carries both timestamps"},
                "submit_to_fill": metric_engine.distribution(fill_latency)
                if fill_latency
                else {"count": 0, "note": "no order carries both timestamps"},
                "measured_from": (
                    f"{len(submit_latency)} of {total} orders for create-to-submit and "
                    f"{len(fill_latency)} for submit-to-fill. An order missing a stamp is "
                    "excluded, never given an assumed one."
                ),
            },
            "slippage_points": (
                metric_engine.distribution(slippage)
                if slippage
                else {"count": 0, "note": "no fill recorded a slippage figure"}
            ),
            "slippage_note": (
                "per FILL and in POINTS, which is why it is not summed into the "
                "account-currency cost total. CLAUDE.md records the live fill 279 points "
                "from its quote that stayed invisible for three days -- read the fill, "
                "never the quote."
            ),
            "fills": len(fills),
        }

    # ------------------------------------------------------- comparison §24

    async def compare(self, db: AsyncSession, scopes: list[Scope]) -> dict[str, Any]:
        """Two or more scopes side by side. Section 24.

        **Differences, never causes.** Every phrase here is observational, and
        the sample sizes are the first thing in each block. §14 and §24: a
        comparison is not an experiment, and this platform has never run one --
        what separates two sets may be what the filter selected for rather than
        what the filter did.
        """
        if len(scopes) < 2:
            raise AnalyticsError("a comparison needs at least two scopes")
        if len(scopes) > 6:
            raise AnalyticsError(
                "a comparison of more than six scopes is a table, not a comparison"
            )

        blocks = [await self.summary(db, scope) for scope in scopes]
        counts = [block["trade_count"] for block in blocks]
        return {
            "scopes": [block["scope"] for block in blocks],
            "blocks": blocks,
            "sample_sizes": counts,
            "smallest_sample": min(counts),
            "comparable": min(counts) >= MIN_TRADES_FOR_COMPARISON,
            "language": (
                "observational. This is a difference between two SELECTED sets, not a "
                "measured effect: nothing here was randomised, so whatever separates "
                "them may be what the filter selected for rather than what it did. Say "
                "'this set had a higher win rate in this sample', never 'X improved "
                "performance'."
            ),
            "warning": (
                None
                if min(counts) >= MIN_TRADES_FOR_COMPARISON
                else f"the smallest set has {min(counts)} trades, below the "
                f"{MIN_TRADES_FOR_COMPARISON} at which a difference is worth reading. "
                "Sample sizes are shown per block."
            ),
        }

    # -------------------------------------------------------- portfolio, §54

    async def exposure(
        self, db: AsyncSession, scope: Scope, *, adapter: Any = None
    ) -> dict[str, Any]:
        """Section 17 and §54. Delegated to the portfolio engine, never recomputed.

        The portfolio owns balance, equity, margin and exposure. Computing a
        second one here would be the duplicate state §54 forbids, and the two
        would eventually disagree.
        """
        if not scope.account_id:
            return {
                "available": False,
                "why": "exposure is per account. Name one.",
            }
        from app.portfolio.service import PortfolioService

        portfolio = PortfolioService()
        account = await portfolio.account_state_for(
            db,
            account_id=scope.account_id,
            environment=scope.environment or "paper",
            adapter=adapter,
        )
        view = await portfolio.build(db, account=account)
        return {
            "available": True,
            "scope": scope.as_dict(),
            "exposure": view.exposure.as_dict() if view.exposure else None,
            "open_risk": view.open_risk,
            "margin": view.margin.as_dict() if view.margin else None,
            "health": str(view.health),
            "source": "the portfolio engine (L30). Analytics reads it and owns none of it.",
            "authority": (
                "the RISK ENGINE decides what a limit permits. Analytics reports "
                "exposure and changes nothing."
            ),
        }

    # ---------------------------------------------------------- positions

    async def open_positions(self, db: AsyncSession, scope: Scope) -> dict[str, Any]:
        """Section 3's POSITION level. Counted, never valued here."""
        stmt = select(func.count(Position.id)).where(
            Position.status.in_(("open", "partially_closed"))
        )
        if scope.environment:
            stmt = stmt.where(Position.mode == scope.environment)
        if scope.account_id:
            stmt = stmt.where(
                (Position.paper_account_id == scope.account_id)
                | (Position.broker_account_id == scope.account_id)
            )
        return {
            "open_positions": int(await db.scalar(stmt) or 0),
            "note": (
                "counted here; VALUED by the portfolio engine, which owns unrealised "
                "P&L and refuses a total when any position cannot be marked."
            ),
        }


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


__all__ = [
    "AI_DIMENSIONS",
    "DIMENSIONS",
    "ENVIRONMENTS",
    "MIN_TRADES_FOR_COMPARISON",
    "AnalyticsError",
    "AnalyticsService",
    "Scope",
    "utcnow",
]
