"""The Order Management System (L19).

The paper venue's OMS has been tested since L16 (`tests/test_paper.py`). This
file tests the *broker-bound* lifecycle — the one that drives a real
`BrokerAdapter` — and the pieces both now share: the state machine, the fill
accounting and the reconciliation rule.

The cases follow the level brief's list of thirty, and the ones that matter
most are the ones about not knowing:

  * `test_an_unknown_submission_is_never_retried` — an IPC timeout after
    `order_send` looks exactly like a rejection from the caller's side.
  * `test_a_second_order_for_an_unresolved_intent_is_refused` — the guard at
    the entrance, not only at the exit.
  * `test_reconciliation_that_cannot_read_the_venue_concludes_nothing` — the
    failure mode where a reconciler decides an order is lost because the
    network was down.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.brokers.base import BrokerOrder, NotConnected, OrderResult, SymbolInfo
from app.brokers.base import OrderStatus as BrokerOrderStatus
from app.brokers.fake import FakeBroker
from app.execution.store import OrderNotRecorded, PriorOrder
from app.oms import (
    NEEDS_RECONCILIATION,
    SAFE_TO_RESEND,
    TERMINAL,
    TRANSITIONS,
    FillBook,
    FillError,
    FillRecord,
    IllegalOrderTransition,
    ManagedOrder,
    OrderManager,
    OrderRefused,
    OrderStatus,
    ReconciliationRequired,
    can_resend,
)
from app.oms.registry import OrderManagerRegistry
from app.oms.worker import OmsReconcileWorker
from app.risk.engine import Approval, OrderProposal, PortfolioState, RiskEngine, RiskLimits

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

SPEC = SymbolInfo(
    symbol="EURUSD",
    digits=5,
    point=Decimal("0.00001"),
    contract_size=Decimal("100000"),
    tick_size=Decimal("0.00001"),
    tick_value=Decimal("1"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("100"),
    volume_step=Decimal("0.01"),
)


# ================================================================= fixtures


def proposal(**overrides: object) -> OrderProposal:
    base: dict[str, object] = {
        "symbol": "EURUSD",
        "side": "buy",
        "mode": "demo",
        "volume": Decimal("1"),
        "entry_price": Decimal("1.1000"),
        "stop_loss": Decimal("1.0980"),
        "take_profit": Decimal("1.1040"),
        "account_id": "acct-a",
        "strategy_id": "sma_cross",
        "base_currency": "USD",
    }
    base.update(overrides)
    return OrderProposal(**base)  # type: ignore[arg-type]


def approve(**overrides: object) -> Approval:
    """A genuine Approval. There is no other way to build one, which is the
    guarantee the OMS rests on. `at` is the decision time; an approval
    expires a minute after it, so a test that submits later must decide
    later too."""
    at = overrides.pop("at", T0)
    assert isinstance(at, datetime)
    engine = RiskEngine(RiskLimits(require_stop_loss=False))
    approval, verdict = engine.approve(proposal(**overrides), PortfolioState(), now=at)
    assert approval is not None, verdict.reason
    return approval


@pytest.fixture
async def broker() -> AsyncIterator[FakeBroker]:
    fake = FakeBroker(mode="demo")
    await fake.connect()
    fake.set_quote("EURUSD", "1.10000", "1.10002")
    fake.symbols["EURUSD"] = SPEC
    yield fake
    await fake.disconnect()


@pytest.fixture
def oms(broker: FakeBroker) -> OrderManager:
    return OrderManager(broker, mode="demo", broker="fake")


# ===================================================== 1-3. create, approval


def test_an_order_cannot_be_created_without_an_approval(oms: OrderManager) -> None:
    """The type is the guarantee, not a check somebody has to remember."""
    with pytest.raises(OrderRefused, match="risk.Approval"):
        oms.create("not an approval", intent_id="i-1", account_id="acct-a")  # type: ignore[arg-type]


def test_an_expired_approval_is_refused(oms: OrderManager) -> None:
    """A decision made against a portfolio snapshot minutes ago is not
    evidence about now."""
    approval = approve()
    with pytest.raises(OrderRefused, match="expired"):
        oms.create(
            approval,
            intent_id="i-1",
            account_id="acct-a",
            at=T0 + timedelta(hours=1),
        )


def test_an_approval_for_another_mode_is_refused(oms: OrderManager) -> None:
    with pytest.raises(OrderRefused, match="different execution environment"):
        oms.create(approve(mode="paper"), intent_id="i-1", account_id="acct-a", at=T0)


def test_the_created_order_carries_the_decision_that_authorised_it(
    oms: OrderManager,
) -> None:
    """Brief §13 and §37: an order must be traceable to its approval."""
    approval = approve()
    order = oms.create(approval, intent_id="i-1", account_id="acct-a", at=T0).order
    assert order.risk_decision_id == approval.decision_id
    assert order.risk_snapshot["request_hash"] == approval.request_hash
    assert order.status is OrderStatus.intent
    assert order.filled_quantity == Decimal("0")
    assert order.remaining_quantity == order.quantity


# ============================================================ 4. sizing


def test_the_oms_computes_no_quantity_of_its_own() -> None:
    """One authoritative sizing calculation. The quantity arrives on the
    approval, already measured; recomputing it here would be a second."""
    package = Path(__file__).resolve().parents[1] / "app" / "oms"
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith("app.sizing"), f"{path.name} imports {name}"


def test_tampering_with_the_approved_volume_breaks_the_binding(
    oms: OrderManager,
) -> None:
    """Stronger than refusing a null volume: the approved volume is one of the
    fields `request_hash` is computed over, so editing it after the fact makes
    the approval stop binding to any order at all. Risk approving order A while
    order B is submitted is not expressible."""
    approval = approve()
    object.__setattr__(approval, "approved_volume", Decimal("99"))
    with pytest.raises(OrderRefused, match="does not bind"):
        oms.create(approval, intent_id="i-1", account_id="acct-a", at=T0)


# ========================================================== 5. idempotency


def test_the_same_intent_produces_one_order(oms: OrderManager) -> None:
    first = oms.create(approve(), intent_id="tv-alert-1", account_id="acct-a", at=T0)
    second = oms.create(approve(), intent_id="tv-alert-1", account_id="acct-a", at=T0)
    assert first.created is True
    assert second.duplicate is True
    assert second.order is first.order
    assert len(oms.orders) == 1
    assert oms.status()["duplicate_orders_prevented"] == 1


async def test_a_duplicate_tradingview_alert_creates_no_second_broker_order(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """The retry case the brief calls critical. The alert id IS the intent id,
    so a retried alert and a retried submit are one fact with one guard."""
    order = oms.create(approve(), intent_id="tv-alert-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert order.status is OrderStatus.filled

    again = oms.create(approve(), intent_id="tv-alert-1", account_id="acct-a", at=T0)
    assert again.duplicate
    assert len(await broker.get_positions()) == 1


# ============================================== 6-11. submission and fills


async def test_a_market_order_is_submitted_and_filled(
    oms: OrderManager, broker: FakeBroker
) -> None:
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert order.status is OrderStatus.filled
    assert order.broker_order_id is not None
    assert order.filled_quantity == Decimal("1")
    assert order.remaining_quantity == Decimal("0")
    # A buy lifts the ask.
    assert order.average_fill_price == Decimal("1.10002")
    assert order.filled_at is not None


async def test_state_is_durable_before_the_venue_is_called(oms: OrderManager) -> None:
    """§18: the record exists before anything is transmitted. `submitting` is
    what makes a crashed send reconcilable rather than invisible."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    seen = [str(t.new) for t in order.transitions]
    assert seen[0] == "submitting"
    assert seen.index("submitting") < seen.index("submitted")


async def test_only_an_intent_can_be_submitted(oms: OrderManager) -> None:
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    with pytest.raises(OrderRefused, match="only an order in `intent`"):
        await oms.submit(order, at=T0)


async def test_a_limit_order_without_a_price_is_refused(oms: OrderManager) -> None:
    with pytest.raises(OrderRefused, match="needs a price"):
        oms.create(
            approve(entry_price=None, stop_loss=None, take_profit=None),
            intent_id="i-1",
            account_id="acct-a",
            order_type="limit",
            at=T0,
        )


async def test_an_unsupported_order_type_or_tif_is_refused(oms: OrderManager) -> None:
    with pytest.raises(OrderRefused, match="unsupported order type"):
        oms.create(approve(), intent_id="i-1", account_id="acct-a", order_type="iceberg", at=T0)
    with pytest.raises(OrderRefused, match="unsupported time in force"):
        oms.create(approve(), intent_id="i-2", account_id="acct-a", time_in_force="gtd", at=T0)


# ======================================================== 12. partial fills


def test_partial_fills_accumulate_deterministically() -> None:
    """The brief's worked example: 40 @ 100.20 then 60 @ 100.25 averages
    100.23 exactly, and the average is recomputed from the list rather than
    updated in place, so a restart that reloads the fills gets the same
    number."""
    book = FillBook(requested=Decimal("100"))
    book.add(FillRecord(Decimal("40"), Decimal("100.20"), T0, "broker", "d1"))
    assert book.filled_quantity == Decimal("40")
    assert book.remaining_quantity == Decimal("60")
    assert book.partial is True
    book.add(FillRecord(Decimal("60"), Decimal("100.25"), T0, "broker", "d2"))
    assert book.complete is True
    assert book.average_price == Decimal("100.23")


async def test_a_partial_fill_leaves_the_order_partially_filled(
    oms: OrderManager,
) -> None:
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")
    oms.apply_execution_report(
        order, quantity=Decimal("0.4"), price=Decimal("1.1"), at=T0, broker_deal_id="d1"
    )
    assert order.status is OrderStatus.partially_filled
    assert order.remaining_quantity == Decimal("0.6")
    oms.apply_execution_report(
        order, quantity=Decimal("0.6"), price=Decimal("1.2"), at=T0, broker_deal_id="d2"
    )
    assert order.status is OrderStatus.filled
    assert order.remaining_quantity == Decimal("0")


def test_a_requested_quantity_is_never_a_filled_quantity() -> None:
    """Invariant §42.9. An acknowledged order has filled nothing."""
    order = ManagedOrder(
        id="o-1",
        client_order_id="i-1",
        account_id="acct-a",
        symbol="EURUSD",
        side="buy",
        quantity=Decimal("1"),
        mode="demo",
        created_at=T0,
    )
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")
    assert order.status is OrderStatus.accepted
    assert order.filled_quantity == Decimal("0")
    assert order.average_fill_price is None  # not zero


def test_an_overfill_is_refused_rather_than_truncated() -> None:
    book = FillBook(requested=Decimal("1"))
    book.add(FillRecord(Decimal("0.6"), Decimal("1.1"), T0, "broker", "d1"))
    with pytest.raises(FillError, match="exceeds"):
        book.add(FillRecord(Decimal("0.6"), Decimal("1.1"), T0, "broker", "d2"))
    assert book.filled_quantity == Decimal("0.6")  # unchanged


def test_a_repeated_execution_report_is_ignored(oms: OrderManager) -> None:
    """Reports arrive twice — a reconnect replays them, a reconciliation
    re-reads history. A deal applied twice is a position twice the size."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")
    assert oms.apply_execution_report(
        order, quantity=Decimal("0.5"), price=Decimal("1.1"), at=T0, broker_deal_id="d1"
    )
    assert not oms.apply_execution_report(
        order, quantity=Decimal("0.5"), price=Decimal("1.1"), at=T0, broker_deal_id="d1"
    )
    assert order.filled_quantity == Decimal("0.5")
    assert oms.status()["duplicate_fills_ignored"] == 1


def test_a_fill_for_an_unacknowledged_order_is_refused(oms: OrderManager) -> None:
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    with pytest.raises(OrderRefused, match="must be reconciled"):
        oms.apply_execution_report(order, quantity=Decimal("1"), price=Decimal("1.1"), at=T0)


# =================================================== 13-14. cancel, modify


async def test_a_refused_cancel_leaves_the_order_standing(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """The simulator refuses every cancel, because it fills market orders
    immediately and has nothing pending. That is the branch worth pinning:
    when the venue says no, the order goes back to being live rather than
    being recorded as cancelled."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")
    order.broker_order_id = "fake-pos-1"

    await oms.cancel(order, reason="operator asked", at=T0)
    seen = [str(t.new) for t in order.transitions]
    assert "cancel_requested" in seen
    assert order.status is OrderStatus.accepted
    assert order.cancelled_at is None
    assert oms.status()["orders_cancelled"] == 0


async def test_a_confirmed_cancel_is_requested_before_it_is_believed() -> None:
    """ "We asked" and "the venue agreed" are two states, in that order.
    Assuming a cancellation succeeded is how a position nobody is watching
    stays open."""

    class Confirming(FakeBroker):
        async def cancel_order(self, order_id: str) -> OrderResult:  # noqa: D102
            # `accepted` is the adapter vocabulary's "the venue took this
            # request". It has three states by design — accepted, rejected,
            # unknown — because those are the three things a venue can tell
            # you about any request, cancels included.
            return OrderResult(
                BrokerOrderStatus.accepted, "cancelled", order_id=order_id, retcode=0
            )

    venue = Confirming(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    manager = OrderManager(venue, mode="demo", broker="fake")
    order = manager.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")
    order.broker_order_id = "fake-pos-1"

    await manager.cancel(order, reason="operator asked", at=T0)
    seen = [str(t.new) for t in order.transitions]
    assert seen.index("cancel_requested") < seen.index("cancelled")
    assert order.status is OrderStatus.cancelled
    assert order.cancelled_at == T0
    assert manager.status()["orders_cancelled"] == 1


async def test_a_cancel_with_no_broker_id_parks_rather_than_assuming() -> None:
    """Nothing was acknowledged with an id, so there is nothing to cancel BY
    id. That is not the same as knowing the venue holds nothing."""
    venue = FakeBroker(mode="demo")
    await venue.connect()
    manager = OrderManager(venue, mode="demo", broker="fake")
    order = manager.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")

    await manager.cancel(order, at=T0)
    assert order.status is OrderStatus.unknown
    assert order.needs_reconciliation


async def test_a_terminal_order_cannot_be_cancelled(oms: OrderManager, broker: FakeBroker) -> None:
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert order.status is OrderStatus.filled
    with pytest.raises(OrderRefused, match="terminal order is not active"):
        await oms.cancel(order, at=T0)


async def test_an_unresolved_order_cannot_be_cancelled(
    oms: OrderManager, broker: FakeBroker
) -> None:
    broker.unknown_next = True
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert order.status is OrderStatus.unknown
    with pytest.raises(ReconciliationRequired):
        await oms.cancel(order, at=T0)


async def test_a_modification_needs_a_fresh_approval(oms: OrderManager, broker: FakeBroker) -> None:
    """The approval that authorised the original order was bound by
    request_hash to THOSE levels. Changed levels need a fresh evaluation."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    with pytest.raises(OrderRefused, match="filled or cancelled"):
        await oms.modify(order, approval=approve(), stop_loss=Decimal("1.09"), at=T0)


async def test_a_modification_is_written_only_after_the_venue_confirms(
    oms: OrderManager, broker: FakeBroker
) -> None:
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")
    order.broker_order_id = "missing-position"
    before = order.stop_loss
    with pytest.raises(OrderRefused):
        await oms.modify(order, approval=approve(), stop_loss=Decimal("1.0900"), at=T0)
    # The venue refused, so the local record still says what the venue holds.
    assert order.stop_loss == before


# ================================================= 15-17. reject, fail, unknown


async def test_a_venue_rejection_is_recorded_with_its_reason(
    oms: OrderManager, broker: FakeBroker
) -> None:
    broker.fail_next = "not enough money"
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert order.status is OrderStatus.rejected
    assert "not enough money" in (order.reject_reason or "")
    assert oms.status()["orders_rejected"] == 1


async def test_a_request_that_never_reached_the_venue_is_failed_not_unknown(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """`failed` and `rejected` and `unknown` are three different facts, and
    the difference decides whether a re-send is safe."""
    await broker.disconnect()
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert order.status is OrderStatus.failed
    assert can_resend(order.status) is True


async def test_an_unknown_submission_is_never_retried(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """The rule the whole module is built around."""
    broker.unknown_next = True
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert order.status is OrderStatus.unknown
    assert can_resend(order.status) is False
    assert order.needs_reconciliation
    # Nothing was sent twice.
    assert len(await broker.get_positions()) == 0
    assert oms.status()["orders_unknown"] == 1


def test_a_second_order_for_an_unresolved_intent_is_refused(oms: OrderManager) -> None:
    """The guard at the entrance, not only at the exit: a caller that has
    forgotten the old order still cannot create a second for the same intent."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitting, at=T0, source="pipeline")
    order.move(OrderStatus.unknown, at=T0, source="broker", reason="timeout")
    with pytest.raises(ReconciliationRequired, match="two positions"):
        oms.guard_resend("i-1")


def test_a_resend_is_refused_for_every_state_but_failed(oms: OrderManager) -> None:
    assert SAFE_TO_RESEND == {OrderStatus.failed}
    for status in OrderStatus:
        assert can_resend(status) is (status is OrderStatus.failed)


# ==================================================== 18. reconciliation


async def test_reconciliation_finds_the_position_a_lost_response_created(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """The response went missing, not the order. The venue is authoritative."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    # Simulate the response having been lost: the venue holds the position,
    # our record does not know how it ended.
    order.status = OrderStatus.unknown
    order.book.fills.clear()
    await oms.reconcile(order, at=T0)
    assert order.status is OrderStatus.filled
    assert order.filled_quantity == Decimal("1")


async def test_reconciliation_marks_an_order_the_venue_never_saw_as_failed(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """`failed` is the ONLY state a fresh order may follow, and this is how one
    is reached honestly: by asking, not by assuming."""
    broker.unknown_next = True
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert order.status is OrderStatus.unknown

    await oms.reconcile(order, at=T0)
    assert order.status is OrderStatus.failed
    assert order.error_code == "NOT_AT_VENUE"
    assert can_resend(order.status)


async def test_reconciliation_that_cannot_read_the_venue_concludes_nothing(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """A reconciler that decides an order is lost because the network was down
    is worse than one that refuses to decide."""
    broker.unknown_next = True
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    await broker.disconnect()

    with pytest.raises(ReconciliationRequired, match="could not be read"):
        await oms.reconcile(order, at=T0)
    assert order.status is OrderStatus.unknown  # unchanged
    assert oms.status()["reconciliation_failures"] == 1


def test_an_unknown_order_can_never_go_back_to_being_sendable() -> None:
    """Asserted on the machine itself rather than by trying every caller.

    Every exit from `unknown` is a state a venue read can establish. None of
    them is a state from which anything is transmitted, so "reconcile, never
    retry" holds by construction rather than by discipline."""
    exits = TRANSITIONS[OrderStatus.unknown]
    sendable = {OrderStatus.intent, OrderStatus.submitting, OrderStatus.submitted}
    assert not (exits & sendable)
    # And nothing here may be re-sent for the same intent. `failed` is the
    # only state that licenses that, and it is not reachable by widening this
    # set -- stated explicitly because the membership list below is a
    # snapshot and this is the property it is protecting.
    assert (exits - {OrderStatus.failed}) & SAFE_TO_RESEND == set()
    assert exits == {
        OrderStatus.filled,
        OrderStatus.partially_filled,
        OrderStatus.cancelled,
        OrderStatus.rejected,
        OrderStatus.expired,
        OrderStatus.failed,
        # Added 2026-09-18. It satisfies this test's own stated property --
        # a state a venue read can establish, from which nothing is
        # transmitted -- and its absence was the reason reconciliation wrote
        # `rejected` over orders the venue was still working. See the
        # justification in `state.py`.
        OrderStatus.accepted,
    }


# =============================================== 21. restart recovery


def test_recovery_reports_what_must_be_settled_and_sends_nothing(
    oms: OrderManager,
) -> None:
    """§39: never assume local state is correct after a crash."""
    live = ManagedOrder(
        id="o-1",
        client_order_id="i-1",
        account_id="acct-a",
        symbol="EURUSD",
        side="buy",
        quantity=Decimal("1"),
        mode="demo",
        created_at=T0,
        status=OrderStatus.unknown,
    )
    mid_send = ManagedOrder(
        id="o-2",
        client_order_id="i-2",
        account_id="acct-a",
        symbol="EURUSD",
        side="buy",
        quantity=Decimal("1"),
        mode="demo",
        created_at=T0,
        status=OrderStatus.submitting,
    )
    done = ManagedOrder(
        id="o-3",
        client_order_id="i-3",
        account_id="acct-a",
        symbol="EURUSD",
        side="buy",
        quantity=Decimal("1"),
        mode="demo",
        created_at=T0,
        status=OrderStatus.filled,
    )
    needs = oms.resume([live, mid_send, done])
    assert {o.id for o in needs} == {"o-1", "o-2"}
    assert oms.status()["unresolved"] == ["o-1", "o-2"]
    # And nothing can be sent for those intents until they are settled.
    for intent in ("i-1", "i-2"):
        with pytest.raises(ReconciliationRequired):
            oms.guard_resend(intent)


async def test_a_crashed_submission_reconciles_through_unknown(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """A `submitting` row found after a restart is exactly the case the state
    exists for."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitting, at=T0, source="pipeline")
    await oms.reconcile(order, at=T0)
    assert order.status is OrderStatus.failed
    seen = [str(t.new) for t in order.transitions]
    assert "unknown" in seen  # it went through unknown, not around it


# ============================================== the state machine itself


def test_a_terminal_order_has_no_outgoing_transitions() -> None:
    """The forbidden moves in §8 are unreachable because the edges do not
    exist, not because a check rejects them."""
    for status in TERMINAL:
        assert TRANSITIONS[status] == frozenset(), status


@pytest.mark.parametrize(
    ("current", "wanted"),
    [
        (OrderStatus.filled, OrderStatus.intent),
        (OrderStatus.filled, OrderStatus.submitted),
        (OrderStatus.cancelled, OrderStatus.submitted),
        (OrderStatus.rejected, OrderStatus.filled),
        (OrderStatus.expired, OrderStatus.submitted),
    ],
)
def test_the_forbidden_transitions_raise(current: OrderStatus, wanted: OrderStatus) -> None:
    order = ManagedOrder(
        id="o-1",
        client_order_id="i-1",
        account_id="acct-a",
        symbol="EURUSD",
        side="buy",
        quantity=Decimal("1"),
        mode="demo",
        created_at=T0,
        status=current,
    )
    with pytest.raises(IllegalOrderTransition):
        order.move(wanted, at=T0, source="pipeline")


def test_an_illegal_transition_leaves_the_order_untouched() -> None:
    """A half-applied state change describes an order that never existed."""
    order = ManagedOrder(
        id="o-1",
        client_order_id="i-1",
        account_id="acct-a",
        symbol="EURUSD",
        side="buy",
        quantity=Decimal("1"),
        mode="demo",
        created_at=T0,
        status=OrderStatus.filled,
    )
    with pytest.raises(IllegalOrderTransition):
        order.move(OrderStatus.submitted, at=T0, source="pipeline")
    assert order.status is OrderStatus.filled
    assert order.transitions == []


def test_every_transition_records_who_caused_it() -> None:
    order = ManagedOrder(
        id="o-1",
        client_order_id="i-1",
        account_id="acct-a",
        symbol="EURUSD",
        side="buy",
        quantity=Decimal("1"),
        mode="demo",
        created_at=T0,
    )
    order.move(OrderStatus.submitted, at=T0, source="pipeline", reason="sent")
    entry = order.transitions[-1]
    assert entry.previous is OrderStatus.intent
    assert entry.new is OrderStatus.submitted
    assert entry.source == "pipeline"
    with pytest.raises(ValueError, match="unknown transition source"):
        order.move(OrderStatus.accepted, at=T0, source="whoever")


def test_the_paper_oms_and_the_broker_oms_share_one_state_machine() -> None:
    """Two machines that disagree about whether `accepted -> cancelled` is
    legal is how a cancel succeeds in paper and corrupts an order in demo."""
    from app.oms import state as shared
    from app.paper import oms as paper

    assert paper.OrderStatus is shared.OrderStatus
    assert paper.TRANSITIONS is shared.TRANSITIONS
    assert paper.check_transition is shared.check_transition


def test_the_states_the_table_allows_and_the_code_reaches_are_the_same_set() -> None:
    from app.models.execution import ORDER_STATUSES

    assert {s.value for s in OrderStatus} == set(ORDER_STATUSES)


def test_needs_reconciliation_is_exactly_the_uncertain_states() -> None:
    assert NEEDS_RECONCILIATION == {OrderStatus.unknown, OrderStatus.submitting}


# ================================================== 26. live trading fence


def test_live_trading_is_still_disabled_by_default() -> None:
    from app.core.settings import LIVE_GATES, Settings

    settings = Settings(_env_file=None)
    assert settings.live_trading is False
    assert settings.live_execution_allowed is False
    assert str(settings.trading_mode) == "paper"
    assert not any(LIVE_GATES.values())


def test_the_oms_holds_no_broker_credential() -> None:
    """The manager holds an adapter, never a secret.

    Checked on the parsed CODE rather than the file text, so the module may
    explain in prose why it holds no credential without the test reading its
    own explanation as a violation.
    """
    package = Path(__file__).resolve().parents[1] / "app" / "oms"
    forbidden = {"password", "api_key", "secret", "token", "login", "credentials"}
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id.lower() not in forbidden, f"{path.name} uses {node.id}"
            if isinstance(node, ast.Attribute):
                assert node.attr.lower() not in forbidden, f"{path.name} reads .{node.attr}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("MetaTrader5"), path.name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("MetaTrader5"), path.name


def test_the_status_report_states_the_retry_policy(oms: OrderManager) -> None:
    status = oms.status()
    assert "None from `unknown`" in str(status["retry_policy"])
    for key in (
        "orders_created",
        "orders_submitted",
        "orders_filled",
        "orders_partially_filled",
        "orders_cancelled",
        "orders_rejected",
        "orders_failed",
        "orders_refused_on_venue_spec",
        "orders_unknown",
        "orders_reconciled",
        "duplicate_orders_prevented",
        "broker_submission_latency_ms_mean",
        "reconciliation_failures",
    ):
        assert key in status


# ============================================== 28. events on the existing bus


def test_every_order_state_has_an_event_type() -> None:
    """The L07 catalogue declared eleven ORDER_* types and named L19 as the
    level that would produce them. Ten had no producer until now. The map is
    total, so a state added later without an event fails the build."""
    from app.oms.events import EVENT_FOR

    assert set(EVENT_FOR) == set(OrderStatus)


async def test_the_lifecycle_publishes_through_the_existing_bus_types(
    oms: OrderManager, broker: FakeBroker
) -> None:
    from app.realtime.catalogue import CATALOGUE, EventType

    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)

    events = oms.drain_events()
    kinds = [e[0] for e in events]
    assert kinds[0] == str(EventType.ORDER_CREATED)
    assert str(EventType.ORDER_SUBMITTED) in kinds
    assert str(EventType.ORDER_ACKNOWLEDGED) in kinds
    assert str(EventType.ORDER_FILLED) in kinds
    # Every type used is one the catalogue already declares, on the scope it
    # assigned. No second event system.
    for kind in kinds:
        assert EventType(kind) in CATALOGUE


async def test_draining_events_twice_does_not_republish(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """A fill published twice is a fill an operator sees twice."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert oms.drain_events()
    assert oms.drain_events() == []


async def test_an_unknown_order_is_published_like_any_other(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """Suppressing it because it looks like an error is how it stops being
    noticed, and noticing is the entire point of the state."""
    from app.realtime.catalogue import EventType

    broker.unknown_next = True
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    assert str(EventType.ORDER_UNKNOWN) in [e[0] for e in oms.drain_events()]


async def test_a_broken_event_sink_does_not_break_the_lifecycle(
    broker: FakeBroker,
) -> None:
    """The durable record is already written when events flush. A delivery
    problem must not become a lifecycle problem."""

    async def explode(*_args: object) -> None:
        raise RuntimeError("the bus is down")

    manager = OrderManager(broker, mode="demo", broker="fake", publish=explode)
    order = manager.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await manager.submit(order, at=T0)
    await manager.flush_events()  # must not raise
    assert order.status is OrderStatus.filled


async def test_a_published_event_says_it_is_a_report(oms: OrderManager, broker: FakeBroker) -> None:
    """A bus that can trigger a trade is a second way to trade."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    for _kind, payload, _order in oms.drain_events():
        assert "nothing may execute from it" in str(payload["advisory"])
        # And nothing that could authorise one is in the payload.
        assert "approval" not in payload
        assert "adapter" not in payload


# ================================================ 29. durable persistence


@pytest.fixture
async def db_session():  # noqa: ANN201
    """An in-memory database with the platform's real schema."""
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401
    from app.db.base import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        from app.models.market import Symbol

        session.add(
            Symbol(
                id="sym-eur",
                code="EURUSD",
                asset_class="fx",
                digits=5,
                point_size=Decimal("0.00001"),
                unit_class="points",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


async def test_the_order_is_on_disk_before_the_venue_is_called(
    db_session, oms: OrderManager
) -> None:
    """§18: never "broker submit, save later". A crash in that window leaves a
    venue holding an order the platform has no record of."""
    from app.models.execution import Order as OrderRow
    from app.oms.repository import OrderRepository

    repo = OrderRepository({"EURUSD": "sym-eur"})
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await repo.persist(db_session, order)
    await db_session.commit()

    row = await db_session.get(OrderRow, order.id)
    assert row is not None
    assert row.status == "intent"
    assert row.intent_id == "i-1"
    assert row.risk_decision_id == order.risk_decision_id
    assert row.filled_quantity == Decimal("0")


async def test_the_audit_trail_records_every_transition_with_its_author(
    db_session, oms: OrderManager, broker: FakeBroker
) -> None:
    """§30. Reconstructible from the sequence is not the same as recorded: a
    reader had to assume no event was missing to know what a transition was
    from."""
    from app.models.execution import OrderEvent
    from app.oms.repository import OrderRepository
    from sqlalchemy import select

    repo = OrderRepository({"EURUSD": "sym-eur"})
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await repo.persist(db_session, order)
    await oms.submit(order, at=T0)
    await repo.persist(db_session, order)
    await db_session.commit()

    events = (
        await db_session.scalars(
            select(OrderEvent).where(OrderEvent.order_id == order.id).order_by(OrderEvent.sequence)
        )
    ).all()
    assert [e.new_status for e in events] == [
        "submitting",
        "submitted",
        "accepted",
        "filled",
    ]
    assert events[0].previous_status == "intent"
    assert {e.source for e in events} == {"pipeline", "broker"}
    # The sequence is what makes the trail readable: all four transitions here
    # share one timestamp, because the submit completed inside one clock
    # reading.
    assert [e.sequence for e in events] == [0, 1, 2, 3]
    assert len({e.occurred_at for e in events}) == 1


async def test_persisting_twice_appends_nothing_new(
    db_session, oms: OrderManager, broker: FakeBroker
) -> None:
    """`persist` is idempotent on the row and append-only on the log, so
    calling it more often than necessary is safe."""
    from app.models.execution import Execution, OrderEvent
    from app.oms.repository import OrderRepository
    from sqlalchemy import func, select

    repo = OrderRepository({"EURUSD": "sym-eur"})
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)
    for _ in range(3):
        await repo.persist(db_session, order)
    await db_session.commit()

    events = await db_session.scalar(
        select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == order.id)
    )
    fills = await db_session.scalar(
        select(func.count()).select_from(Execution).where(Execution.order_id == order.id)
    )
    assert events == 4
    assert fills == 1


async def test_restart_recovery_loads_only_the_unresolved_orders(
    db_session, oms: OrderManager, broker: FakeBroker
) -> None:
    """§39. An order that reached a terminal state is settled; re-examining it
    against the venue would risk changing something already true."""
    from app.oms.repository import OrderRepository

    repo = OrderRepository({"EURUSD": "sym-eur"})

    settled = oms.create(approve(), intent_id="i-done", account_id="acct-a", at=T0).order
    await oms.submit(settled, at=T0)
    await repo.persist(db_session, settled)

    broker.unknown_next = True
    lost = oms.create(approve(), intent_id="i-lost", account_id="acct-a", at=T0).order
    await oms.submit(lost, at=T0)
    await repo.persist(db_session, lost)
    await db_session.commit()

    recovered = await repo.load_unresolved(db_session, mode="demo")
    assert [o.client_order_id for o in recovered] == ["i-lost"]
    assert recovered[0].status is OrderStatus.unknown


async def test_recovery_rebuilds_the_fills_from_the_executions(
    db_session, oms: OrderManager, broker: FakeBroker
) -> None:
    """The derived figures are re-derived on the way back in, so a stored
    figure that disagrees with the record becomes visible rather than
    authoritative."""
    from app.oms.repository import OrderRepository

    repo = OrderRepository({"EURUSD": "sym-eur"})
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")
    oms.apply_execution_report(
        order, quantity=Decimal("0.4"), price=Decimal("1.1"), at=T0, broker_deal_id="d1"
    )
    # Park it so recovery will pick it up.
    order.move(OrderStatus.unknown, at=T0, source="broker", reason="lost the connection")
    await repo.persist(db_session, order)
    await db_session.commit()

    recovered = await repo.load_unresolved(db_session, mode="demo")
    assert len(recovered) == 1
    assert recovered[0].filled_quantity == Decimal("0.4")
    assert recovered[0].remaining_quantity == Decimal("0.6")


async def test_the_database_refuses_a_second_order_for_one_intent(
    db_session, oms: OrderManager
) -> None:
    """The in-memory guard and the unique constraint enforce the same fact, so
    a second process cannot do what a second call cannot."""
    from app.models.execution import Order as OrderRow
    from sqlalchemy.exc import IntegrityError

    db_session.add(
        OrderRow(
            id="o-1",
            intent_id="tv-1",
            mode="demo",
            symbol_id="sym-eur",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
            status="filled",
            source="pipeline",
        )
    )
    await db_session.commit()
    db_session.add(
        OrderRow(
            id="o-2",
            intent_id="tv-1",
            mode="demo",
            symbol_id="sym-eur",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
            status="intent",
            source="pipeline",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


# ================================ L45 C-4: the approval must bind to THIS order


def test_an_approval_cannot_create_an_order_on_a_different_account(
    oms: OrderManager,
) -> None:
    """The defect this file's own fixtures were hiding.

    `_validate_approval` used to ask `approval.binds(approval.bound_fields())`.
    Both sides derived from the same object, so the digest always equalled
    `request_hash` and the check could never fail -- while its message claimed
    to be checking "this order". Every test in this file passed
    `account_id="a"` against an approval issued for `"acct-a"`, and nothing
    noticed for the life of the project.

    Risk evaluates exposure against a specific account's portfolio. An approval
    for one account must not book an order against another.
    """
    approval = approve()
    assert approval.proposal.account_id == "acct-a"
    with pytest.raises(OrderRefused, match="does not bind"):
        oms.create(approval, intent_id="i-evil", account_id="someone-else", at=T0)


def test_an_approval_for_a_market_order_cannot_create_a_limit_order(
    oms: OrderManager,
) -> None:
    """`OrderProposal` carries no order type, so the engine assesses an
    immediate fill at `entry_price`. A limit or stop order has different
    execution semantics and has not been evaluated."""
    with pytest.raises(OrderRefused, match="does not bind"):
        oms.create(approve(), intent_id="i-limit", account_id="acct-a", order_type="limit", at=T0)


def test_the_binding_check_is_capable_of_failing(oms: OrderManager) -> None:
    """The property the original check lacked, asserted directly.

    A safety check that cannot fail is worse than an absent one: it reads as
    protection in every audit. This asserts the check is reachable in the
    negative, independently of any particular field.
    """
    approval = approve()
    assert approval.binds_order(account_id="acct-a", order_type="market", mode="demo") is True
    assert approval.binds_order(account_id="other", order_type="market", mode="demo") is False
    assert approval.binds_order(account_id="acct-a", order_type="limit", mode="demo") is False
    assert approval.binds_order(account_id="acct-a", order_type="market", mode="paper") is False


def test_bound_fields_alone_is_never_a_valid_binding_check() -> None:
    """Guard the shape of the defect, not just this instance of it.

    `binds(bound_fields())` is a tautology. Nothing in the application may ask
    the question that way again.
    """
    approval = approve()
    assert approval.binds(approval.bound_fields()) is True, (
        "if this is ever False the tautology has changed shape; re-read C-4"
    )
    # Matched on the AST, not the text: `oms/service.py` and this file both
    # DISCUSS the tautology in prose, and a substring search calls that a
    # reintroduction.
    root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "binds" or len(node.args) != 1:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Call) and getattr(arg.func, "attr", None) == "bound_fields":
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"vacuous binding check reintroduced at {offenders}"


def test_the_oms_is_the_only_module_that_reaches_a_venue() -> None:
    """The general form of L45 C-2, so the next instance cannot land quietly.

    C-2 was `app/positions/broker_executor.py` calling
    `manager.adapter.close_position(...)` — a second path to the venue, built
    from an object the module had been legitimately handed. No forbidden import
    appears anywhere in it, which is why six structural audits walked past it:
    **structural verification proves a package cannot reach something; it
    cannot prove a package uses correctly what it was given.**

    So this asserts the property directly. Every method by which a
    `BrokerAdapter` transmits an instruction may be called from exactly two
    places: the adapters themselves, and `app/oms/service.py`. Anything else
    is a second execution path, and the second path is always the one nobody
    is watching.

    Read methods are deliberately not listed. Asking the venue what it holds is
    how reconciliation works and must stay available; it is the *writing* that
    belongs to one module.
    """
    import ast
    from pathlib import Path

    #: Every abstract method on `BrokerAdapter` that instructs the venue.
    TRANSMITS = {"place_order", "close_position", "cancel_order", "modify_order"}

    app_dir = Path(__file__).resolve().parents[1] / "app"
    allowed = {("oms", "service.py")}
    offenders: list[str] = []

    for path in app_dir.rglob("*.py"):
        if path.parent.name == "brokers":
            continue  # the adapters ARE the venue boundary
        if (path.parent.name, path.name) in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in TRANSMITS
            ):
                offenders.append(f"{path.relative_to(app_dir)}:{node.lineno} -> {node.func.attr}")

    assert offenders == [], (
        "a module outside the OMS transmits to a venue: "
        + ", ".join(offenders)
        + ". That is a second execution path with no order record and no intent id."
    )


# ================== L70m: which venue identifier a fill is remembered by


async def test_a_fill_is_keyed_by_the_deal_when_the_venue_reports_one(
    oms: OrderManager,
) -> None:
    """`already_recorded` deduplicates by identity, so the identity has to be
    the execution's and not the position's.

    Before this, `_apply_result` keyed every fill by `position_id` -- the same
    value for every fill of one position. Two genuine partial fills of one
    order both carried it, and the book would have discarded the second as a
    replay: the very thing the dedupe docstring says it must not do, since
    "two genuine fills of the same size at the same price are a real thing
    that happens".
    """
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    # Where `submit` leaves an order at the moment the venue answers.
    order.move(OrderStatus.submitting, at=T0, source="pipeline")
    oms._apply_result(  # noqa: SLF001
        order,
        OrderResult(
            BrokerOrderStatus.accepted,
            "filled",
            order_id="ord-1",
            position_id="pos-1",
            deal_id="deal-1",
            fill_price=Decimal("1.1"),
            filled_volume=Decimal("1"),
            fill_source="broker",
        ),
        T0,
    )
    assert [f.broker_deal_id for f in order.book.fills] == ["deal-1"]
    # Still the ORDER's own venue id on the order itself. Different question,
    # different field.
    assert order.broker_order_id == "ord-1"


async def test_two_fills_of_one_order_are_both_kept(oms: OrderManager) -> None:
    """The defect the key change exists to prevent, driven end to end."""
    order = oms.create(
        approve(**{"volume": Decimal("1")}), intent_id="i-1", account_id="acct-a", at=T0
    ).order
    order.move(OrderStatus.submitted, at=T0, source="pipeline")
    order.move(OrderStatus.accepted, at=T0, source="broker")
    for deal in ("deal-1", "deal-2"):
        assert oms.apply_execution_report(
            order,
            quantity=Decimal("0.5"),
            price=Decimal("1.1"),
            at=T0,
            broker_deal_id=deal,
        )
    assert order.filled_quantity == Decimal("1")
    assert len(order.book.fills) == 2


async def test_a_venue_with_no_deal_id_falls_back_and_invents_nothing(
    oms: OrderManager,
) -> None:
    """Not every venue reports a deal. The fallback is coarser and it is still
    something the venue said -- unlike a generated id, which would look unique
    and dedupe nothing."""
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    # Where `submit` leaves an order at the moment the venue answers.
    order.move(OrderStatus.submitting, at=T0, source="pipeline")
    oms._apply_result(  # noqa: SLF001
        order,
        OrderResult(
            BrokerOrderStatus.accepted,
            "filled",
            order_id="ord-1",
            position_id="pos-1",
            fill_price=Decimal("1.1"),
            filled_volume=Decimal("1"),
            fill_source="broker",
        ),
        T0,
    )
    assert [f.broker_deal_id for f in order.book.fills] == ["pos-1"]


# ==================================================== reconciliation, verified
#
# The three behaviours below were changed on 2026-09-18 and none of them was
# covered: `FakeBroker.get_orders` returns `[]` unconditionally, so the branch
# where the venue is HOLDING a working order was unreachable from the suite,
# and `_match_position`'s fallback was never exercised with a known broker id.
# 207 tests passed either way, which is how both defects survived.


async def test_a_working_order_at_the_venue_is_accepted_not_rejected(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """The venue holding an order is not the venue refusing it.

    Reconciliation moved such an order to `rejected`, which is TERMINAL, while
    writing a transition reason that said "the venue holds order X in state Y".
    Both cannot be true, and it fired on exactly the orders reconciliation
    exists to rescue: submitted, unacknowledged, alive at the venue, lost
    locally.
    """
    from app.brokers.base import BrokerOrder

    order = oms.create(approve(), intent_id="i-work", account_id="acct-a", at=T0).order
    await oms.submit(order, at=T0)

    # The venue is working it: an order, no fills, no position. `unknown`
    # because reconcile() only acts on the two uncertain states -- that is
    # the whole point of it.
    order.status = OrderStatus.unknown
    order.book.fills.clear()
    broker._positions.clear()
    working = BrokerOrder(
        order_id=order.broker_order_id or "v-1",
        symbol=order.symbol,
        side=order.side,
        volume=order.quantity,
        price=None,
        state="placed",
        placed_at=T0,
    )
    broker.get_orders = lambda magic=None: _just([working])  # type: ignore[method-assign]

    await oms.reconcile(order, at=T0)

    assert order.status is OrderStatus.accepted, (
        f"a working order became {order.status}; `rejected` is terminal and would "
        f"close an order the venue is still holding"
    )
    assert order.status not in TERMINAL


def _just(value: object):
    """An awaitable returning `value`, for stubbing an async broker method."""

    async def _coro(*_a: object, **_k: object) -> object:
        return value

    return _coro()


def test_a_known_broker_id_that_matches_nothing_does_not_guess(
    oms: OrderManager,
) -> None:
    """The id is the better evidence, and a known id that matches said no.

    The symbol-and-side fallback is documented as used "only when there is no
    id to match on" and sat inside the same loop as the id test, so it ran for
    every candidate regardless. An order WITH a broker id could be handed the
    first position that merely shared its symbol and side.
    """
    from app.brokers.base import BrokerPosition

    order = oms.create(approve(), intent_id="i-id", account_id="acct-a", at=T0).order
    order.broker_order_id = "the-one-i-sent"

    decoy = BrokerPosition(
        position_id="somebody-elses",
        symbol=order.symbol,
        side="long" if order.side == "buy" else "short",
        volume=order.quantity,
        entry_price=Decimal("1"),
        opened_at=T0,
    )

    assert oms._match_position(order, [decoy]) is None, (
        "an order with a known broker id was matched to a position that only "
        "shares its symbol and side"
    )

    # And the id match itself still works, across the whole list.
    mine = BrokerPosition(
        position_id="the-one-i-sent",
        symbol=order.symbol,
        side="long" if order.side == "buy" else "short",
        volume=order.quantity,
        entry_price=Decimal("1"),
        opened_at=T0,
    )
    assert oms._match_position(order, [decoy, mine]) is mine


def test_a_position_this_system_did_not_open_is_never_adopted(
    oms: OrderManager,
) -> None:
    """`tools/` trades the same demo account under a different magic.

    Without an ownership guard the symbol-and-side fallback can attach a
    harness position -- or a hand trade -- to a local intent, after which
    every fill, exit and P&L figure describes somebody else's position.
    """
    from app.brokers.base import BrokerPosition
    from app.brokers.mt5 import MAGIC, TOOLKIT_MAGIC

    order = oms.create(approve(), intent_id="i-magic", account_id="acct-a", at=T0).order
    order.broker_order_id = None  # no id, so the fallback is in play
    wanted = "long" if order.side == "buy" else "short"

    def at(magic: int | None) -> BrokerPosition:
        return BrokerPosition(
            position_id=f"p-{magic}",
            symbol=order.symbol,
            side=wanted,
            volume=order.quantity,
            entry_price=Decimal("1"),
            opened_at=T0,
            magic=magic,
        )

    harness = at(TOOLKIT_MAGIC)
    assert oms._match_position(order, [harness]) is None, (
        "a harness position was adopted into the platform's book"
    )

    # Our own is taken, and a position with no magic still is -- the paper and
    # fake brokers do not set one and this must stay broker-agnostic.
    own = oms._match_position(order, [harness, at(MAGIC)])
    assert own is not None and own.magic == MAGIC
    assert oms._match_position(order, [at(None)]) is not None


# ================== Tier-1 item 3: the venue's terms, checked before any send


@dataclass
class TermsVenue(FakeBroker):
    """A FakeBroker whose contract-terms endpoint can be counted or broken
    independently of its order endpoint, so a test can tell WHICH call an
    order was refused on."""

    reads: int = 0
    terms_down: bool = False

    async def get_symbols(self) -> list[SymbolInfo]:
        self.reads += 1
        if self.terms_down:
            raise NotConnected("the terms endpoint is down; orders still accepted")
        return await super().get_symbols()


async def terms_venue(**overrides: object) -> TermsVenue:
    venue = TermsVenue(mode="demo", **overrides)  # type: ignore[arg-type]
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    venue.symbols["EURUSD"] = SPEC
    return venue


def refused_on_terms(order: ManagedOrder, code: str) -> str:
    """Assert the refusal and hand back its reason, which is never empty."""
    assert order.status is OrderStatus.failed, order.status
    assert order.error_code == code, (order.error_code, order.reject_reason)
    assert order.reject_reason
    return order.reject_reason


async def test_a_volume_off_the_venue_step_is_refused_before_anything_is_sent(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """`validate_order` existed for exactly this and had no caller; a volume
    the venue would refuse was discovered by the venue refusing it. 0.037 is
    a legal risk decision and an illegal MT5 volume at a 0.01 step."""
    order = oms.create(
        approve(volume=Decimal("0.037")), intent_id="i-1", account_id="acct-a", at=T0
    ).order
    out = await oms.submit(order, at=T0)
    reason = refused_on_terms(out, "VENUE_SPEC_REFUSED")
    assert "not a multiple of the step 0.01" in reason
    assert "0.03" in reason, "the nearest legal volume is named, not applied"
    assert broker._orders_placed == 0, "the venue was sent an order it would refuse"
    assert oms.status()["orders_refused_on_venue_spec"] == 1


async def test_a_volume_below_the_venue_minimum_is_refused(
    oms: OrderManager, broker: FakeBroker
) -> None:
    order = oms.create(
        approve(volume=Decimal("0.005")), intent_id="i-1", account_id="acct-a", at=T0
    ).order
    out = await oms.submit(order, at=T0)
    reason = refused_on_terms(out, "VENUE_SPEC_REFUSED")
    assert "below the venue minimum 0.01" in reason
    assert broker._orders_placed == 0


async def test_a_symbol_the_venue_does_not_list_is_refused(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """The venue answered, and the answer did not contain the symbol. That is
    a refusal in its own right, not a missing input to default around."""
    del broker.symbols["EURUSD"]
    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    out = await oms.submit(order, at=T0)
    reason = refused_on_terms(out, "VENUE_SPEC_REFUSED")
    assert "no symbol specification for 'EURUSD'" in reason
    assert broker._orders_placed == 0


async def test_unreadable_venue_terms_refuse_rather_than_guess() -> None:
    """The venue would have TAKEN the order -- `place_order` works -- and it
    is still refused, because the terms could not be read and an order sent
    without them is priced from a guess. Restoring the endpoint is enough:
    no state is left behind by the refusal."""
    venue = await terms_venue(terms_down=True)
    oms = OrderManager(venue, mode="demo", broker="fake")

    order = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    out = await oms.submit(order, at=T0)
    reason = refused_on_terms(out, "VENUE_SPEC_UNREADABLE")
    assert "NotConnected" in reason
    assert "priced from a guess" in reason
    assert venue._orders_placed == 0
    assert oms.status()["orders_refused_on_venue_spec"] == 1

    venue.terms_down = False
    again = oms.create(approve(), intent_id="i-2", account_id="acct-a", at=T0).order
    assert (await oms.submit(again, at=T0)).status is OrderStatus.filled
    assert venue._orders_placed == 1


async def test_a_venue_spec_refusal_never_looks_like_a_send(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """`submitting` is the evidence a send was in flight, and it is what a
    restart reconciles against the venue. A refusal sends nothing, so it
    must leave none of that evidence: `intent -> failed` in one step, and
    not counted among the submitted."""
    order = oms.create(
        approve(volume=Decimal("0.037")), intent_id="i-1", account_id="acct-a", at=T0
    ).order
    out = await oms.submit(order, at=T0)
    assert [(t.previous, t.new) for t in out.transitions] == [
        (OrderStatus.intent, OrderStatus.failed)
    ]
    assert OrderStatus.submitting not in {t.new for t in out.transitions}
    assert out.needs_reconciliation is False
    status = oms.status()
    assert status["orders_submitted"] == 0
    assert status["orders_failed"] == 1
    assert status["unresolved"] == []


async def test_a_venue_spec_refusal_leaves_the_intent_free_to_resend(
    oms: OrderManager, broker: FakeBroker
) -> None:
    """A refused-on-terms order is the one kind of failure a corrected order
    may follow: nothing reached the venue, so a resend cannot make two."""
    order = oms.create(
        approve(volume=Decimal("0.037")), intent_id="i-1", account_id="acct-a", at=T0
    ).order
    await oms.submit(order, at=T0)
    assert can_resend(order.status)
    oms.guard_resend("i-1")  # would raise for `unknown` or `submitting`

    corrected = oms.create(
        approve(volume=Decimal("0.03")), intent_id="i-1-corrected", account_id="acct-a", at=T0
    ).order
    assert (await oms.submit(corrected, at=T0)).status is OrderStatus.filled
    assert broker._orders_placed == 1


async def test_the_venue_terms_are_read_once_per_cache_window() -> None:
    """One `get_symbols` per window, not one per order: the read is a venue
    round trip on the critical path of every send."""
    venue = await terms_venue()
    oms = OrderManager(venue, mode="demo", broker="fake")
    assert oms.spec_cache_seconds == 300.0

    for n, at in enumerate((T0, T0 + timedelta(seconds=60), T0 + timedelta(seconds=299))):
        order = oms.create(approve(at=at), intent_id=f"i-{n}", account_id="acct-a", at=at).order
        assert (await oms.submit(order, at=at)).status is OrderStatus.filled
    assert venue.reads == 1

    late = T0 + timedelta(seconds=300)
    order = oms.create(approve(at=late), intent_id="i-late", account_id="acct-a", at=late).order
    assert (await oms.submit(order, at=late)).status is OrderStatus.filled
    assert venue.reads == 2


async def test_a_changed_venue_step_is_honoured_once_the_cache_expires() -> None:
    """The authority is the venue NOW, within the cache window. A step the
    venue widens is enforced at the next read; inside the window the old
    terms still apply, which is the trade-off the window buys and the reason
    it is five minutes rather than an hour."""
    venue = await terms_venue()
    oms = OrderManager(venue, mode="demo", broker="fake")

    first = oms.create(
        approve(volume=Decimal("0.03")), intent_id="i-1", account_id="acct-a", at=T0
    ).order
    assert (await oms.submit(first, at=T0)).status is OrderStatus.filled

    venue.symbols["EURUSD"] = replace(SPEC, volume_step=Decimal("0.05"))

    inside = T0 + timedelta(seconds=120)
    second = oms.create(
        approve(at=inside, volume=Decimal("0.03")), intent_id="i-2", account_id="acct-a", at=inside
    ).order
    assert (await oms.submit(second, at=inside)).status is OrderStatus.filled, (
        "the cached terms apply inside the window"
    )

    after = T0 + timedelta(seconds=301)
    third = oms.create(
        approve(at=after, volume=Decimal("0.03")), intent_id="i-3", account_id="acct-a", at=after
    ).order
    out = await oms.submit(third, at=after)
    reason = refused_on_terms(out, "VENUE_SPEC_REFUSED")
    assert "step 0.05" in reason
    assert venue._orders_placed == 2


async def test_a_symbol_missing_from_the_cache_forces_a_fresh_read() -> None:
    """The venue can list an instrument after the cache was built. Answering
    "no spec" from the stale map would refuse an order the venue would take,
    so an absent symbol is one forced refresh, not a refusal."""
    venue = await terms_venue()
    oms = OrderManager(venue, mode="demo", broker="fake")

    first = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    assert (await oms.submit(first, at=T0)).status is OrderStatus.filled
    assert venue.reads == 1

    venue.set_quote("GBPUSD", "1.30000", "1.30002")
    venue.symbols["GBPUSD"] = replace(SPEC, symbol="GBPUSD")
    soon = T0 + timedelta(seconds=1)
    cable = oms.create(
        approve(
            symbol="GBPUSD",
            entry_price=Decimal("1.3000"),
            stop_loss=Decimal("1.2980"),
            take_profit=Decimal("1.3040"),
        ),
        intent_id="i-2",
        account_id="acct-a",
        at=soon,
    ).order
    assert (await oms.submit(cable, at=soon)).status is OrderStatus.filled
    assert venue.reads == 2, "the miss forced one refresh"

    # And a symbol that is STILL absent after the refresh is refused, having
    # cost exactly one more read rather than one per attempt. The SECOND
    # attempt is the one that matters: `symbol not in self._specs` is true
    # forever for a symbol the venue does not list, so without a memo of the
    # miss every signal for it re-reads the whole symbol table.
    venue.set_quote("USDJPY", "150.000", "150.002")
    yen = oms.create(
        approve(
            symbol="USDJPY",
            entry_price=Decimal("150.000"),
            stop_loss=Decimal("149.800"),
            take_profit=Decimal("150.400"),
        ),
        intent_id="i-3",
        account_id="acct-a",
        at=soon,
    ).order
    out = await oms.submit(yen, at=soon)
    reason = refused_on_terms(out, "VENUE_SPEC_REFUSED")
    assert "no symbol specification for 'USDJPY'" in reason
    assert venue.reads == 3

    again = oms.create(
        approve(
            symbol="USDJPY",
            entry_price=Decimal("150.000"),
            stop_loss=Decimal("149.800"),
            take_profit=Decimal("150.400"),
        ),
        intent_id="i-4",
        account_id="acct-a",
        at=soon,
    ).order
    refused_on_terms(await oms.submit(again, at=soon), "VENUE_SPEC_REFUSED")
    assert venue.reads == 3, "a symbol the venue does not list was re-read per attempt"

    # The memo lasts exactly as long as the snapshot it describes. Once the
    # window expires the venue is asked again, because it may list it now.
    later = T0 + timedelta(seconds=301)
    third = oms.create(
        approve(
            at=later,
            symbol="USDJPY",
            entry_price=Decimal("150.000"),
            stop_loss=Decimal("149.800"),
            take_profit=Decimal("150.400"),
        ),
        intent_id="i-5",
        account_id="acct-a",
        at=later,
    ).order
    refused_on_terms(await oms.submit(third, at=later), "VENUE_SPEC_REFUSED")
    assert venue.reads == 4
    assert venue._orders_placed == 2


async def test_the_status_report_separates_terms_refusals_from_sends(
    oms: OrderManager, broker: FakeBroker
) -> None:
    good = oms.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    bad = oms.create(
        approve(volume=Decimal("0.037")), intent_id="i-2", account_id="acct-a", at=T0
    ).order
    await oms.submit(good, at=T0)
    await oms.submit(bad, at=T0)
    status = oms.status()
    assert status["orders_refused_on_venue_spec"] == 1
    assert status["orders_submitted"] == 1
    assert status["orders_filled"] == 1
    assert status["orders_failed"] == 1
    assert broker._orders_placed == 1


# ================= Tier-1 item 4: the reconcile sweep, on a loop at last


class RecordingStore:
    """An `OrderStore` that remembers what it was asked to write.

    It implements the whole protocol -- `prior_order` and `record` -- so it
    satisfies `OrderStore` structurally rather than by being cast to it.
    """

    def __init__(self, *, refuse: bool = False) -> None:
        self.recorded: list[ManagedOrder] = []
        self.refuse = refuse

    async def prior_order(self, intent_id: str) -> PriorOrder | None:
        return None

    async def record(self, order: ManagedOrder) -> None:
        if self.refuse:
            raise OrderNotRecorded(f"symbol {order.symbol!r} does not resolve")
        self.recorded.append(order)


@dataclass
class SweepVenue(FakeBroker):
    """Counts the venue reads a reconciliation costs."""

    order_reads: int = 0

    async def get_orders(self, magic: int | None = None) -> list[BrokerOrder]:
        self.order_reads += 1
        return await super().get_orders(magic)


async def sweep_venue() -> SweepVenue:
    venue = SweepVenue(mode="demo")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    venue.symbols["EURUSD"] = SPEC
    return venue


async def park_unknown(
    registry: OrderManagerRegistry, venue: FakeBroker, *, account: str, intent: str
) -> ManagedOrder:
    """One order the venue never answered for, in `unknown`, in memory."""
    manager = registry.managers.get(account) or registry.register(
        account, venue, mode="demo", broker="fake"
    )
    venue.unknown_next = True
    # The approval has to name the SAME account the order is created for:
    # `account_id` is one of the fields `request_hash` binds over, so an
    # approval issued for another account does not bind to this order.
    order = manager.create(
        approve(account_id=account), intent_id=intent, account_id=account, at=T0
    ).order
    await manager.submit(order, at=T0)
    assert order.status is OrderStatus.unknown
    return order


def sweeper(
    registry: OrderManagerRegistry, store: RecordingStore, *, max_per_pass: int = 10
) -> OmsReconcileWorker:
    return OmsReconcileWorker(registry, store, interval_seconds=0.01, max_per_pass=max_per_pass)


async def test_the_sweep_settles_an_unresolved_order_without_a_human() -> None:
    """The defect: only `POST /v1/orders/{id}/reconcile` ever did this."""
    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    order = await park_unknown(registry, venue, account="acct-a", intent="i-1")
    assert registry.unresolved() == {"acct-a": [order.id]}

    store = RecordingStore()
    worker = sweeper(registry, store)
    assert await worker.run(max_passes=1) == 1

    assert order.status is OrderStatus.failed
    assert order.error_code == "NOT_AT_VENUE"
    assert can_resend(order.status)
    assert store.recorded == [order], "the settled order was not written down"
    assert registry.unresolved() == {}
    assert worker.status.failures == 0
    assert worker.report()["last_pass"] == {
        "settled": 1,
        "unreadable": 0,
        "unrecorded": 0,
        "faulted": 0,
        "gone": 0,
        "deferred": 0,
    }


async def test_the_sweep_records_a_fill_the_venue_turned_out_to_hold() -> None:
    """The other exit from `unknown`: the venue holds the position, the
    answer went missing. The sweep records `filled`, and the store sees an
    order carrying its fill -- the shape `record_fill` turns into a Position."""
    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    manager = registry.register("acct-a", venue, mode="demo", broker="fake")
    order = manager.create(approve(), intent_id="i-1", account_id="acct-a", at=T0).order
    await manager.submit(order, at=T0)
    assert order.status is OrderStatus.filled
    order.status = OrderStatus.unknown
    order.book.fills.clear()
    assert registry.unresolved() == {"acct-a": [order.id]}

    store = RecordingStore()
    await sweeper(registry, store).run(max_passes=1)
    assert order.status is OrderStatus.filled
    assert order.filled_quantity == Decimal("1")
    assert store.recorded == [order]


async def test_a_sweep_that_cannot_read_the_venue_leaves_the_order_unknown() -> None:
    """Fail closed, twice over: an unreachable venue is not a conclusion, and
    it is not a clean pass either."""
    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    order = await park_unknown(registry, venue, account="acct-a", intent="i-1")
    await venue.disconnect()

    store = RecordingStore()
    worker = sweeper(registry, store)
    await worker.run(max_passes=1)

    assert order.status is OrderStatus.unknown
    assert registry.managers["acct-a"].status()["reconciliation_failures"] == 1
    assert store.recorded == []
    assert worker.status.failures == 0, "an unreadable venue is a condition, not a loop fault"
    assert worker.last_pass["unreadable"] == 1
    assert worker.last_pass["settled"] == 0
    assert registry.unresolved() == {"acct-a": [order.id]}


async def test_one_unreadable_account_does_not_stop_the_sweep_reaching_the_others() -> None:
    down, up = await sweep_venue(), await sweep_venue()
    registry = OrderManagerRegistry()
    stuck = await park_unknown(registry, down, account="acct-down", intent="i-1")
    free = await park_unknown(registry, up, account="acct-up", intent="i-2")
    await down.disconnect()

    worker = sweeper(registry, RecordingStore())
    await worker.run(max_passes=1)

    assert stuck.status is OrderStatus.unknown
    assert free.status is OrderStatus.failed
    last = worker.last_pass
    assert (last["settled"], last["unreadable"]) == (1, 1)


async def test_the_sweep_takes_the_account_lock_so_it_cannot_race_a_submission() -> None:
    """The pipeline and the manual route hold `registry.lock(account)` across
    create, persist and submit. A sweep that did not would see an order in
    flight, find nothing at the venue yet, and write `failed` -- licensing a
    second send for the same intent. While the lock is held the pass makes
    no progress; when it is released, the order is settled."""
    # THE CONTROL FIRST, so the assertion below cannot pass vacuously. An
    # unlocked account settles well inside the window the locked one is
    # given, which is what makes "it had not settled yet" evidence of the
    # lock rather than evidence of a slow machine.
    free_venue = await sweep_venue()
    free = OrderManagerRegistry()
    quick = await park_unknown(free, free_venue, account="acct-a", intent="i-1")
    await asyncio.wait_for(sweeper(free, RecordingStore()).run(max_passes=1), timeout=0.05)
    assert quick.status is OrderStatus.failed

    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    order = await park_unknown(registry, venue, account="acct-a", intent="i-1")
    worker = sweeper(registry, RecordingStore())

    lock = registry.lock("acct-a")
    await lock.acquire()
    try:
        task = asyncio.create_task(worker.run(max_passes=1))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert order.status is OrderStatus.unknown, "settled while the account lock was held"
        assert venue.order_reads == 0, "the venue was read before the lock was taken"
        assert worker.status.passes == 0
    finally:
        lock.release()
    await asyncio.wait_for(task, timeout=2.0)
    assert order.status is OrderStatus.failed
    assert venue.order_reads == 1


async def test_the_sweep_never_submits_closes_cancels_or_modifies() -> None:
    """Asserted on the module's source AND on a live venue."""
    package = Path(__file__).resolve().parents[1] / "app" / "oms"
    tree = ast.parse((package / "worker.py").read_text(encoding="utf-8"))
    forbidden = {
        "submit",
        "close",
        "cancel",
        "modify",
        "place_order",
        "close_position",
        "modify_order",
        "cancel_order",
    }
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & forbidden), called & forbidden
    assert "reconcile" in called

    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    await park_unknown(registry, venue, account="acct-a", intent="i-1")
    assert venue._orders_placed == 0
    await sweeper(registry, RecordingStore()).run(max_passes=1)
    assert venue._orders_placed == 0
    assert venue.order_reads == 1


async def test_a_reconciled_order_is_persisted_before_its_events_are_published() -> None:
    """Ordering is the property `drain_events` exists for: an event must
    never describe something that was not saved."""
    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    store = RecordingStore()
    recorded_when_published: list[int] = []

    async def sink(event_type: str, payload: dict[str, object], order: ManagedOrder) -> None:
        recorded_when_published.append(len(store.recorded))

    registry.register("acct-a", venue, mode="demo", broker="fake", publish=sink)
    order = await park_unknown(registry, venue, account="acct-a", intent="i-1")
    # The submission's own events are flushed here so the sweep's are the
    # only ones the sink sees.
    await registry.managers["acct-a"].flush_events()
    recorded_when_published.clear()

    await sweeper(registry, store).run(max_passes=1)
    assert order.status is OrderStatus.failed
    assert recorded_when_published, "the settlement published no event"
    assert all(n >= 1 for n in recorded_when_published), recorded_when_published


async def test_a_reconciled_order_the_store_refuses_is_counted_not_hidden() -> None:
    """`record` refuses when the symbol will not resolve. The order already
    exists at a venue, so the refusal is reported and the sweep goes on --
    it is not a clean pass and not a loop failure."""
    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    order = await park_unknown(registry, venue, account="acct-a", intent="i-1")
    worker = sweeper(registry, RecordingStore(refuse=True))
    await worker.run(max_passes=1)
    assert order.status is OrderStatus.failed
    assert worker.status.failures == 0
    assert worker.last_pass["unrecorded"] == 1


async def test_the_sweep_is_bounded_per_pass_and_reports_what_it_deferred() -> None:
    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    orders = [
        await park_unknown(registry, venue, account="acct-a", intent=f"i-{n}") for n in range(12)
    ]
    worker = sweeper(registry, RecordingStore(), max_per_pass=10)

    await worker.tick()
    assert venue.order_reads == 10, "each settlement is one venue read pair; ten were budgeted"
    assert sum(o.status is OrderStatus.failed for o in orders) == 10
    assert worker.last_pass["settled"] == 10
    assert worker.last_pass["deferred"] == 2

    await worker.tick()
    assert venue.order_reads == 12
    assert all(o.status is OrderStatus.failed for o in orders)
    assert worker.last_pass == {
        "settled": 2,
        "unreadable": 0,
        "unrecorded": 0,
        "faulted": 0,
        "gone": 0,
        "deferred": 0,
    }


async def test_one_order_that_raises_does_not_starve_the_rest_forever() -> None:
    """`Worker.run` survives a failing tick, which is not enough here: a
    single order raising something unexpected would abort every pass at the
    same place, so every OTHER unresolved order would stay unresolved for
    good. It is counted rather than swallowed -- `faulted` is the difference
    between nothing to settle and something that cannot be settled."""
    venue = await sweep_venue()
    registry = OrderManagerRegistry()
    bad = await park_unknown(registry, venue, account="acct-a", intent="i-1")
    good = await park_unknown(registry, venue, account="acct-a", intent="i-2")

    manager = registry.managers["acct-a"]
    real = manager.reconcile

    async def explode(order: ManagedOrder, **kw: object) -> ManagedOrder:
        if order.id == bad.id:
            raise RuntimeError("something nobody anticipated")
        return await real(order, **kw)  # type: ignore[arg-type]

    manager.reconcile = explode  # type: ignore[method-assign]

    worker = sweeper(registry, RecordingStore())
    await worker.run(max_passes=1)

    assert worker.status.failures == 0, "the pass itself must not fail"
    assert worker.last_pass["faulted"] == 1
    assert worker.last_pass["settled"] == 1
    assert good.status is OrderStatus.failed, "a healthy order was starved by a broken one"
    assert bad.status is OrderStatus.unknown


async def test_an_empty_registry_is_a_clean_sweep_not_a_failure() -> None:
    """The default deployment: no adapter registered, nothing to settle."""
    worker = sweeper(OrderManagerRegistry(), RecordingStore())
    assert await worker.run(max_passes=1) == 1
    assert worker.status.failures == 0
    assert worker.last_pass["settled"] == 0
    assert worker.report()["unresolved"] == {}


def test_the_sweep_reports_that_it_only_sees_in_memory_orders() -> None:
    """Honesty over a silent default: an order left by a PREVIOUS process is
    not swept, and the report says so rather than looking complete."""
    report = sweeper(OrderManagerRegistry(), RecordingStore()).report()
    assert "resume" in str(report["scope"])
    assert "load_unresolved" in str(report["scope"])
    assert report["worker"] == "oms_reconcile"
    supervisor = report["supervisor"]
    assert isinstance(supervisor, dict) and supervisor["running"] is False
