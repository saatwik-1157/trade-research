"""Continuous position monitoring, as a backend worker.

Runs in the backend, not the browser: closing a page must never stop a
position being watched.

The loop, the heartbeat, the survive-a-failing-pass behaviour and cooperative
shutdown come from `app.workers.base.Worker`, which was extracted at L02 from
the copy originally written here. This module is now only the position-specific
part: fetch quotes, sweep, publish, and log what happened.

**One session per pass, opened here.** This worker used to take an already
built `PositionManager`, which holds a session and a symbol cache for its
whole life. For a request-scoped manager that is right; for a loop meant to
run for the life of the process it is not -- a pinned session keeps a
connection, never clears its identity map, and stays poisoned after one
failed flush. Every other worker in this platform takes an
`async_sessionmaker` and opens a session per tick, and now so does this one.

**Events are published after the commit, and only then.** `run_once` commits,
and `PositionManager.drain_events` exists so that an event can never describe
something that was not saved. Nothing drained them until now: L21 wrote that
publishing them "is the monitor's job once L22 starts it", and the monitor was
never started.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.execution import Position
from app.positions.manager import ManagedResult, PositionManager
from app.positions.policies import MarketState, RiskContext
from app.workers.base import Worker

log = logging.getLogger("app.positions.monitor")

#: The open positions keyed by tradable CODE -> the quote each is judged
#: against. The row travels with the code because a broker position is priced
#: at the venue that holds it, and only the row says which venue that is.
QuoteSource = Callable[[AsyncSession, dict[str, Position]], Awaitable[dict[str, MarketState]]]
ContextSource = Callable[[], Awaitable[RiskContext]]
#: Keyed by OUR position id, matching what `run_once` looks up.
FloatingSource = Callable[[AsyncSession, list[Position]], Awaitable[dict[str, Decimal]]]
#: One `PositionManager` per pass, built against that pass's session.
ManagerFactory = Callable[[AsyncSession], PositionManager]


class PositionMonitor(Worker):
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        build: ManagerFactory,
        quotes: QuoteSource,
        context: ContextSource,
        floating: FloatingSource | None = None,
        interval_seconds: float = 5.0,
        mode: str | None = "paper",
        name: str = "position_monitor",
        publish: Callable[[object], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(name=name, interval_seconds=interval_seconds)
        self.sessions = sessions
        self.build = build
        self.quotes = quotes
        self.context = context
        self.floating = floating
        self.mode = mode
        self.publish = publish
        self.last_results: list[ManagedResult] = []

    @property
    def passes(self) -> int:
        return self.status.passes

    async def tick(self) -> None:
        async with self.sessions() as db:
            manager = self.build(db)
            rows = await manager.open_positions(self.mode)
            by_code = await manager.codes_for(rows)
            quotes = await self.quotes(db, by_code)
            context = await self.context()
            floating = await self.floating(db, rows) if self.floating else None
            # `run_once` commits before it returns.
            results = await manager.run_once(quotes, context, self.mode, floating)
            events = manager.drain_events()

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

        # AFTER the session closed, so the rows these describe are committed
        # and readable by whatever the event wakes up.
        await self._announce(events)

    async def _announce(self, events: Sequence[object]) -> None:
        if self.publish is None:
            return
        for event in events:
            try:
                await self.publish(event)
            except Exception as exc:  # noqa: BLE001 - a lost event is not a lost trade
                log.warning(
                    "a position event was not published; the record is unaffected",
                    extra={
                        "event": "position_event_publish_failed",
                        "reason": str(exc)[:200],
                        "mode": self.mode,
                    },
                )
