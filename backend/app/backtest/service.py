"""Backtest jobs: queue, run, store, publish.

Uses the `backtests` and `backtest_trades` tables from L05 and the asyncio task
model the platform already has. No second worker framework is introduced.

**A failed backtest is never `finished`.** The status is set before the work
starts, and the failure path sets `failed` with the reason. A run that crashed
and left `queued` would be indistinguishable from one still waiting, which is
why `started_at` and `finished_at` are both written.

**Results are stored where they belong.** Metrics and the equity curve go in
`backtests.summary` (a JSON column, and both are small); individual trades go
in `backtest_trades` as rows, because that is what the table is for and a few
hundred trades inside one JSON blob is a payload nobody can query.

The run happens in a background task rather than in the request, because a
prefix-walk backtest over several thousand bars takes seconds, not
milliseconds, and an API that blocks for that is an API with a timeout.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtest.config import ENGINE_VERSION, BacktestConfig, SizingMode
from app.backtest.runner import BacktestError, BacktestResult, run
from app.core.events import Event
from app.marketdata.base import ProviderUnavailable
from app.models.research import Backtest, BacktestTrade
from app.realtime.catalogue import EventType, Scope
from app.realtime.channels import Channel

log = logging.getLogger("app.backtest.service")

# Concurrent runs allowed per process. A prefix walk is CPU-bound, and letting
# a user queue fifty of them is a denial of service they did not intend.
MAX_CONCURRENT = 2
MAX_QUEUED_PER_USER = 5


class BacktestBusy(Exception):
    """Too many runs already in flight. Refused, not silently dropped."""


class BacktestService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        market_data: object,
        registry: object,
        hub: object | None = None,
    ) -> None:
        self.sessions = sessions
        self.market_data = market_data
        self.registry = registry
        self.hub = hub
        self._running: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    # ------------------------------------------------------------- queueing

    async def queue(
        self, db: AsyncSession, config: BacktestConfig, user_id: str, name: str
    ) -> Backtest:
        """Record a queued run and start it. Returns the row immediately."""
        config.validate()
        in_flight = await db.scalar(
            select(func.count())
            .select_from(Backtest)
            .where(
                Backtest.requested_by_user_id == user_id,
                Backtest.status.in_(("queued", "running")),
            )
        )
        if int(in_flight or 0) >= MAX_QUEUED_PER_USER:
            raise BacktestBusy(
                f"{in_flight} of your backtests are already queued or running; the "
                f"limit is {MAX_QUEUED_PER_USER}. Wait for one to finish"
            )

        row = Backtest(
            name=name,
            # The constrained vocabulary the L05 table already defines.
            # This IS rule_backtest: the runner calls its simulate().
            # The engine VERSION lives in params, so a result stays
            # traceable without widening a check constraint for a value
            # that names the same thing.
            engine="rule_backtest",
            timeframe=str(config.timeframe),
            universe=[config.symbol],
            # Every assumption is stored with the run, so a result can always be
            # traced back to what produced it.
            params=config.describe(),
            status="queued",
            requested_by_user_id=user_id,
        )
        db.add(row)
        await db.flush()
        backtest_id = row.id
        await db.commit()

        task = asyncio.create_task(
            self._execute(backtest_id, config), name=f"backtest:{backtest_id}"
        )
        self._running[backtest_id] = task
        task.add_done_callback(lambda _t: self._running.pop(backtest_id, None))
        return row

    async def cancel(self, backtest_id: str) -> bool:
        task = self._running.get(backtest_id)
        if task is None or task.done():
            return False
        task.cancel()
        async with self.sessions() as db:
            row = await db.get(Backtest, backtest_id)
            if row is not None and row.status in ("queued", "running"):
                row.status = "cancelled"
                row.finished_at = datetime.now(UTC).replace(tzinfo=None)
                await db.commit()
        return True

    # -------------------------------------------------------------- running

    async def _execute(self, backtest_id: str, config: BacktestConfig) -> None:
        async with self._semaphore:
            async with self.sessions() as db:
                row = await db.get(Backtest, backtest_id)
                if row is None or row.status == "cancelled":
                    return
                row.status = "running"
                row.started_at = datetime.now(UTC).replace(tzinfo=None)
                await db.commit()

            try:
                result = await self._run(config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - recorded as failed, never as done
                log.warning(
                    "backtest failed",
                    extra={
                        "event": "backtest_failed",
                        "backtest_id": backtest_id,
                        "error": type(exc).__name__,
                    },
                )
                async with self.sessions() as db:
                    row = await db.get(Backtest, backtest_id)
                    if row is not None:
                        # A failed run must never look completed.
                        row.status = "failed"
                        row.error = f"{type(exc).__name__}: {exc}"[:500]
                        row.finished_at = datetime.now(UTC).replace(tzinfo=None)
                        await db.commit()
                return

            await self._store(backtest_id, result)

    async def _run(self, config: BacktestConfig) -> BacktestResult:
        async with self.sessions() as db:
            try:
                series = await self.market_data.get_bars(  # type: ignore[attr-defined]
                    db, config.symbol, config.timeframe, config.provider, limit=config.max_bars
                )
            except ProviderUnavailable as exc:
                raise BacktestError(f"no historical data: {exc}") from exc

        bars = series.series.bars
        if config.start:
            bars = [b for b in bars if b.bar_time >= config.start]
        if config.end:
            bars = [b for b in bars if b.bar_time <= config.end]

        strategy = self.registry.create(  # type: ignore[attr-defined]
            config.strategy_key, config.strategy_config
        )
        # The instrument's measured contract terms, needed by the risk sizing
        # modes (L18). Loaded from the platform's own symbol mappings -- the
        # same source the paper engine and the broker layer read, never a
        # second metadata system. A missing spec is surfaced as a refusal
        # naming what is absent, because sizing from a default is the failure
        # `app.sizing` exists to prevent.
        spec = None
        if config.sizing_mode is not SizingMode.fixed_quantity:
            from app.symbols.errors import SymbolError
            from app.symbols.service import contract_spec

            async with self.sessions() as db:
                try:
                    spec = await contract_spec(db, config.symbol)
                except SymbolError as exc:
                    raise BacktestError(
                        f"{config.sizing_mode} sizing needs a contract spec for "
                        f"{config.symbol}: {exc}"
                    ) from exc

        # The prefix walk is CPU-bound; a thread keeps the event loop answering.
        return await asyncio.to_thread(run, config, strategy, bars, spec)

    async def _store(self, backtest_id: str, result: BacktestResult) -> None:
        async with self.sessions() as db:
            row = await db.get(Backtest, backtest_id)
            if row is None:  # pragma: no cover - the row was created above
                return
            # The L05 vocabulary calls a successful run `finished`. The brief
            # says COMPLETED; they name the same state, and using the
            # existing word beats widening a check constraint to add a
            # synonym.
            row.status = "finished"
            row.finished_at = datetime.now(UTC).replace(tzinfo=None)
            # Metrics and the curve are small and belong with the run. The
            # trades are rows, because that is what the table is for.
            row.summary = {
                "metrics": result.metrics,
                "quality": result.quality.as_dict(),
                "equity_curve": result.equity_curve,
                "signals_generated": result.signals_generated,
                "bars_processed": result.bars_processed,
                "engine_version": ENGINE_VERSION,
                "fingerprint": result.config.fingerprint(),
            }
            for trade in result.trades:
                db.add(
                    BacktestTrade(
                        backtest_id=backtest_id,
                        candidate=result.config.strategy_key,
                        side=trade["side"],
                        entry_time=datetime.fromisoformat(trade["entry_time"]).replace(tzinfo=None),
                        exit_time=datetime.fromisoformat(trade["exit_time"]).replace(tzinfo=None),
                        entry_price=Decimal(trade["entry_price"]),
                        exit_price=Decimal(trade["exit_price"]),
                        pnl_points=Decimal(str(trade["net_points"])),
                        exit_reason=trade["exit_reason"],
                    )
                )
            await db.commit()

        if self.hub is not None:
            try:
                await self.hub.publish(  # type: ignore[attr-defined]
                    Event(
                        type=str(EventType.SYSTEM_ALERT),
                        payload={
                            "message": "backtest completed",
                            "backtest_id": backtest_id,
                            "trades": result.metrics.get("trades", 0),
                        },
                        channel=str(Channel(Scope.system)),
                        source="backtest",
                    )
                )
            except Exception:  # noqa: BLE001 - a lost event is not a lost result
                log.warning(
                    "backtest stored but the event was not published",
                    extra={"event": "backtest_publish_failed", "backtest_id": backtest_id},
                )

    async def wait(self, backtest_id: str, timeout: float = 60.0) -> None:
        """Await a running job. For tests and for a synchronous caller."""
        task = self._running.get(backtest_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
