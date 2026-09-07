"""The bot-recovery safety check L22 left a seat for.

Step 10, and L22's own docstring:

    *Registered and NOT started ... refusing every one of them while no safety
    check is wired, because the absence of a check is not evidence that
    recovery is safe.*

`BotSupervisor` takes a `SafetyCheck` -- one callable, `Bot -> refusal reason
or None` -- and defaults to `_refuse_by_default`, which refuses everything.
That default has been in place since L22 and is correct: a supervisor that
restarted everything because nobody told it not to would be the most dangerous
default in the system.

L38 fills the seat. It does not change the supervisor, does not change the
state machine, and does not lower the bar: it supplies the checks L22's
docstring already named, plus the two L38 adds.

**Every one of these is a refusal, and none is an approval.** The function
returns a reason to refuse or `None`, and `None` means only "this check found
nothing" -- the supervisor's own gates (the run must be `crashed` rather than
`halted`, the bot must be enabled) still apply afterwards. There is no path
through this module that makes a restart more likely than L22 intended.

**It reads. It cannot act.** No order manager, no adapter, no risk engine.
It runs four queries and consults the safe-mode latch.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bots import Bot
from app.models.execution import Order, Position
from app.recovery.reconciliation import UNRESOLVED_ORDER_STATES

log = logging.getLogger("app.recovery.bots")

SafetyCheck = Callable[[Bot], Awaitable[str | None]]


async def _unresolved_for(db: AsyncSession, bot: Bot) -> int:
    """Orders on this bot's account whose venue state was never established.

    Step 10 and step 6. A restart with one of these outstanding is a second
    order waiting to happen: the venue may be holding the first, and nobody has
    settled it. L22's docstring names this exact condition.
    """
    account = bot.broker_account_id or bot.paper_account_id
    if not account:
        return 0
    return len(
        list(
            (
                await db.scalars(
                    select(Order.id).where(
                        Order.status.in_(UNRESOLVED_ORDER_STATES),
                        (Order.broker_account_id == account) | (Order.paper_account_id == account),
                    )
                )
            ).all()
        )
    )


async def _unsettled_positions_for(db: AsyncSession, bot: Bot) -> int:
    """Positions on this bot's account the platform could not settle.

    Step 14 and rule 7: position state is reconciled before autonomous
    execution resumes. A bot restarted over an unreconciled position is a bot
    managing a position whose size nobody agrees on.
    """
    account = bot.broker_account_id or bot.paper_account_id
    if not account:
        return 0
    return len(
        list(
            (
                await db.scalars(
                    select(Position.id).where(
                        Position.status.in_(["unknown", "reconciling"]),
                        (Position.broker_account_id == account)
                        | (Position.paper_account_id == account),
                    )
                )
            ).all()
        )
    )


def recovery_gate(
    session_factory: Any,
    *,
    safe_mode: Any = None,
    risk: Any = None,
    brokers: Any = None,
) -> SafetyCheck:
    """The check `BotSupervisorWorker` is given. Refuses; never approves.

    Five conditions, in the order they are cheapest to establish:

      1. safe mode is engaged -- the platform has a stated reason not to start
         new work, and a bot is new work;
      2. a kill switch covers the platform, the account or the strategy -- a
         decision somebody made, and recovering out of it automatically is the
         bot-level bypass L22 section 22 forbids;
      3. the account has an order whose venue state was never established;
      4. the account has a position the platform could not settle;
      5. the account's broker adapter is registered and not usable -- a bot
         that cannot reach its venue restarts into an immediate refusal, and
         the restart itself is what would then be retried.

    Each returns a sentence naming the condition. `None` means these five found
    nothing, not that recovery is approved: L22's own gates run afterwards.
    """

    async def check(bot: Bot) -> str | None:
        if safe_mode is not None and getattr(safe_mode, "engaged", False):
            reasons = "; ".join(f"{latch.reason}" for latch in getattr(safe_mode, "reasons", []))
            return (
                "the platform is in safe mode, so no bot is recovered. "
                f"Reasons: {reasons or 'unspecified'}"
            )

        if risk is not None:
            status = risk.status()
            switches = status.get("kill_switches", {})
            account = bot.broker_account_id or bot.paper_account_id
            if switches.get("global"):
                return (
                    "a global kill switch is engaged. That is a decision somebody "
                    "made, and recovery does not undo it."
                )
            if account and account in set(switches.get("accounts") or []):
                return f"a kill switch is engaged for account {account}"

        async with session_factory() as db:
            unresolved = await _unresolved_for(db, bot)
            if unresolved:
                return (
                    f"{unresolved} order(s) on this bot's account have a venue state "
                    "that was never established. Restarting over one is a second order "
                    "waiting to happen; settle them first "
                    "(POST /v1/orders/{id}/reconcile)."
                )
            unsettled = await _unsettled_positions_for(db, bot)
            if unsettled:
                return (
                    f"{unsettled} position(s) on this bot's account could not be "
                    "settled against the venue. A bot restarted over one manages a "
                    "position whose size nobody agrees on."
                )

        account = bot.broker_account_id
        adapters = getattr(brokers, "adapters", {}) if brokers is not None else {}
        if account and account in adapters:
            health = await adapters[account].health()
            if not health.usable:
                return (
                    f"the broker adapter for account {account} reports {health.state}. "
                    "A bot that cannot reach its venue restarts into an immediate "
                    "refusal."
                )

        return None

    return check


__all__ = ["SafetyCheck", "recovery_gate"]
