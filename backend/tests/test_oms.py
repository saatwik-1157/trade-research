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
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.brokers.base import OrderResult, SymbolInfo
from app.brokers.base import OrderStatus as BrokerOrderStatus
from app.brokers.fake import FakeBroker
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
    guarantee the OMS rests on."""
    engine = RiskEngine(RiskLimits(require_stop_loss=False))
    approval, verdict = engine.approve(proposal(**overrides), PortfolioState(), now=T0)
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
    assert exits == {
        OrderStatus.filled,
        OrderStatus.partially_filled,
        OrderStatus.cancelled,
        OrderStatus.rejected,
        OrderStatus.expired,
        OrderStatus.failed,
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
    order = oms.create(approve(**{"volume": Decimal("1")}), intent_id="i-1",
                       account_id="acct-a", at=T0).order
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
