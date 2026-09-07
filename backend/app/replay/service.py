"""Replay sessions: lifecycle, the background driver, and the event stream.

A session lives in a background asyncio task, so **closing the browser does not
stop it**. The frontend controls a backend session; it does not host one.

Sessions are isolated by construction. Each holds its own clock, engine and
portfolio, and nothing in this package is module-level mutable state, so
session A cannot see or change session B's positions, balance or cursor.

**Replay can never reach a broker, and that is structural rather than
configured.** `ReplayEngine` holds no adapter and imports none; there is no
setting that could point it at a venue because there is no venue reference to
point. A test parses every module in the package and asserts the absence.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtest.config import BacktestConfig
from app.core.events import Event
from app.models.research import ReplaySession as ReplaySessionRow
from app.realtime.catalogue import EventType, Scope
from app.realtime.channels import Channel
from app.replay.clock import (
    TERMINAL,
    IllegalTransition,
    ReplayClock,
    ReplayState,
    check_speed,
    check_transition,
)
from app.replay.engine import EventKind, ReplayEngine, ReplayEvent, atr_for

log = logging.getLogger("app.replay.service")

MAX_SESSIONS_PER_USER = 3
# How many events to keep in memory for the state endpoint. A session over
# 5,000 bars produces tens of thousands; keeping them all would grow without
# bound, and the trades are the durable record.
EVENT_BUFFER = 500


class ReplayBusy(Exception):
    """Too many sessions already open for this user."""


@dataclass
class Session:
    """One live replay. Everything it touches is on this object."""

    id: str
    user_id: str
    config: BacktestConfig
    clock: ReplayClock
    engine: ReplayEngine
    state: ReplayState = ReplayState.created
    error: str | None = None
    recent: list[ReplayEvent] = field(default_factory=list)
    _resume: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, object]:
        now = self.clock.now()
        return {
            "session_id": self.id,
            "state": str(self.state),
            "simulated_time": now.isoformat() if now else None,
            "bars_revealed": self.clock.revealed,
            "bars_total": self.clock.total,
            "progress": round(self.clock.progress, 6),
            "speed": self.clock.speed,
            "sequence": self.clock._seq,
            "error": self.error,
            **self.engine.state(),
            "execution_mode": "REPLAY",
            "note": (
                "Simulated execution only. No broker adapter is reachable from a replay session."
            ),
        }


class ReplayService:
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
        self.live: dict[str, Session] = {}

    # ------------------------------------------------------------- creation

    async def create(self, db: AsyncSession, config: BacktestConfig, user_id: str) -> Session:
        config.validate()
        mine = [s for s in self.live.values() if s.user_id == user_id and s.state not in TERMINAL]
        if len(mine) >= MAX_SESSIONS_PER_USER:
            raise ReplayBusy(
                f"{len(mine)} replay sessions are already open; the limit is "
                f"{MAX_SESSIONS_PER_USER}. Stop one first"
            )

        series = await self.market_data.get_bars(  # type: ignore[attr-defined]
            db, config.symbol, config.timeframe, config.provider, limit=config.max_bars
        )
        bars = [b for b in series.series.bars if b.complete]
        if config.start:
            bars = [b for b in bars if b.bar_time >= config.start]
        if config.end:
            bars = [b for b in bars if b.bar_time <= config.end]
        if len(bars) < 62:
            raise ValueError(
                f"{len(bars)} bars in this window; a replay needs more than 61 before "
                "it can open anything. This is a data shortfall, not a session"
            )
        # The dataset is validated before it is replayed, using L08's checks.
        # Corrupt bars refuse the session rather than being repaired.
        from app.marketdata.validation import inspect_series

        quality = inspect_series(bars, config.timeframe)
        if quality.invalid_ohlc:
            raise ValueError(
                f"{quality.invalid_ohlc} bars have impossible OHLC; refusing to replay "
                "corrupt data rather than silently repairing it"
            )

        strategy = self.registry.create(  # type: ignore[attr-defined]
            config.strategy_key, config.strategy_config
        )
        row = ReplaySessionRow(
            requested_by_user_id=user_id,
            universe=[config.symbol],
            timeframe=str(config.timeframe),
            from_time=bars[0].bar_time.replace(tzinfo=None),
            to_time=bars[-1].bar_time.replace(tzinfo=None),
            speed=1,
            status=str(ReplayState.created),
            # Everything needed to reproduce the session, including the
            # fingerprint two identical configurations share.
            summary={"config": config.describe(), "quality": quality.as_dict()},
        )
        db.add(row)
        await db.flush()
        session_id = row.id
        await db.commit()

        session = Session(
            id=session_id,
            user_id=user_id,
            config=config,
            clock=ReplayClock([b.bar_time for b in bars]),
            engine=ReplayEngine(config, strategy, bars, atr_for(bars)),
        )
        self.live[session_id] = session
        return session

    # ------------------------------------------------------------- controls

    def get(self, session_id: str, user_id: str) -> Session:
        session = self.live.get(session_id)
        # Someone else's session answers exactly as one that does not exist.
        if session is None or session.user_id != user_id:
            raise KeyError(session_id)
        return session

    async def start(self, session: Session) -> None:
        check_transition(session.state, ReplayState.running)
        await self._set_state(session, ReplayState.running)
        session._resume.set()
        if session._task is None or session._task.done():
            session._task = asyncio.create_task(self._drive(session), name=f"replay:{session.id}")

    async def pause(self, session: Session) -> None:
        check_transition(session.state, ReplayState.paused)
        # Clearing the gate stops the driver before its next bar. The cursor
        # does not move while paused, so resuming continues from exactly here.
        session._resume.clear()
        await self._set_state(session, ReplayState.paused)

    async def resume(self, session: Session) -> None:
        check_transition(session.state, ReplayState.running)
        await self._set_state(session, ReplayState.running)
        session._resume.set()

    async def stop(self, session: Session) -> None:
        check_transition(session.state, ReplayState.stopped)
        await self._set_state(session, ReplayState.stopped)
        session._resume.set()  # let the driver observe the state and exit
        if session._task is not None:
            session._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session._task

    async def step(self, session: Session) -> list[ReplayEvent]:
        """Advance exactly one bar. Legal from created or paused, never running.

        Stepping a running session would race the driver and reveal two bars
        for one request, so it is refused rather than serialised.
        """
        if session.state not in (ReplayState.created, ReplayState.paused):
            raise IllegalTransition(
                f"step is only allowed on a created or paused session, not a {session.state} one"
            )
        return await self._one_bar(session)

    async def set_speed(self, session: Session, speed: float) -> float:
        """Speed paces playback and nothing else.

        It cannot skip, reorder or merge an event, because `advance()` does not
        consult it. A test runs the same session at 1x and 100x and asserts the
        trades are identical.
        """
        value = session.clock.set_speed(check_speed(speed))
        async with self.sessions() as db:
            row = await db.get(ReplaySessionRow, session.id)
            if row is not None:
                row.speed = max(1, int(value))
                await db.commit()
        return value

    # -------------------------------------------------------------- driving

    async def _one_bar(self, session: Session) -> list[ReplayEvent]:
        index = session.clock.advance()
        if index is None:
            await self._finish(session)
            return []
        events = session.engine.step(index, session.clock.next_sequence)
        session.recent.extend(events)
        del session.recent[:-EVENT_BUFFER]
        await self._persist_cursor(session)
        await self._publish(session, events)
        if session.clock.exhausted:
            # Close anything still open, exactly as the batch simulator does.
            closing = session.engine.finalise(session.clock.next_sequence)
            if closing:
                events.extend(closing)
                session.recent.extend(closing)
                del session.recent[:-EVENT_BUFFER]
                await self._publish(session, closing)
            await self._finish(session)
        return events

    async def _drive(self, session: Session) -> None:
        try:
            while session.state in (ReplayState.running, ReplayState.paused):
                await session._resume.wait()
                if session.state is not ReplayState.running:
                    # Stopped or completed while we were waiting.
                    return
                await self._one_bar(session)
                if session.state in TERMINAL:
                    return
                # The ONLY place the machine's clock is consulted.
                await asyncio.sleep(session.clock.pacing_delay())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one session's failure is its own
            log.warning(
                "replay session failed",
                extra={
                    "event": "replay_failed",
                    "session_id": session.id,
                    "error": type(exc).__name__,
                },
            )
            session.error = f"{type(exc).__name__}: {exc}"[:300]
            await self._set_state(session, ReplayState.failed)

    async def _finish(self, session: Session) -> None:
        if session.state in TERMINAL:
            return
        await self._set_state(session, ReplayState.completed)
        async with self.sessions() as db:
            row = await db.get(ReplaySessionRow, session.id)
            if row is not None:
                summary = dict(row.summary or {})
                summary["metrics"] = session.engine.metrics()
                summary["trades"] = session.engine.portfolio.trades
                summary["equity_curve"] = session.engine.portfolio.equity_curve[-500:]
                row.summary = summary
                await db.commit()

    async def _set_state(self, session: Session, wanted: ReplayState) -> None:
        if session.state is not wanted:
            check_transition(session.state, wanted)
        session.state = wanted
        async with self.sessions() as db:
            row = await db.get(ReplaySessionRow, session.id)
            if row is not None:
                row.status = str(wanted)
                await db.commit()

    async def _persist_cursor(self, session: Session) -> None:
        now = session.clock.now()
        if now is None:
            return
        async with self.sessions() as db:
            row = await db.get(ReplaySessionRow, session.id)
            if row is not None:
                # So a session can be described after a restart, and so a
                # failure records how far it got.
                row.cursor_time = now.replace(tzinfo=None)
                await db.commit()

    async def _publish(self, session: Session, events: list[ReplayEvent]) -> None:
        if self.hub is None:
            return
        interesting = [
            e
            for e in events
            if e.kind in (EventKind.position_opened, EventKind.position_closed, EventKind.signal)
        ]
        for event in interesting:
            try:
                await self.hub.publish(  # type: ignore[attr-defined]
                    Event(
                        type=str(EventType.SYSTEM_ALERT),
                        payload={
                            "message": f"replay {event.kind}",
                            "session_id": session.id,
                            "execution_mode": "REPLAY",
                            **event.as_dict(),
                        },
                        # The user's own channel: a replay session belongs to
                        # one user and its events are theirs.
                        channel=str(Channel(Scope.user, session.user_id)),
                        source="replay",
                    )
                )
            except Exception:  # noqa: BLE001 - a lost event is not a lost trade
                log.warning(
                    "replay event not published",
                    extra={"event": "replay_publish_failed", "session_id": session.id},
                )

    async def shutdown(self) -> None:
        for session in list(self.live.values()):
            if session._task is not None and not session._task.done():
                session._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await session._task


def run_to_completion(config: BacktestConfig, strategy, bars) -> ReplayEngine:  # noqa: ANN001
    """Drive a replay synchronously, with no clock, task or database.

    This is what the consistency and determinism tests use: the same engine and
    the same order of operations as a live session, without the pacing. If this
    and a paced session disagreed, the difference would be the pacing -- which
    is exactly what must not matter.
    """
    clock = ReplayClock([b.bar_time for b in bars])
    engine = ReplayEngine(config, strategy, bars, atr_for(bars))
    while True:
        index = clock.advance()
        if index is None:
            break
        engine.step(index, clock.next_sequence)
    # The same close-at-the-end the batch simulator performs.
    engine.finalise(clock.next_sequence)
    return engine
