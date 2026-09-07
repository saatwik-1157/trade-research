"""One `OrderManager` per account, beside the adapter it drives.

This mirrors `app.brokers.BrokerRegistry` deliberately: an account has exactly
one adapter and exactly one order manager, and both are looked up the same
way. A manager handed out for the wrong account would submit against the wrong
venue, so `get` raises rather than falling back to "the other one" — the same
rule the broker registry already states.

**Empty at startup.** Constructing a manager requires an adapter, and
registering an adapter is an operator action rather than a side effect of the
API booting. So in the default deployment there is no manager for any account,
and every broker-bound route refuses with that reason — which is the honest
description of a platform whose `TRADING_MODE` is `paper` and whose ten
`LIVE_GATES` are all false.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.brokers.base import BrokerAdapter
from app.oms.order import ManagedOrder
from app.oms.service import OrderManager


class NoOrderManager(Exception):
    """No order manager is registered for this account. Never substituted."""


@dataclass
class OrderManagerRegistry:
    """Account id -> OrderManager, with a lock per account.

    The lock is what makes concurrent signals safe within this process: two
    coroutines creating an order for the same account would otherwise
    interleave between the idempotency check and the insert, and the unique
    constraint on `orders.intent_id` would turn the race into an error instead
    of preventing it. It is NOT a distributed lock, and `describe()` says so —
    a second backend process needs row-level locking, and this deployment does
    not have one.
    """

    managers: dict[str, OrderManager] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def register(
        self,
        account_id: str,
        adapter: BrokerAdapter,
        *,
        mode: str,
        broker: str | None = None,
        publish: Callable[[str, dict[str, object], ManagedOrder], Awaitable[None]] | None = None,
    ) -> OrderManager:
        if not account_id:
            raise ValueError("an order manager must be registered against an account id")
        if account_id in self.managers:
            raise ValueError(f"account {account_id} already has an order manager")
        manager = OrderManager(adapter, mode=mode, broker=broker, publish=publish)
        self.managers[account_id] = manager
        self._locks[account_id] = asyncio.Lock()
        return manager

    def get(self, account_id: str) -> OrderManager:
        try:
            return self.managers[account_id]
        except KeyError as exc:
            raise NoOrderManager(
                f"no order manager is registered for account {account_id}. An adapter is "
                "registered by an operator, not by the API booting, so this is the "
                "expected state of a deployment that has not been pointed at a venue"
            ) from exc

    def lock(self, account_id: str) -> asyncio.Lock:
        """Serialise order creation for one account within this process."""
        return self._locks.setdefault(account_id, asyncio.Lock())

    def unresolved(self) -> dict[str, list[str]]:
        """Orders per account whose outcome the venue has not settled.

        This is what a health check reads. An account with anything here has a
        venue that may be holding an order the platform cannot see, and
        nothing may be sent for those intents until each is reconciled.
        """
        return {
            account: [o.id for o in manager.orders.values() if o.needs_reconciliation]
            for account, manager in self.managers.items()
            if any(o.needs_reconciliation for o in manager.orders.values())
        }

    def describe(self) -> dict[str, object]:
        return {
            "accounts": sorted(self.managers),
            "unresolved": self.unresolved(),
            "concurrency": (
                "One asyncio lock per account serialises order creation within this "
                "process. It is not a distributed lock; a second backend process would "
                "need row-level locking, and this deployment does not have one. The "
                "unique constraint on orders.intent_id is the backstop either way."
            ),
            "registration": (
                "Empty until an operator registers an adapter. A route that needs a "
                "manager refuses rather than choosing one."
            ),
        }
