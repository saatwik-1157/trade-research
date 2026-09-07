"""Durable capital reservations, and the budget they may never exceed.

**Two things live here because they are the same rule seen from two sides.**

`RiskService` has held reservations in a process-local dict since L17. That is
correct for the race it was built for and it is empty after a restart -- an
approval that reserved budget and had not filled when the process died releases
nothing, and the next process believes the whole budget is free. Same shape as
the L45 C-1 defect: a guard whose state does not outlive its process.

`ReservationBook` is the durable record. It does not replace the dict; the dict
stays as the fast path inside one process and is rebuilt FROM this on startup,
because two derivations of one fact eventually disagree.

`conserves_budget` is the invariant an allocator must satisfy: **the parts may
never exceed the whole.** L55 states it as a mandatory safety test -- an
approved budget of 10,000 with a recommendation of 15,000 must be REJECTED --
and it is written here, next to the reservations, because a reservation IS a
claim on that budget and the two must be checked together.

**Nothing here grants budget.** Every function either records a claim against
an existing budget or refuses one. There is no path by which a reservation, an
allocation or an optimizer raises a limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.risk import RESERVATION_HOLDING, CapitalReservation

log = logging.getLogger("app.risk.reservations")

ZERO = Decimal("0")


class BudgetExceeded(Exception):
    """An allocation claimed more than the approved budget. Never partial."""


@dataclass(frozen=True)
class Held:
    """What an account currently has claimed but not yet settled."""

    risk_amount: Decimal
    exposure: Decimal
    positions: int
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_amount": str(self.risk_amount),
            "exposure": str(self.exposure),
            "positions": self.positions,
            "reservations": self.count,
        }


def conserves_budget(allocations: dict[str, Decimal], *, approved: Decimal) -> tuple[bool, str]:
    """Do the parts fit inside the whole? **L55's mandatory safety rule.**

    Returns `(ok, reason)` rather than raising, so a caller can record WHY a
    candidate was rejected without catching an exception to read it.

    Three refusals, and each is a different mistake:

      * a negative allocation -- which would create budget elsewhere;
      * a negative approved budget -- which is not a budget;
      * a total above the approved figure -- the case L55 names.

    **There is no tolerance and no rounding.** `Decimal` is exact and a budget
    that "nearly" fits is a budget that does not. An optimizer convinced the
    portfolio can support more must ask a human to raise the limit, which is a
    different action with a different approval.
    """
    if approved < ZERO:
        return False, f"the approved budget is negative ({approved}); that is not a budget"
    negative = sorted(k for k, v in allocations.items() if v < ZERO)
    if negative:
        return False, (
            f"negative allocation for {', '.join(negative)}. A negative share creates "
            "budget somewhere else, which is the one direction allocation may not go"
        )
    total = sum(allocations.values(), ZERO)
    if total > approved:
        over = total - approved
        return False, (
            f"the allocation totals {total} against an approved budget of {approved}, "
            f"which is {over} over. Rejected whole: a partially accepted allocation is "
            "an allocation nobody chose"
        )
    return True, f"{total} of {approved} allocated, {approved - total} unallocated"


class ReservationBook:
    """The durable side of a reservation. Holds no session of its own."""

    async def held_for(self, db: AsyncSession, *, account_id: str, now: datetime) -> Held:
        """What this account has claimed and not yet settled.

        Expired reservations are excluded by the query rather than by a sweep,
        so a book nobody has swept is still correct. A sweep that had not run
        would otherwise leave stale claims holding budget forever, which fails
        in the safe direction but starves a live account.
        """
        rows = (
            await db.scalars(
                select(CapitalReservation).where(
                    CapitalReservation.account_id == account_id,
                    CapitalReservation.status.in_(RESERVATION_HOLDING),
                    CapitalReservation.expires_at > now.replace(tzinfo=None),
                )
            )
        ).all()
        return Held(
            risk_amount=sum((r.risk_amount for r in rows), ZERO),
            exposure=sum((r.exposure for r in rows), ZERO),
            positions=sum(r.positions for r in rows),
            count=len(rows),
        )

    async def reserve(
        self,
        db: AsyncSession,
        *,
        decision_id: str,
        intent_id: str,
        account_id: str,
        mode: str,
        risk_amount: Decimal,
        exposure: Decimal,
        expires_at: datetime,
        strategy_id: str | None = None,
        bot_id: str | None = None,
        symbol: str | None = None,
        positions: int = 1,
    ) -> CapitalReservation:
        """Claim budget for one intent. **Idempotent.**

        A retried signal reserves once: `intent_id` is UNIQUE, and a caller
        that races itself gets the existing row back rather than an error. The
        same rule and the same enforcement as `orders.intent_id`.
        """
        existing = await self._for_intent(db, intent_id)
        if existing is not None:
            log.info(
                "a reservation already exists for this intent; returning it",
                extra={
                    "event": "reservation_duplicate",
                    "intent_id": intent_id,
                    "reservation_id": existing.id,
                    "status": existing.status,
                },
            )
            return existing

        row = CapitalReservation(
            decision_id=decision_id,
            intent_id=intent_id,
            account_id=account_id,
            mode=mode,
            strategy_id=strategy_id,
            bot_id=bot_id,
            symbol=symbol,
            risk_amount=risk_amount,
            exposure=exposure,
            positions=positions,
            status="active",
            expires_at=expires_at.replace(tzinfo=None),
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            # Two callers raced past the read above. The unique index is the
            # arbiter and losing the race is a duplicate, not an error --
            # exactly how the webhook gateway treats a racing alert.
            await db.rollback()
            found = await self._for_intent(db, intent_id)
            if found is None:  # pragma: no cover - the index just refused it
                raise
            return found
        return row

    async def release(
        self,
        db: AsyncSession,
        *,
        intent_id: str,
        status: str,
        reason: str,
        now: datetime,
        order_id: str | None = None,
    ) -> bool:
        """Stop holding budget, and say why.

        `status` is the caller's statement of what happened -- `consumed` when
        a fill took it, `released` when the order went away, `cancelled` when
        somebody stopped it. They are kept apart because "the budget came back
        because it traded" and "the budget came back because it did not" are
        different facts about the same account.
        """
        result = await db.execute(
            update(CapitalReservation)
            .where(
                CapitalReservation.intent_id == intent_id,
                CapitalReservation.status.in_(RESERVATION_HOLDING),
            )
            .values(
                status=status,
                release_reason=reason[:500],
                released_at=now.replace(tzinfo=None),
                **({"order_id": order_id} if order_id else {}),
            )
        )
        # `rowcount` is on the CursorResult the ORM returns for an UPDATE;
        # the static type is the wider `Result`, which does not declare it.
        return bool(getattr(result, "rowcount", 0))

    async def expire_stale(self, db: AsyncSession, *, now: datetime) -> int:
        """Mark reservations whose approval aged out.

        `held_for` already excludes them, so this changes no arithmetic. It
        exists so the RECORD says expired rather than leaving a row that looks
        live forever -- an operator reading the table should not have to
        compare timestamps to know what is holding.
        """
        result = await db.execute(
            update(CapitalReservation)
            .where(
                CapitalReservation.status.in_(RESERVATION_HOLDING),
                CapitalReservation.expires_at <= now.replace(tzinfo=None),
            )
            .values(
                status="expired",
                release_reason="the approval expired before anything consumed it",
                released_at=now.replace(tzinfo=None),
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def _for_intent(self, db: AsyncSession, intent_id: str) -> CapitalReservation | None:
        return (
            await db.scalars(
                select(CapitalReservation).where(CapitalReservation.intent_id == intent_id)
            )
        ).first()


__all__ = ["BudgetExceeded", "Held", "ReservationBook", "conserves_budget"]
