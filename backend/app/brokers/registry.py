"""One adapter per trading account, kept apart from every other account.

The shape the platform commits to:

    User -> Trading Account -> Broker Connection -> BrokerAdapter

Not a global singleton. A module-level "the MT5 connection" is what makes two
accounts share credentials, positions and risk state by accident, and it is
easier to avoid now than to unpick after four levels have imported it.

**Isolation is the whole job.** Look-up is always by account id, and an
adapter is never handed out without one. There is no "current" adapter and no
default, because a default is what a bug reaches for.

Credentials are not stored here and are not stored anywhere yet: the platform
holds none, and encrypted broker credentials are L39. An adapter is
constructed with a terminal path at most. That is why `describe()` can be
returned from an API without filtering -- there is nothing in it to filter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.brokers.base import BrokerAdapter, BrokerHealth, ConnectionState

log = logging.getLogger("app.brokers.registry")


class UnknownAccount(Exception):
    """No adapter for that account. Never resolved to another account's."""


@dataclass
class BrokerRegistry:
    """Account id -> adapter, with a lock per account.

    The lock matters because MT5's Python API is a single global connection per
    process: two coroutines interleaving `connect()` for different accounts
    would leave the terminal pointed at whichever finished last, and every read
    after that would silently belong to the wrong account. Serialising per
    account does not make that safe on its own -- running two MT5 accounts in
    one process is not supported and `describe()` says so -- but it does stop
    the platform from corrupting a single account's session with itself.
    """

    adapters: dict[str, BrokerAdapter] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def register(self, account_id: str, adapter: BrokerAdapter) -> BrokerAdapter:
        if not account_id:
            raise ValueError("an adapter must be registered against an account id")
        if account_id in self.adapters:
            raise ValueError(f"account {account_id} already has an adapter registered")
        self.adapters[account_id] = adapter
        self._locks[account_id] = asyncio.Lock()
        return adapter

    def get(self, account_id: str) -> BrokerAdapter:
        try:
            return self.adapters[account_id]
        except KeyError as exc:
            # Never falls back to "the other one". An adapter handed out for
            # the wrong account reads the wrong positions.
            raise UnknownAccount(f"no broker adapter registered for account {account_id}") from exc

    def lock(self, account_id: str) -> asyncio.Lock:
        self.get(account_id)
        return self._locks[account_id]

    def remove(self, account_id: str) -> None:
        self.adapters.pop(account_id, None)
        self._locks.pop(account_id, None)

    async def health(self) -> dict[str, BrokerHealth]:
        """Health for every registered account. Never raises."""
        return {account: await adapter.health() for account, adapter in self.adapters.items()}

    async def disconnect_all(self) -> None:
        for account, adapter in list(self.adapters.items()):
            try:
                await adapter.disconnect()
            except Exception:  # noqa: BLE001 - shutdown continues past one bad adapter
                log.warning(
                    "adapter did not disconnect cleanly",
                    extra={"event": "broker_disconnect_failed", "account_id": account},
                )

    def describe(self) -> list[dict[str, object]]:
        """What is registered. Carries no credential, because none is held."""
        return [
            {
                "account_id": account,
                "adapter": adapter.name,
                "mode": adapter.mode,
                "state": str(adapter.state),
                "reconnects": adapter.reconnects,
            }
            for account, adapter in self.adapters.items()
        ]

    @property
    def connected(self) -> list[str]:
        return [
            account
            for account, adapter in self.adapters.items()
            if adapter.state is ConnectionState.connected
        ]
