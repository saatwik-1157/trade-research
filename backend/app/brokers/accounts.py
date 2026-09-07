"""The `broker_accounts` row that lets a position name where it is held.

`app/api/v1/accounts.py` has said since L05 that creating one waits for L10,
and said why: *"a broker account that cannot be connected is a row that
promises something the platform cannot do."* That condition is now met — a
venue registration connects the adapter, runs the demo fence and reads the
account back — so the row is written at exactly the moment the promise becomes
true, and never before.

**Why it has to exist at all.** `positions.broker_account_id` is the only
column that can say which venue a position is held at. Without a row to point
at, a recorded broker position is an orphan: the close route cannot work out
which adapter to ask for a price, and reconciliation cannot be scoped to one
account. That is not a theoretical gap — it is what refused the first attempt
to close a real position on 2026-09-07.

**It holds no credential.** `login`, `server`, `currency` and `account_mode` are
what the terminal reported about an account it is already logged in to; the
password is not here and is not anywhere. `BrokerAccountOut` deliberately omits
`login` when this is read back, which is why nothing here changes that shape.

**Identity is the account, not the label.** A row is matched on
`(user_id, broker, login, server)` — what uniquely names an account at a venue.
`name` is the registry key an operator chose, and it is refreshed rather than
matched on, so re-registering the same terminal under a different label updates
one row instead of growing a second.

**Paper has no broker account, deliberately.** `account_mode` is constrained to
`demo` or `live`; a simulator is not an account at a venue and giving it a row
here would put a fictional account in the same table as a real one.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import BrokerAccount

#: Modes that name a real venue. Matches `account_mode IN ('demo','live')` in
#: the schema, and `app.positions.ingest.BROKER_MODES` in intent.
ACCOUNT_MODES = frozenset({"demo", "live"})

#: `broker IN ('mt5','fake')` in the schema. The adapter key an operator
#: registers is mapped to one of those; an unknown key writes no row rather
#: than guessing at a value the constraint would reject anyway.
_BROKER_FOR_ADAPTER = {"mt5_demo": "mt5", "simulator": "fake"}


async def ensure_broker_account(
    db: AsyncSession,
    *,
    user_id: str,
    name: str,
    adapter_key: str,
    mode: str,
    account: Any,
) -> BrokerAccount | None:
    """Create or refresh the row for a venue that has just been connected.

    `account` is the `Account` the adapter returned from `connect()` -- what the
    venue said, not what anybody configured. Returns None for a mode that is not
    a venue, which is how the simulator is skipped.

    Does not commit. The caller owns the transaction, so the row and the audit
    record of the registration that created it land together.
    """
    if mode not in ACCOUNT_MODES:
        return None
    broker = _BROKER_FOR_ADAPTER.get(adapter_key)
    if broker is None:
        return None

    login = str(getattr(account, "login", "") or "") or None
    server = str(getattr(account, "server", "") or "") or None

    existing = await db.scalar(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user_id,
            BrokerAccount.broker == broker,
            BrokerAccount.login == login,
            BrokerAccount.server == server,
        )
    )
    if existing is not None:
        existing.name = name
        existing.account_mode = mode
        existing.currency = str(getattr(account, "currency", "") or "") or None
        existing.is_active = True
        return existing

    row = BrokerAccount(
        user_id=user_id,
        name=name,
        broker=broker,
        account_mode=mode,
        login=login,
        server=server,
        currency=str(getattr(account, "currency", "") or "") or None,
        is_active=True,
    )
    db.add(row)
    return row


async def account_for(db: AsyncSession, *, name: str, mode: str) -> BrokerAccount | None:
    """The row registered under one registry key, or None.

    `name` is what the operator passed as `account_id` when registering the
    venue, which is also the key `BrokerRegistry` and `OrderManagerRegistry`
    are keyed by. That shared key is the whole link between an in-process
    adapter and a durable row.
    """
    if mode not in ACCOUNT_MODES:
        return None
    return await db.scalar(
        select(BrokerAccount).where(
            BrokerAccount.name == name,
            BrokerAccount.account_mode == mode,
            BrokerAccount.is_active.is_(True),
        )
    )
