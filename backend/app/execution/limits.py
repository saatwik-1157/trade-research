"""The account's configured risk limits, resolved per pass. **L55.**

The execution pipeline evaluated every signal against `RiskLimits()` -- the
bare default, with **17 of its 20 limits unset**. Only three are set:
`one_position_per_symbol`, `require_stop_loss` and `require_market_open=False`.

So an operator who configured a daily loss cap, a maximum position count, an
exposure cap or a concentration cap got it enforced on the API order path --
which calls `RiskService.limits_for` -- and **not** on the automated one.

`app/main.py` has described the intended design since L22:

> The engine's limits are replaced per pass by the worker's caller once a bot
> configuration exists (L22). Until then it runs on the account's stored
> configuration through RiskService, and a bare engine here would be a second,
> looser copy -- so it is built from the same defaults the service loads.

The comment is exactly right about why it matters, and **the caller it
describes was never written**. This module is that caller.

**It can only ever tighten.** `RiskService.configuration` combines the
account's limits with the strategy's and the symbol's by taking the more
restrictive at every field, so a resolved set is never looser than the bare
default the pipeline used before. That is what makes wiring this safe to do
without a separate approval: it removes permissions, it never grants them.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.risk.engine import RiskLimits

log = logging.getLogger("app.execution.limits")


@runtime_checkable
class LimitsLoader(Protocol):
    """account + strategy + symbol -> the effective limits, or None."""

    async def __call__(
        self, *, account_id: str, strategy_id: str | None, symbol: str | None
    ) -> RiskLimits | None: ...


def loader_for(sessions: async_sessionmaker[AsyncSession], risk: object) -> LimitsLoader:
    """The loader the deployed pipeline uses.

    Takes the application's own `RiskService` rather than building a second
    one: the service holds the kill switches, the configuration cache and the
    reservation state, and a second copy would be a second answer to "what is
    this account allowed to do".
    """

    async def load(
        *, account_id: str, strategy_id: str | None, symbol: str | None
    ) -> RiskLimits | None:
        if not account_id:
            return None
        async with sessions() as db:
            resolved = await risk.limits_for(  # type: ignore[attr-defined]
                db,
                account_id=account_id,
                strategy_id=strategy_id,
                symbol=symbol,
            )
        limits = getattr(resolved, "limits", None)
        if limits is None:
            log.warning(
                "risk configuration resolved to nothing for an account; the "
                "pipeline's own engine applies",
                extra={"event": "risk_limits_empty", "account_id": account_id},
            )
        return limits

    return load


__all__ = ["LimitsLoader", "loader_for"]
