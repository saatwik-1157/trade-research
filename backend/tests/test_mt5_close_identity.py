"""What the platform records about a close, and what it can ask the venue later.

A close is the half of the lifecycle that had no identity. Until 2026-09-07 the
toolkit's `close_own` reported a retcode, and then a fill, but never the venue's
ticket for the close order it had just sent -- so `orders.broker_order_id` was
empty on every close the platform had ever made. That is the field an
`unknown` close is settled by: the OMS parks the order, refuses to retry it,
and the only safe way out is to ask the venue about that ticket. Without one
there is nothing to ask with, and a parked close can only be resolved by a
human reading MetaTrader's own history beside the database.

The adapter is stubbed. What is under test is which venue identifiers survive
the trip into an `OrderResult`, not whether MetaTrader returns them.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.brokers.base import ConnectionState, OrderStatus
from app.brokers.mt5 import MT5Adapter


def _adapter(row: dict[str, Any] | None) -> MT5Adapter:
    """An adapter whose terminal is a stub returning exactly `row`."""
    adapter = MT5Adapter()
    adapter._state = ConnectionState.connected  # noqa: SLF001
    adapter._mt5 = SimpleNamespace()  # noqa: SLF001
    adapter._paper = SimpleNamespace(  # noqa: SLF001
        close_own=lambda *a, **k: ([] if row is None else [row]),
    )
    return adapter


CLOSED = {
    "ticket": 58332563074,
    "symbol": "EURUSD",
    "status": "CLOSED",
    "retcode": 10009,
    "order": 74110771234,   # the close ORDER's ticket
    "deal": 74110779999,    # the EXECUTION's ticket
    "fill_price": 1.16239,
    "closed_volume": 0.01,
    "requested_volume": 0.01,
    "net": 0.02,
}


async def test_a_confirmed_close_carries_the_venues_order_ticket() -> None:
    result = await _adapter(CLOSED).close_position("58332563074")
    assert result.status is OrderStatus.accepted
    assert result.order_id == "74110771234"
    # The position it closed, not the order that closed it. Two identifiers,
    # and confusing them is how a close gets attributed to the wrong trade.
    assert result.position_id == "58332563074"
    assert result.fill_price == Decimal("1.16239")
    assert result.realized_pnl == Decimal("0.02")


async def test_the_deal_is_kept_apart_from_the_order() -> None:
    """An order is the instruction; a deal is what the venue did about it.

    Only the deal identifies one execution, and the fill book deduplicates by
    it -- so two genuine partial fills of one order, both keyed by the
    position's ticket, would have had the second silently discarded as a
    replay.
    """
    result = await _adapter(CLOSED).close_position("58332563074")
    assert result.deal_id == "74110779999"
    assert result.deal_id != result.order_id
    assert result.deal_id != result.position_id


async def test_an_unknown_close_still_names_the_ticket_to_ask_about() -> None:
    """The case the field exists for.

    The venue said done and reported no fill, so nothing is confirmed and
    nothing is retried. What the platform keeps is the ticket -- the only
    handle it has on a close it cannot otherwise account for.
    """
    row = dict(CLOSED, fill_price=None, closed_volume=None)
    result = await _adapter(row).close_position("58332563074")
    assert result.status is OrderStatus.unknown
    assert result.order_id == "74110771234"


async def test_a_refused_close_names_the_ticket_too() -> None:
    row = dict(CLOSED, status="FAILED", retcode=10016)
    result = await _adapter(row).close_position("58332563074")
    assert result.status is OrderStatus.rejected
    assert result.order_id == "74110771234"


async def test_nothing_sent_means_no_ticket_and_none_is_invented() -> None:
    """`no_quote` is refused before `order_send`, so there is no order to name."""
    row = {"ticket": 58332563074, "status": "no_quote"}
    result = await _adapter(row).close_position("58332563074")
    assert result.status is OrderStatus.rejected
    assert result.order_id is None


async def test_a_venue_that_reports_no_ticket_yields_none_not_a_placeholder() -> None:
    """A missing identifier is a gap. `"None"`, `""` or `"0"` would all read as
    an id downstream and none of them is one."""
    row = dict(CLOSED)
    row["order"] = None
    row["deal"] = None
    result = await _adapter(row).close_position("58332563074")
    assert result.order_id is None
    assert result.deal_id is None


async def test_no_such_position_is_refused_without_touching_the_venue() -> None:
    result = await _adapter(None).close_position("nope")
    assert result.status is OrderStatus.rejected
    assert "no own position" in result.detail


@pytest.mark.parametrize("field", ["order", "deal"])
async def test_a_zero_ticket_is_not_an_identifier(field: str) -> None:
    """MetaTrader reports 0 for "no ticket", and `str(0)` is a truthy string
    that would be stored as though it named something."""
    row = dict(CLOSED)
    row[field] = 0
    result = await _adapter(row).close_position("58332563074")
    assert getattr(result, "order_id" if field == "order" else "deal_id") is None
