"""Continuous position monitoring, as a backend worker.

Runs in the backend, not the browser: closing a page must never stop a
position being watched.

The loop, the heartbeat, the survive-a-failing-pass behaviour and cooperative
shutdown come from `app.workers.base.Worker`, which was extracted at L02 from
the copy originally written here. This module is now only the position-specific
part: fetch quotes, sweep, and log what happened.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

from app.positions.manager import ManagedResult, PositionManager
from app.positions.policies import MarketState, RiskContext
from app.workers.base import Worker

log = logging.getLogger("app.positions.monitor")

QuoteSource = Callable[[], Awaitable[dict[str, MarketState]]]
ContextSource = Callable[[], Awaitable[RiskContext]]
FloatingSource = Callable[[], Awaitable[dict[str, Decimal]]]


class PositionMonitor(Worker):
    def __init__(
        self,
        manager: PositionManager,
        quotes: QuoteSource,
        context: ContextSource,
        floating: FloatingSource | None = None,
        interval_seconds: float = 5.0,
        mode: str | None = "paper",
        name: str = "position_monitor",
    ) -> None:
        super().__init__(name=name, interval_seconds=interval_seconds)
        self.manager = manager
        self.quotes = quotes
        self.context = context
        self.floating = floating
        self.mode = mode
        self.last_results: list[ManagedResult] = []

    @property
    def passes(self) -> int:
        return self.status.passes

    async def tick(self) -> None:
        quotes = await self.quotes()
        context = await self.context()
        floating = await self.floating() if self.floating else None
        results = await self.manager.run_once(quotes, context, self.mode, floating)
        self.last_results = results
        for result in results:
            if result.needs_reconciliation:
                log.error(
                    "close outcome unknown; position parked for reconciliation",
                    extra={
                        "event": "close_unknown",
                        "position_id": result.position_id,
                        "mode": self.mode,
                    },
                )
            elif result.closed and result.decision is not None:
                log.info(
                    "position closed",
                    extra={
                        "event": "position_closed",
                        "position_id": result.position_id,
                        "reason": str(result.decision.reason),
                        "mode": self.mode,
                    },
                )
