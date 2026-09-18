"""The OMS reconcile sweep: an unknown order is settled without a human.

A `app.workers.Worker` -- the same supervised loop every other background job
uses. It is not a second reconciler: it calls `OrderManager.reconcile`, which
L19 built and which `POST /v1/orders/{id}/reconcile` had been the only
production caller of. An order parked `unknown` at 02:00 blocked its intent,
its account's bot recovery and safe mode's reason list until somebody woke up
and posted.

**It asks; it never sends.** Nothing here calls `submit`, `close`, `cancel` or
`modify`, and `reconcile` itself cannot: it reads `get_orders` and
`get_positions` and records what it found. A test parses this file to keep
that true.

**A venue it cannot read settles nothing.** `reconcile` raises
`ReconciliationRequired` when the adapter fails, and the order stays `unknown`,
which is the honest state. This worker catches that per order, counts it and
moves on -- one unreachable account must not stop the sweep reaching the
others, and an unreachable account must not become a conclusion.

**It takes the account's lock.** `execution/pipeline.py` and the manual order
route both hold `registry.lock(account_id)` across create, persist and submit.
Without it this sweep could see an order in `submitting` while `place_order`
was still in flight, conclude "the venue holds nothing", and write `failed` --
which is the one state a fresh order for the same intent may follow. That is
the double-send the whole `unknown` rule exists to prevent, reached by the
thing meant to prevent it. It is the registry's existing lock, not a new one.

**It sweeps what is in memory.** `OrderManagerRegistry.unresolved()` reads
`manager.orders`, so an order parked by THIS process is covered and an order
left by a previous one is not: `OrderManager.resume` and
`OrderRepository.load_unresolved` still have no production caller. That is a
separate gap and this worker does not paper over it -- `report()` says so.
"""

from __future__ import annotations

import logging
from typing import Any

from app.execution.store import OrderNotRecorded, OrderStore
from app.oms.registry import OrderManagerRegistry
from app.oms.service import OrderManager, ReconciliationRequired
from app.workers.base import Worker

log = logging.getLogger("app.oms.worker")

# Outcomes of one `_settle`, counted per pass. Strings rather than an enum
# because they are only ever summed and reported.
SETTLED = "settled"
UNREADABLE = "unreadable"
UNRECORDED = "unrecorded"
FAULTED = "faulted"
GONE = "gone"


class OmsReconcileWorker(Worker):
    """Settles unresolved orders on an interval, forever."""

    name = "oms_reconcile"

    def __init__(
        self,
        managers: OrderManagerRegistry,
        store: OrderStore,
        *,
        interval_seconds: float = 60.0,
        max_per_pass: int = 10,
        name: str = "oms_reconcile",
    ) -> None:
        super().__init__(name=name, interval_seconds=interval_seconds)
        self.managers = managers
        self.store = store
        self.max_per_pass = max_per_pass
        self.last_pass: dict[str, Any] = {}

    async def tick(self) -> None:
        counts = {SETTLED: 0, UNREADABLE: 0, UNRECORDED: 0, FAULTED: 0, GONE: 0}
        deferred = 0
        # A snapshot: `reconcile` mutates the dict this reads from.
        for account, order_ids in self.managers.unresolved().items():
            manager = self.managers.managers.get(account)
            if manager is None:  # deregistered between the read and here
                continue
            # A bound, so a backlog cannot turn one tick into a hundred venue
            # round trips; the remainder is taken next pass and counted here
            # so a pass that left work behind does not read as a clean one.
            deferred += max(0, len(order_ids) - self.max_per_pass)
            for order_id in order_ids[: self.max_per_pass]:
                try:
                    outcome = await self._settle(manager, account, order_id)
                except Exception:  # noqa: BLE001 - one bad order is not the book
                    # `Worker.run` already survives a failing tick, but a
                    # single order that raises something unexpected would
                    # abort every pass at the same place, so every OTHER
                    # unresolved order would stay unresolved forever. It is
                    # counted rather than swallowed: `faulted` in the report
                    # is the difference between "nothing to settle" and
                    # "something here cannot be settled".
                    counts[FAULTED] += 1
                    log.exception(
                        "an order could not be swept; the sweep continues",
                        extra={
                            "event": "oms_sweep_order_faulted",
                            "order_id": order_id,
                            "account": account,
                        },
                    )
                    continue
                counts[outcome] += 1
        self.last_pass = {**counts, "deferred": deferred}
        if counts[UNREADABLE] or counts[UNRECORDED] or counts[FAULTED] or deferred:
            log.warning(
                "the reconcile sweep left orders unsettled",
                extra={"event": "oms_sweep_incomplete", **self.last_pass},
            )

    async def _settle(self, manager: OrderManager, account: str, order_id: str) -> str:
        async with self.managers.lock(account):
            order = manager.orders.get(order_id)
            # Re-read under the lock: a submission that completed between the
            # snapshot and here is settled already and must not be touched.
            if order is None or not order.needs_reconciliation:
                return GONE
            try:
                await manager.reconcile(order)
            except ReconciliationRequired as exc:
                # The venue could not be read. Nothing is concluded and the
                # order stays `unknown`, which is the state L19 chose for
                # exactly this. Warning, not error: an unreachable venue at
                # 02:00 is a condition, not a fault in this loop.
                log.warning(
                    "the sweep could not read the venue; the order stays unknown",
                    extra={
                        "event": "oms_sweep_venue_unreadable",
                        "order_id": order_id,
                        "account": account,
                        "reason": str(exc)[:200],
                    },
                )
                return UNREADABLE
            try:
                # The SAME store the deployed pipeline writes through, so
                # there is one answer to "what makes an order durable".
                await self.store.record(order)
            except OrderNotRecorded:
                # `record` refuses when the symbol will not resolve. On the
                # pre-send path that refusal is the point; here the order
                # already exists at a venue, so a missing row is reported
                # rather than raised -- the reasoning `execution/store.py`
                # spells out beside the raise.
                log.exception(
                    "a reconciled order could not be written down",
                    extra={"event": "oms_sweep_not_recorded", "order_id": order_id},
                )
                return UNRECORDED
        # Events AFTER the record is durable and OUTSIDE the lock, the order
        # the manual route uses: an event can then never describe something
        # that was not saved, and a bus publish never holds a per-account
        # lock.
        await manager.flush_events()
        return SETTLED

    def report(self) -> dict[str, object]:
        """The last pass plus this worker's own heartbeat.

        Named `report` rather than `status` because `Worker.status` is the
        heartbeat the registry reads, and shadowing it would make this worker
        lie to the thing that watches it.
        """
        return {
            "worker": self.name,
            "supervisor": self.status.as_dict(),
            "last_pass": dict(self.last_pass),
            "unresolved": self.managers.unresolved(),
            "authority": (
                "This worker asks the venue what it holds and records the answer. It "
                "submits, closes, cancels and modifies nothing, and it releases no "
                "safe-mode latch."
            ),
            "scope": (
                "In-memory orders only. `OrderManager.resume` and "
                "`OrderRepository.load_unresolved` still have no production caller, so "
                "an order left unresolved by a PREVIOUS process is not swept here."
            ),
        }


__all__ = ["OmsReconcileWorker"]
