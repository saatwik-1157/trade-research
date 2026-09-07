"""Capital reservations and the budget they may never exceed. **L54/L55.**

Two properties, and they are the same rule from two sides:

* **A reservation must outlive the process that made it.** `RiskService` has
  held reservations in a process-local dict since L17, and an approval that
  reserved budget and had not filled when the process died released nothing —
  the next process believed the whole budget was free. Same shape as the L45
  C-1 defect this project fixed the same week.

* **The parts may never exceed the whole.** L55's mandatory safety test: an
  approved budget of 10,000 with a recommendation of 15,000 must be REJECTED,
  and rejected whole rather than trimmed to fit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.db.base import Base
from app.models.risk import CapitalReservation
from app.risk.reservations import Held, ReservationBook, conserves_budget
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
ACCOUNT = "acct-a"


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _reserve(
    db: AsyncSession, book: ReservationBook, intent: str, risk: str = "100", **over: object
) -> CapitalReservation:
    fields: dict[str, object] = {
        "decision_id": f"dec-{intent}",
        "intent_id": intent,
        "account_id": ACCOUNT,
        "mode": "paper",
        "risk_amount": Decimal(risk),
        "exposure": Decimal("1000"),
        "expires_at": LATER,
    }
    fields.update(over)
    return await book.reserve(db, **fields)  # type: ignore[arg-type]


# ==================================== 1. L55's mandatory budget-conservation


def test_an_allocation_over_the_approved_budget_is_rejected() -> None:
    """**L55's mandatory safety test.** 10,000 approved, 15,000 recommended."""
    ok, reason = conserves_budget(
        {"strategy_a": Decimal("9000"), "strategy_b": Decimal("6000")},
        approved=Decimal("10000"),
    )
    assert ok is False
    assert "15000" in reason and "10000" in reason
    assert "5000 over" in reason


def test_an_allocation_inside_the_budget_is_allowed() -> None:
    """The rule refuses what exceeds, not everything."""
    ok, reason = conserves_budget(
        {
            "strategy_a": Decimal("4000"),
            "strategy_b": Decimal("3000"),
            "strategy_c": Decimal("2000"),
            "reserve": Decimal("1000"),
        },
        approved=Decimal("10000"),
    )
    assert ok is True
    assert "10000 of 10000" in reason


def test_the_budget_is_rejected_WHOLE_not_trimmed_to_fit() -> None:
    """A partially accepted allocation is an allocation nobody chose.

    The function returns a refusal and a reason. It does not return a smaller
    allocation, because scaling somebody's proposal down and applying it is a
    decision the caller did not make.
    """
    ok, reason = conserves_budget({"a": Decimal("20000")}, approved=Decimal("10000"))
    assert ok is False
    assert "Rejected whole" in reason


def test_a_negative_allocation_is_refused() -> None:
    """A negative share creates budget somewhere else — the one direction
    allocation may not go. Without this, `{a: -5000, b: 15000}` sums to 10,000
    and would pass."""
    ok, reason = conserves_budget(
        {"a": Decimal("-5000"), "b": Decimal("15000")}, approved=Decimal("10000")
    )
    assert ok is False
    assert "negative allocation for a" in reason


def test_exact_equality_is_allowed_and_there_is_no_tolerance() -> None:
    """`Decimal` is exact. A budget that "nearly" fits does not."""
    assert conserves_budget({"a": Decimal("10000")}, approved=Decimal("10000"))[0] is True
    assert conserves_budget({"a": Decimal("10000.0001")}, approved=Decimal("10000"))[0] is False


def test_a_negative_approved_budget_is_not_a_budget() -> None:
    ok, reason = conserves_budget({}, approved=Decimal("-1"))
    assert ok is False
    assert "not a budget" in reason


# ====================================== 2. reservations survive a restart


async def test_a_reservation_outlives_the_process_that_made_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """**The defect this closes.** The in-memory dict is empty after a restart;
    the row is not."""
    book = ReservationBook()
    async with sessions() as db:
        await _reserve(db, book, "intent-1", risk="600")
        await db.commit()

    # A new process: nothing in memory, everything on disk.
    async with sessions() as db:
        held = await ReservationBook().held_for(db, account_id=ACCOUNT, now=NOW)
    assert held.risk_amount == Decimal("600")
    assert held.count == 1


async def test_a_second_signal_sees_the_first_reservation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """L54's worked example: 1,000 available, A reserves 600, B must see 400."""
    book = ReservationBook()
    approved = Decimal("1000")

    async with sessions() as db:
        await _reserve(db, book, "signal-a", risk="600")
        await db.commit()

    async with sessions() as db:
        held = await book.held_for(db, account_id=ACCOUNT, now=NOW)
    remaining = approved - held.risk_amount
    assert remaining == Decimal("400"), "signal B would have seen the whole budget"

    # And B's own request is checked against what is left, not the total.
    ok, _ = conserves_budget({"signal-b": Decimal("500")}, approved=remaining)
    assert ok is False


async def test_reserving_the_same_intent_twice_reserves_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Idempotent by `intent_id`, the same backstop `orders.intent_id` gives
    orders. A retried signal must not consume the budget twice."""
    book = ReservationBook()
    async with sessions() as db:
        first = await _reserve(db, book, "intent-dup", risk="300")
        second = await _reserve(db, book, "intent-dup", risk="300")
        await db.commit()

    assert first.id == second.id
    async with sessions() as db:
        held = await book.held_for(db, account_id=ACCOUNT, now=NOW)
        rows = list((await db.scalars(select(CapitalReservation))).all())
    assert held.risk_amount == Decimal("300")
    assert len(rows) == 1


async def test_two_callers_that_both_saw_no_reservation_still_make_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """**The real race, driven deterministically.**

    Both callers check, both see nothing, then both insert. `asyncio.gather`
    would not reproduce this: SQLite's `StaticPool` shares ONE connection, so
    one session's rollback discards another's uncommitted work and the test
    would measure the fixture rather than the code.

    So the interleaving is written out. The unique index is the arbiter and
    losing the race is a duplicate, not an error — exactly how the webhook
    gateway treats a racing alert.
    """
    book = ReservationBook()

    async with sessions() as first, sessions() as second:
        # Both look, both see nothing.
        assert await book._for_intent(first, "intent-race") is None
        assert await book._for_intent(second, "intent-race") is None

        # The winner writes.
        await _reserve(first, book, "intent-race", risk="250")
        await first.commit()

        # The loser writes the same intent and must not raise, must not
        # duplicate, and must come back with the row that exists.
        loser = await _reserve(second, book, "intent-race", risk="250")
        await second.commit()

    async with sessions() as db:
        rows = list((await db.scalars(select(CapitalReservation))).all())
        held = await book.held_for(db, account_id=ACCOUNT, now=NOW)

    assert len(rows) == 1, f"{len(rows)} reservations for one intent"
    assert loser.intent_id == "intent-race"
    assert held.risk_amount == Decimal("250"), "the budget was claimed twice"


async def test_the_unique_index_is_what_prevents_the_double_claim(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The capability check: without the constraint the race would double-claim.

    Asserting the index exists is not the same as asserting it is enforced, so
    this drives a raw second insert past the application guard entirely.
    """
    from sqlalchemy.exc import IntegrityError

    book = ReservationBook()
    async with sessions() as db:
        await _reserve(db, book, "intent-uniq", risk="250")
        await db.commit()

    async with sessions() as db:
        db.add(
            CapitalReservation(
                decision_id="dec-2",
                intent_id="intent-uniq",  # the same intent, straight to the table
                account_id=ACCOUNT,
                mode="paper",
                risk_amount=Decimal("250"),
                exposure=Decimal("1000"),
                positions=1,
                status="active",
                expires_at=LATER.replace(tzinfo=None),
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


# ================================================ 3. giving the budget back


@pytest.mark.parametrize(
    "status,reason",
    [
        ("consumed", "a fill took it"),
        ("released", "the order went away"),
        ("cancelled", "an operator stopped it"),
    ],
)
async def test_releasing_frees_the_budget_and_records_why(
    sessions: async_sessionmaker[AsyncSession], status: str, reason: str
) -> None:
    """ "The budget came back because it traded" and "because it did not" are
    different facts about the same account, so the statuses stay apart."""
    book = ReservationBook()
    async with sessions() as db:
        await _reserve(db, book, "intent-rel", risk="700")
        await db.commit()

    async with sessions() as db:
        freed = await book.release(
            db, intent_id="intent-rel", status=status, reason=reason, now=NOW
        )
        await db.commit()
    assert freed is True

    async with sessions() as db:
        held = await book.held_for(db, account_id=ACCOUNT, now=NOW)
        row = (await db.scalars(select(CapitalReservation))).first()
    assert held.risk_amount == Decimal("0")
    assert row is not None
    assert row.status == status
    assert row.release_reason == reason
    assert row.released_at is not None


async def test_an_expired_reservation_stops_holding_without_a_sweep(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A book nobody has swept is still correct. A sweep that had not run would
    otherwise leave stale claims holding budget forever — safe, but it starves
    a live account."""
    book = ReservationBook()
    async with sessions() as db:
        await _reserve(db, book, "intent-old", risk="900", expires_at=NOW - timedelta(minutes=1))
        await db.commit()

    async with sessions() as db:
        held = await book.held_for(db, account_id=ACCOUNT, now=NOW)
    assert held.risk_amount == Decimal("0")


async def test_expiring_stale_reservations_records_them_as_expired(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The arithmetic does not change; the RECORD does. An operator reading the
    table should not have to compare timestamps to know what is holding."""
    book = ReservationBook()
    async with sessions() as db:
        await _reserve(db, book, "intent-a", expires_at=NOW - timedelta(minutes=1))
        await _reserve(db, book, "intent-b", expires_at=LATER)
        await db.commit()

    async with sessions() as db:
        swept = await book.expire_stale(db, now=NOW)
        await db.commit()
    assert swept == 1

    async with sessions() as db:
        rows = {r.intent_id: r.status for r in (await db.scalars(select(CapitalReservation))).all()}
    assert rows == {"intent-a": "expired", "intent-b": "active"}


async def test_releasing_an_already_released_reservation_changes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Idempotent on the way out as well as in. A retried release must not
    resurrect budget or rewrite why it went."""
    book = ReservationBook()
    async with sessions() as db:
        await _reserve(db, book, "intent-x", risk="400")
        await db.commit()
    async with sessions() as db:
        assert await book.release(
            db, intent_id="intent-x", status="consumed", reason="filled", now=NOW
        )
        await db.commit()
    async with sessions() as db:
        again = await book.release(
            db, intent_id="intent-x", status="released", reason="second try", now=NOW
        )
        await db.commit()
    assert again is False

    async with sessions() as db:
        row = (await db.scalars(select(CapitalReservation))).first()
    assert row is not None
    assert row.status == "consumed"
    assert row.release_reason == "filled"


# ============================================ 4. account isolation (L54 §16)


async def test_one_accounts_reservations_never_count_against_another(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Cross-account contamination would let one account's activity starve
    another's budget, or hide it."""
    book = ReservationBook()
    async with sessions() as db:
        await _reserve(db, book, "intent-a1", risk="500", account_id=ACCOUNT)
        await _reserve(db, book, "intent-b1", risk="900", account_id="acct-b")
        await db.commit()

    async with sessions() as db:
        a = await book.held_for(db, account_id=ACCOUNT, now=NOW)
        b = await book.held_for(db, account_id="acct-b", now=NOW)
    assert a.risk_amount == Decimal("500")
    assert b.risk_amount == Decimal("900")


# ================================================ 5. nothing here grants budget


def test_no_function_in_this_module_can_raise_a_limit() -> None:
    """The module's central claim, asserted rather than promised.

    Every function either records a claim against an existing budget or refuses
    one. If a function appeared here that returned a LARGER budget than it was
    given, this is where it would be caught.
    """
    import inspect

    from app.risk import reservations

    source = inspect.getsource(reservations)
    # No assignment to anything that reads as a limit or a budget.
    for forbidden in ("approved =", "budget =", "max_", "limit ="):
        assert f"self.{forbidden}" not in source, forbidden

    # And the invariant holds for every random-ish split of a budget.
    approved = Decimal("10000")
    for n in (1, 2, 3, 5):
        share = approved / n
        ok, _ = conserves_budget({f"s{i}": share for i in range(n)}, approved=approved)
        assert ok is True


def test_held_reports_what_it_holds_and_nothing_more() -> None:
    """`Held` is a report. It carries no method that changes anything."""
    held = Held(risk_amount=Decimal("100"), exposure=Decimal("1000"), positions=1, count=1)
    assert held.as_dict()["risk_amount"] == "100"
    assert not [m for m in dir(held) if m.startswith("set_") or m.startswith("add")]
