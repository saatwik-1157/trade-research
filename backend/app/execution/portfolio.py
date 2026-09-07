"""The portfolio the RiskEngine is evaluated against. **The L53 fix.**

`PortfolioState` has twenty-two fields and the RiskEngine's docstring is
explicit that "a None that a limit needs produces a veto rather than an
assumption". Both execution paths passed exactly one of them:

    PortfolioState(equity=self.equity)      # app/execution/pipeline.py
    PortfolioState(equity=body.equity)      # app/api/v1/orders.py

So every portfolio-level limit was unenforceable, whatever it was configured
to. Not because the numbers were wrong — **because the engine was never told
them.**

`PortfolioService.to_risk_state()` already returns exactly the fields
`PortfolioState` reads, and its only caller was a route that DISPLAYS it. The
platform computed portfolio risk, showed it on a dashboard, and never enforced
it. This module is the wire between the two.

**Reproduced on the running deployment before the fix.** It held an open
EURUSD short with `one_position_per_symbol=True`, and the pipeline created and
approved a EURUSD buy:

    PortfolioState(equity=None)                   -> approve
    PortfolioState(open_symbols={'EURUSD'})       -> veto: EURUSD already open

Same engine, same order, same limits. The only difference is whether the engine
was told what the account already held.

**An unreadable portfolio makes trading MORE conservative, never less.** Every
field stays None on failure, and a None a limit needs is a veto. That is why
this module never invents a figure and never falls back to a cached one: a
stale portfolio that permits an order is worse than no order.
"""

from __future__ import annotations

import logging
from dataclasses import fields
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.risk.engine import PortfolioState

log = logging.getLogger("app.execution.portfolio")

#: The keys `to_risk_state()` offers that `PortfolioState` does not accept.
#: Filtered rather than assumed away: `PortfolioState(**state)` would raise on
#: them, and a snapshot provider that raised inside the risk stage would turn a
#: portfolio problem into a refused signal for the wrong reason.
_ACCEPTED = {f.name for f in fields(PortfolioState)}


@runtime_checkable
class PortfolioSnapshot(Protocol):
    """Account id and mode -> the fields the RiskEngine reads."""

    async def state_for(self, account_id: str, *, mode: str) -> dict[str, Any]: ...


def to_portfolio_state(raw: dict[str, Any]) -> PortfolioState:
    """Build the engine's input from a `to_risk_state()` mapping.

    Unknown keys are dropped rather than passed through. `to_risk_state()`
    deliberately returns more than the engine takes -- `gross_exposure`,
    `not_supplied` and an explanatory `note` -- because it is also read by a
    human-facing route.
    """
    return PortfolioState(**{k: v for k, v in raw.items() if k in _ACCEPTED})


class DatabasePortfolio:
    """A snapshot read from the tables that already own these figures.

    No new state and no cache. The portfolio engine aggregates; it does not
    own -- each figure names the system to go and ask -- and a cache here would
    be a second answer to "what is open" that could disagree with the first at
    exactly the wrong moment.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def state_for(self, account_id: str, *, mode: str) -> dict[str, Any]:
        """The account's current portfolio, or `{}` when it cannot be read.

        `{}` becomes an all-None `PortfolioState`, which is the conservative
        direction: a limit that needs a figure it does not have vetoes.
        """
        from app.models.accounts import PaperAccount
        from app.portfolio import service as portfolio_service

        if mode != "paper":
            # A broker account's balance and equity are the BROKER's figures,
            # and `from_broker_account` takes them from a live `get_account()`
            # rather than from a row. Calling a venue from inside the risk gate
            # would put a network round-trip between the decision and the
            # order, so this returns empty instead -- every field None, which
            # is the conservative direction: a limit that needs a figure it
            # does not have vetoes.
            #
            # Whether the risk stage may query the venue is a design decision
            # nobody has taken, and it is recorded rather than assumed.
            log.info(
                "no portfolio snapshot for a non-paper account; risk sees an empty "
                "portfolio, which vetoes any limit that needs one",
                extra={
                    "event": "portfolio_snapshot_not_paper",
                    "account_id": account_id,
                    "mode": mode,
                },
            )
            return {}

        try:
            async with self.sessions() as db:
                row = await db.get(PaperAccount, account_id)
                if row is None:
                    log.warning(
                        "no account row for a portfolio snapshot; risk will see an "
                        "empty portfolio, which vetoes any limit that needs one",
                        extra={
                            "event": "portfolio_snapshot_no_account",
                            "account_id": account_id,
                            "mode": mode,
                        },
                    )
                    return {}
                account = portfolio_service.from_paper_account(row)
                view = await portfolio_service.PortfolioService().build(db, account=account)
                return view.to_risk_state()
        except Exception:  # noqa: BLE001 - never turn a portfolio read into a crash
            log.exception(
                "a portfolio snapshot could not be built; risk will see an empty "
                "portfolio, which is the conservative direction",
                extra={
                    "event": "portfolio_snapshot_failed",
                    "account_id": account_id,
                    "mode": mode,
                },
            )
            return {}


def snapshot_for(sessions: async_sessionmaker[AsyncSession]) -> DatabasePortfolio:
    """The snapshot provider the deployed pipeline uses. One function, so there
    is one answer to "what portfolio does risk see", and one place to find it."""
    return DatabasePortfolio(sessions)


__all__ = [
    "DatabasePortfolio",
    "PortfolioSnapshot",
    "snapshot_for",
    "to_portfolio_state",
]
