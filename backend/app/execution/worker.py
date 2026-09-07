"""The execution worker: signals become orders with no browser involved.

This is a `app.workers.Worker` — the same supervised loop with a heartbeat
that every other background job in this platform uses. It is not a second
worker system, a second scheduler or a second queue.

**Why a worker rather than the webhook request.** Brief §20 says not to run
execution inside the HTTP request when the architecture supports asynchronous
processing. It does, and there is a harder reason than latency: an alert whose
execution happens inside the request is an alert whose execution is lost if
the connection drops after the gateway wrote the row. The gateway's job ends
at "this signal is recorded"; the worker's job starts there, and the `signals`
table is the handover.

**A signal is claimed before it is processed.** `status` moves `new ->
executed` (or a refusal state) in the same transaction that reads it, so two
workers cannot both pick it up. The in-process `seen` set and the OMS's
`intent_id` uniqueness are the second and third guards; this is the first, and
it is the only one that survives two processes.

**Nothing is replayed on restart.** The worker reads signals in `new` only. A
signal already carried to a decision has left that state, so a restart resumes
rather than re-runs — the failure §26 warns about.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.outcome import NO_ORDER, Outcome
from app.execution.pipeline import ExecutionPipeline, ExecutionResult, IncomingSignal
from app.models.signals import Signal
from app.workers.base import Worker

log = logging.getLogger("app.execution.worker")

#: How a pipeline outcome maps onto the `signals.status` CHECK constraint,
#: which is L05's and is not widened here. `executed` means "carried to a
#: decision", not "filled": a vetoed signal is finished with, and leaving it
#: `new` would mean re-running the same veto every pass forever.
STATUS_FOR: dict[Outcome, str] = {
    Outcome.filled: "executed",
    Outcome.partially_filled: "executed",
    Outcome.order_submitted: "executed",
    Outcome.duplicate_signal: "executed",
    Outcome.risk_vetoed: "vetoed",
    Outcome.risk_halted: "vetoed",
    Outcome.kill_switch: "vetoed",
    Outcome.ai_rejected: "vetoed",
    Outcome.sizing_refused: "vetoed",
    Outcome.strategy_disabled: "vetoed",
    Outcome.signal_stale: "expired",
    Outcome.signal_invalid: "vetoed",
    Outcome.strategy_unknown: "vetoed",
    Outcome.source_unauthorized: "vetoed",
    Outcome.execution_rejected: "executed",
}


def status_for(outcome: Outcome) -> str | None:
    """The row status for an outcome, or None to leave the signal alone.

    Returning None is the important case and it covers exactly two things: an
    order whose fate the venue has not settled (`execution_unknown`), and a
    fault on our side (`no_venue`, `spec_incomplete`, `strategy_error`). Both
    are conditions that can clear, and a signal parked in `new` is one a later
    pass or a reconciliation can still act on. Marking either as finished
    would throw away a signal because a dependency was briefly down.

    **L38 added a third of the same kind.** `safe_mode` is a latched condition
    somebody will resolve, so a signal refused by it is parked rather than
    vetoed: marking it `vetoed` would discard it for a reason that goes away.
    It is not consumed into `pipeline.seen` either, so a later pass reoffers
    it -- and if safe mode outlasts the signal's own validity window, the
    staleness check expires it, which is the honest outcome and is why
    reoffering is safe.
    """
    return STATUS_FOR.get(outcome)


class ExecutionWorker(Worker):
    """Drains `signals` in `new` through the pipeline, forever."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        pipeline: ExecutionPipeline,
        *,
        to_signal: Callable[[Signal], IncomingSignal | None],
        interval_seconds: float = 2.0,
        batch: int = 20,
        name: str = "execution",
    ) -> None:
        super().__init__(name=name, interval_seconds=interval_seconds)
        self.sessions = sessions
        self.pipeline = pipeline
        self.to_signal = to_signal
        self.batch = batch
        self.results: list[ExecutionResult] = []

    async def tick(self) -> None:
        async with self.sessions() as db:
            rows = await self._claim(db)
            await db.commit()

        for row, incoming in rows:
            result = await self.pipeline.process(incoming)
            self.results.append(result)
            del self.results[:-200]
            await self._settle(row.id, result)

    async def _claim(self, db: AsyncSession) -> Sequence[tuple[Signal, IncomingSignal]]:
        """Take a batch of `new` signals and mark them claimed.

        The claim and the read are the same transaction, so a second worker
        sees them already moved rather than picking them up again.
        """
        rows = (
            await db.scalars(
                select(Signal)
                .where(Signal.status == "new")
                .order_by(Signal.signal_time)
                .limit(self.batch)
                .with_for_update(skip_locked=True)
            )
        ).all()
        claimed: list[tuple[Signal, IncomingSignal]] = []
        for row in rows:
            incoming = self.to_signal(row)
            if incoming is None:
                # Not translatable into something the pipeline can act on --
                # an unmapped symbol, a missing account. Recorded as vetoed
                # with the reason rather than left to be retried forever.
                row.status = "vetoed"
                log.warning(
                    "a signal could not be prepared for execution",
                    extra={"event": "signal_not_executable", "signal_id": row.id},
                )
                continue
            claimed.append((row, incoming))
        return claimed

    async def _settle(self, signal_id: str, result: ExecutionResult) -> None:
        status = status_for(result.outcome)
        if status is None:
            # Deliberately left in `new`. See `status_for`.
            log.warning(
                "a signal is parked rather than finished; a later pass may act on it",
                extra={
                    "event": "signal_parked",
                    "signal_id": signal_id,
                    "execution_id": result.execution_id,
                    "outcome": str(result.outcome),
                },
            )
            return
        async with self.sessions() as db:
            row = await db.get(Signal, signal_id)
            if row is None:  # pragma: no cover - it was read a moment ago
                return
            row.status = status
            row.meta = {
                **(row.meta or {}),
                # The correlation id, so "why did this trade happen" has one
                # string to follow from the alert to the order.
                "execution_id": result.execution_id,
                "outcome": str(result.outcome),
                "detail": result.detail[:300],
                "settled_at": datetime.now(UTC).isoformat(),
            }
            await db.commit()

    def report(self) -> dict[str, object]:
        """The pipeline's counters plus this worker's own.

        Named `report` rather than `status` because `Worker.status` is already
        the heartbeat property every supervised loop exposes, and shadowing it
        would make this worker lie to the registry that watches it.
        """
        recent = self.results[-50:]
        return {
            **self.pipeline.status(),
            "worker": self.name,
            "recent": [r.as_dict() for r in recent],
            "recent_refusals": sum(1 for r in recent if r.outcome in NO_ORDER),
            "browser_independent": (
                "This is a supervised background worker. Closing a browser stops "
                "nothing; the API is a control plane, not the execution path."
            ),
        }
