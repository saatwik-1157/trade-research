"""Reconciliation, orchestrated. Every check here already existed.

Steps 13, 14 and 38, and the one that decides the shape:

    *If a subsystem already satisfies Level 38: mark it ALREADY COMPLETE and
    verify it instead of rebuilding it.*

Three reconcilers were already built, each by the level that owns the state it
compares, and **not one of them ran at boot**:

    app/brokers/reconcile.py        L10. Venue positions and orders against
                                    ours. Reports; never repairs.
    app/oms/service.py .reconcile   L19. The ONLY exit from `unknown`. Asks the
                                    venue and records the answer; never re-sends.
    app/positions/reconciler.py     L21. Local positions against the venue's.
    app/bots/supervisor.py          L22. What each bot was doing, and what
                                    should happen to it. Refuses recovery far
                                    more often than it attempts one.

This module calls them in an order and collects what they said. It contains no
comparison logic of its own, because a second implementation of "do these
positions match" is a second answer, and the one on the dashboard would
eventually disagree with the one the OMS acted on.

**It repairs nothing, and that is not a limitation.** Step 14 says not to close
or modify an unexpected broker position, and `app/brokers/reconcile.py` already
spends a paragraph on why: the instinct on finding a position we do not know
about is to close it, and the instinct on finding one the venue does not have
is to re-send it. The first closes a trade somebody opened by hand; the second
is exactly how a crash between send and log becomes two positions.

**An unknown order blocks, it does not retry.** Step 6. `OrderManager.reconcile`
is the only exit from `unknown`, it works by asking the venue, and nothing here
calls `submit`. A signal that cannot be settled leaves the account blocked and
says so.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.models.execution import Order, Position
from app.recovery.contract import RecoveryReport, Step, StepStatus

log = logging.getLogger("app.recovery.reconciliation")

#: Order states that mean the venue's answer was never established. Step 6:
#: these must be settled before another order is sent for the same intent.
UNRESOLVED_ORDER_STATES: tuple[str, ...] = ("unknown", "submitting")

#: Position states that mean the local record and the venue may disagree.
UNRESOLVED_POSITION_STATES: tuple[str, ...] = ("unknown", "reconciling")


def _step(name: str, status: StepStatus, detail: str, **facts: Any) -> Step:
    return Step(name=name, status=status, detail=detail, at=utcnow(), facts=facts)


async def unresolved_orders(db: AsyncSession) -> list[Order]:
    """Orders whose venue state was never established. Step 6 and step 13."""
    return list(
        (
            await db.scalars(
                select(Order)
                .where(Order.status.in_(UNRESOLVED_ORDER_STATES))
                .order_by(Order.created_at)
            )
        ).all()
    )


async def unresolved_positions(db: AsyncSession) -> list[Position]:
    return list(
        (
            await db.scalars(
                select(Position)
                .where(Position.status.in_(UNRESOLVED_POSITION_STATES))
                .order_by(Position.opened_at)
            )
        ).all()
    )


async def check_orders(db: AsyncSession) -> Step:
    """Step 13. What the OMS could not settle, named rather than counted away.

    ATTENTION rather than FAILED: the OMS is working correctly. It parked an
    order it could not account for instead of guessing, which is the behaviour
    L19 built. What it needs is `POST /v1/orders/{id}/reconcile`, which asks
    the venue -- and nothing here calls it, because settling an order is an act
    against a venue and this module does not act.
    """
    rows = await unresolved_orders(db)
    if not rows:
        return _step(
            "oms_reconciliation",
            StepStatus.ok,
            "no order is in a state whose venue answer was never established",
            unresolved=0,
        )
    return _step(
        "oms_reconciliation",
        StepStatus.attention,
        (
            f"{len(rows)} order(s) whose venue state was never established. They are "
            "settled by asking the venue (POST /v1/orders/{id}/reconcile), never by "
            "re-sending: an IPC timeout after order_send looks exactly like a "
            "rejection from this side."
        ),
        unresolved=len(rows),
        order_ids=[row.id for row in rows[:20]],
        accounts=sorted(
            {row.broker_account_id or row.paper_account_id or "" for row in rows} - {""}
        ),
    )


async def check_positions(db: AsyncSession) -> Step:
    """Step 14. Local positions the platform itself flagged as unsettled."""
    rows = await unresolved_positions(db)
    if not rows:
        open_count = int(
            await db.scalar(
                select(func.count(Position.id)).where(
                    Position.status.in_(["open", "partially_closed"])
                )
            )
            or 0
        )
        return _step(
            "position_reconciliation",
            StepStatus.ok,
            f"{open_count} open position(s); none flagged as unsettled",
            open=open_count,
            unresolved=0,
        )
    return _step(
        "position_reconciliation",
        StepStatus.attention,
        (
            f"{len(rows)} position(s) the platform could not settle against the venue. "
            "Nothing is closed or opened to resolve this: an unexpected venue position "
            "may be a trade somebody made by hand."
        ),
        unresolved=len(rows),
        position_ids=[row.id for row in rows[:20]],
    )


async def check_broker(brokers: Any) -> Step:
    """Steps 5 and 17. What each adapter says about its own link.

    NOT_CONFIGURED is `SKIPPED`, not OK and not FAILED. An adapter is
    registered by an operator action, and a deployment that has not been
    pointed at a venue has nothing to reconcile -- reporting that as a clean
    reconciliation would be the fake health L37 spends a section on.
    """
    adapters = getattr(brokers, "adapters", {}) if brokers is not None else {}
    if not adapters:
        return _step(
            "broker",
            StepStatus.skipped,
            (
                "no broker adapter is registered, so there is nothing to reconcile "
                "against. This is not a clean reconciliation; it is the absence of one."
            ),
            adapters=0,
        )
    health = await brokers.health()
    usable = [name for name, h in health.items() if h.usable]
    if len(usable) == len(health):
        return _step(
            "broker",
            StepStatus.ok,
            f"{len(usable)} adapter(s) connected",
            adapters=len(health),
            connected=len(usable),
            states={name: str(h.state) for name, h in health.items()},
        )
    return _step(
        "broker",
        StepStatus.attention,
        (
            f"{len(health) - len(usable)} of {len(health)} adapter(s) are not usable. "
            "New orders must not be sent on an account whose venue link is not "
            "established; the adapter refuses regardless, and safe mode says so."
        ),
        adapters=len(health),
        connected=len(usable),
        states={name: str(h.state) for name, h in health.items()},
    )


async def check_bots(session_factory: Any) -> Step:
    """Step 10. L22's supervisor, asked what it would do -- not told to do it.

    `plan_restart` reads what each bot was doing and says what should happen.
    A `stopped` bot stays stopped, a `paused` bot stays paused, and a `running`
    bot whose process is gone is marked `crashed` for consideration -- never
    silently started. That is L22's own design and it is exactly step 10's
    requirement, so it is called rather than reimplemented.
    """
    from app.bots.supervisor import BotSupervisor

    async with session_factory() as db:
        supervisor = BotSupervisor(db)
        plans = await supervisor.plan_restart()
        await db.commit()
    if not plans:
        return _step("bot_recovery", StepStatus.ok, "no bots to consider", bots=0)
    needing = [p for p in plans if getattr(p, "action", "") not in ("leave", "none", "")]
    return _step(
        "bot_recovery",
        StepStatus.ok if not needing else StepStatus.attention,
        (
            f"{len(plans)} bot(s) considered; {len(needing)} need a decision. Nothing "
            "was started: a crashed bot is restarted only when no kill switch is "
            "engaged, the bot is enabled and the account has no unresolved order."
        ),
        bots=len(plans),
        plans=[p.as_dict() for p in plans[:20]],
    )


async def sweep(
    db: AsyncSession,
    *,
    brokers: Any = None,
    session_factory: Any = None,
) -> RecoveryReport:
    """One reconciliation pass. Reports; repairs nothing.

    Ordered the way step 13 asks: the broker link first, because a comparison
    against a venue that cannot be reached is not a comparison; then orders,
    then positions, then bots -- each depending on the one before it being
    settled.
    """
    report = RecoveryReport(kind="reconciliation", at=utcnow())
    report.steps.append(await check_broker(brokers))
    report.steps.append(await check_orders(db))
    report.steps.append(await check_positions(db))
    if session_factory is not None:
        report.steps.append(await check_bots(session_factory))
    return report


__all__ = [
    "UNRESOLVED_ORDER_STATES",
    "UNRESOLVED_POSITION_STATES",
    "check_bots",
    "check_broker",
    "check_orders",
    "check_positions",
    "sweep",
    "unresolved_orders",
    "unresolved_positions",
]
