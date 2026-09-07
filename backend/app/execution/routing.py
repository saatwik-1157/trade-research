"""Which bot executes an alert, and therefore on which account. **L45 F-1.**

An alert names a strategy. It does not name an account, and it must not: the
account is a routing decision the platform owns, and a payload that could
select an account could select somebody else's.

**This is the join L40 and L41 never made.** They verified the webhook half and
the pipeline half separately; nothing joined them, and the gap was that the
gateway wrote no `account_id` into a signal's `meta`, so
`app/main.py::_to_incoming_signal` returned None and the worker retired every
recorded alert as `signal_not_executable`. All 118 signals on the deployed
database sit at `status='new'` for that reason.

**The bot is the join, and it already exists.** `bots` carries
`strategy_version_id`, `paper_account_id`/`broker_account_id`, `mode` and
`max_risk_per_trade` -- the account and the per-trade risk budget, on the row,
already modelled. Nothing new is introduced here; this module reads what L22
built and states the answer.

**Ambiguity is refused, never resolved.** Two enabled bots on one strategy
version is not "pick the first": it is a configuration nobody has finished, and
choosing for them would send an order to an account nobody selected. The same
rule `OrderManagerRegistry.get` already states for adapters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bots import Bot

log = logging.getLogger("app.execution.routing")


@dataclass(frozen=True)
class Route:
    """The bot that will execute this signal, and what it permits."""

    bot_id: str
    account_id: str
    #: The bot's per-trade risk budget, or None when it has not set one. None
    #: means NOT SET rather than unlimited -- the same rule `BotLimits` states
    #: -- and the pipeline's own configuration applies instead.
    risk_amount: Decimal | None
    #: Whether this bot's signals may take their bracket from the alert.
    #: **False unless an operator turned it on for this bot.** See
    #: `app/models/bots.py` and migration 0026 for why it is a decision rather
    #: than a default.
    use_alert_bracket: bool = False


@dataclass(frozen=True)
class NoRoute:
    """No bot executes this signal, and why. Recorded, never guessed around."""

    reason: str


async def route_for(
    db: AsyncSession, *, strategy_version_id: str | None, mode: str
) -> Route | NoRoute:
    """The one enabled bot for this strategy version in this mode.

    Returns `NoRoute` with a stated reason rather than raising: an alert that
    cannot be routed is a recorded signal that will not execute, which is a
    normal operating state for a platform whose bots are switched off -- not an
    error on the intake path.
    """
    if not strategy_version_id:
        return NoRoute(
            "the alert names no strategy, so no bot claims it. An external alert is "
            "recorded and not executed; that is the documented 'external alert' case"
        )

    bots = (
        await db.scalars(
            select(Bot).where(
                Bot.strategy_version_id == strategy_version_id,
                Bot.mode == mode,
                Bot.is_enabled.is_(True),
                Bot.is_disabled.is_(False),
            )
        )
    ).all()

    if not bots:
        return NoRoute(
            f"no enabled bot runs strategy version {strategy_version_id} in {mode}; "
            "the alert is recorded and nothing executes it"
        )
    if len(bots) > 1:
        # Refused rather than resolved. Choosing would send an order to an
        # account nobody selected.
        names = ", ".join(sorted(b.id for b in bots))
        log.warning(
            "an alert matches more than one enabled bot; it will not be executed",
            extra={
                "event": "signal_route_ambiguous",
                "strategy_version_id": strategy_version_id,
                "mode": mode,
                "bots": names,
            },
        )
        return NoRoute(
            f"{len(bots)} enabled bots run strategy version {strategy_version_id} in "
            f"{mode} ({names}); refusing to choose one, because the account an order "
            "lands on would be arbitrary"
        )

    bot = bots[0]
    account_id = bot.paper_account_id or bot.broker_account_id
    if not account_id:
        return NoRoute(
            f"bot {bot.id} runs this strategy but names no account, so there is no "
            "order manager to route the signal through"
        )
    return Route(
        bot_id=bot.id,
        account_id=account_id,
        risk_amount=bot.max_risk_per_trade,
        use_alert_bracket=bool(getattr(bot, "use_alert_bracket", False)),
    )


__all__ = ["NoRoute", "Route", "route_for"]
