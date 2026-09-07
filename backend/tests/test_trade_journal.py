"""The trade journal and trade lifecycle history (L31).

The ones that matter most, and they are the rules the brief calls strict:

  * `test_a_duplicate_close_does_not_create_a_second_trade` — §27 and §50.
  * `test_partial_closes_weight_the_exit_and_do_not_double_count` — §14.
  * `test_a_position_the_venue_never_confirmed_is_not_closed` — §15 and §50.
  * `test_internal_closed_against_broker_open_is_reconciliation_required` — §28.
  * `test_the_exact_model_version_is_recorded_never_latest` — §9.
  * `test_historical_risk_context_is_read_never_recomputed` — §10.
  * `test_the_journal_cannot_place_or_modify_anything` — §55.
  * `test_no_webhook_secret_reaches_the_timeline` — §45.
  * `test_nothing_is_invented_when_no_close_was_confirmed` — §51.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.db.base import Base
from app.journal import closes as close_engine
from app.journal import context as context_engine
from app.journal import quality as quality_engine
from app.journal import timeline as timeline_engine
from app.journal.lifecycle import EXIT_REASONS, Holding, JournalExit, TradeStatus, exit_reason_of
from app.journal.service import RECORDABLE, TradeJournalService
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.ai_integration import AiDecisionRecord
from app.models.execution import (
    TRADE_STATUSES,
    Execution,
    Order,
    OrderEvent,
    Position,
    PositionEvent,
    Trade,
)
from app.models.market import Symbol
from app.models.risk import RiskEvent
from app.models.signals import Signal, WebhookEvent
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

JOURNAL = Path(__file__).resolve().parents[1] / "app" / "journal"
ROUTER = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "trades.py"
QUERIES = Path(__file__).resolve().parents[1] / "app" / "services" / "journal.py"

#: The export header, imported so a column rename breaks the test that
#: asserts the empty export still emits it.
from app.api.v1.trades import EXPORT_COLUMNS as EXPORT_HEADER  # noqa: E402

NOW = datetime(2026, 9, 4, 12, 0, 0)
SECRET = "tv-shared-secret-do-not-leak"


# ================================================================= fixtures


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        from app.auth.models import User

        session.add(User(id="u1", email="a@b.io", password_hash="x", role="admin"))
        session.add(
            PaperAccount(
                id="paper1",
                user_id="u1",
                name="paper",
                currency="USD",
                starting_balance=Decimal("100000"),
                balance=Decimal("100000"),
                equity=Decimal("100000"),
                status="active",
            )
        )
        session.add(
            BrokerAccount(
                id="broker1",
                user_id="u1",
                name="demo",
                broker="mt5",
                account_mode="demo",
                currency="USD",
            )
        )
        session.add(Symbol(id="sym1", code="EURUSD", asset_class="fx"))
        await session.commit()
    yield factory
    await engine.dispose()


async def make_position(
    db: AsyncSession,
    *,
    mode: str = "demo",
    account: str = "broker1",
    status: str = "closed",
    quantity: Decimal = Decimal("0"),
    initial: Decimal = Decimal("1"),
    closed_quantity: Decimal = Decimal("1"),
    realized: Decimal | None = Decimal("100"),
    entry: Decimal = Decimal("1.1000"),
    broker_position_id: str | None = "bp-1",
    strategy: str | None = None,
) -> Position:
    row = Position(
        mode=mode,
        broker_account_id=account if mode != "paper" else None,
        paper_account_id=account if mode == "paper" else None,
        symbol_id="sym1",
        strategy_version_id=strategy,
        broker_position_id=broker_position_id,
        side="long",
        quantity=quantity,
        initial_quantity=initial,
        closed_quantity=closed_quantity,
        entry_price=entry,
        realized_pnl=realized,
        status=status,
        opened_at=NOW - timedelta(hours=2),
        closed_at=NOW if status == "closed" else None,
    )
    db.add(row)
    await db.flush()
    return row


async def add_close(
    db: AsyncSession,
    position: Position,
    *,
    at: datetime,
    price: str,
    quantity: str,
    reason: str = "take_profit",
    running: str | None = None,
    kind: str = "closed",
    commission: str | None = None,
    swap: str | None = None,
) -> PositionEvent:
    payload: dict[str, Any] = {
        "reason": reason,
        "fill_price": price,
        "closed_quantity": quantity,
        "realized_pnl": running,
        "broker_deal_id": f"deal-{at.isoformat()}",
        "fill_source": "broker",
    }
    if commission is not None:
        payload["commission"] = commission
    if swap is not None:
        payload["swap"] = swap
    event = PositionEvent(position_id=position.id, event_type=kind, occurred_at=at, payload=payload)
    db.add(event)
    await db.flush()
    return event


async def make_order(
    db: AsyncSession,
    *,
    signal_id: str | None = None,
    requested: Decimal | None = Decimal("1.1000"),
    sizing: dict[str, Any] | None = None,
) -> Order:
    row = Order(
        intent_id=f"intent-{signal_id or 'x'}",
        mode="demo",
        broker_account_id="broker1",
        symbol_id="sym1",
        signal_id=signal_id,
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        requested_price=requested,
        status="filled",
        sizing=sizing,
        source="pipeline",
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(hours=3),
    )
    db.add(row)
    await db.flush()
    return row


# ================================================= order vs trade vs position


def test_the_status_vocabulary_matches_the_check_constraint() -> None:
    """A state the code can produce and the schema rejects is a runtime error."""
    assert tuple(str(s) for s in TradeStatus) == TRADE_STATUSES


def test_a_cancelled_order_is_not_a_trade_status() -> None:
    """§4 and §25. An order that never filled never opened a position."""
    assert "cancelled" not in TRADE_STATUSES
    assert "rejected" not in TRADE_STATUSES


def test_only_a_finished_episode_is_recordable() -> None:
    """§15 and §50. `open` and `closing` are deliberately absent."""
    assert RECORDABLE == frozenset({"closed", "unknown"})
    assert "open" not in RECORDABLE
    assert "partially_closed" not in RECORDABLE


def test_the_exit_vocabulary_is_l21s_plus_three() -> None:
    """§17. Not a second spelling of the same eight reasons."""
    from app.positions.policies import ExitReason

    for reason in ExitReason:
        assert str(reason) in EXIT_REASONS
    assert {"manual_exit", "broker_exit", "unknown"} <= EXIT_REASONS


def test_an_unrecognised_exit_reason_becomes_unknown_not_the_nearest_match() -> None:
    assert exit_reason_of("stop_loss") == "stop_loss"
    assert exit_reason_of("stopped out") == "unknown"
    assert exit_reason_of(None) == "unknown"
    assert exit_reason_of("") == str(JournalExit.unknown)


# ======================================================== partial fills, §13


async def test_the_entry_price_is_read_not_recomputed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§13 and §31. L21 already weights the entry across fills."""
    async with sessions() as db:
        # 0.40 at 1.1000 and 0.60 at 1.1050 is 1.1030, and L21 stored it.
        position = await make_position(db, entry=Decimal("1.1030"))
        await add_close(db, position, at=NOW, price="1.2000", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
    assert recorded.created is True
    assert recorded.trade is not None
    assert recorded.trade.entry_price == Decimal("1.1030")


# ======================================================= partial closes, §14


def test_the_exit_is_the_volume_weighted_average_of_the_closes() -> None:
    parts = [
        close_engine.Close(
            at=NOW,
            quantity=Decimal("0.4"),
            fill_price=Decimal("1.2000"),
            reason="partial_take_profit",
            realized_running=Decimal("40"),
            broker_deal_id="d1",
            fill_source="broker",
        ),
        close_engine.Close(
            at=NOW + timedelta(minutes=5),
            quantity=Decimal("0.6"),
            fill_price=Decimal("1.1500"),
            reason="stop_loss",
            realized_running=Decimal("70"),
            broker_deal_id="d2",
            fill_source="broker",
        ),
    ]
    found = close_engine.exit_of(parts)
    assert found.quantity == Decimal("1.0")
    # (1.2 * 0.4 + 1.15 * 0.6) / 1.0
    assert found.price == Decimal("1.17")
    assert found.closes == 2


def test_the_exit_reason_is_the_last_close_not_the_first() -> None:
    """A scale-out at a target then a stop on the rest exited at the STOP."""
    parts = [
        close_engine.Close(
            NOW, Decimal("0.4"), Decimal("1.2"), "partial_take_profit", None, None, None
        ),
        close_engine.Close(
            NOW + timedelta(minutes=5),
            Decimal("0.6"),
            Decimal("1.1"),
            "stop_loss",
            None,
            None,
            None,
        ),
    ]
    assert close_engine.exit_of(parts).reason == "stop_loss"


def test_realized_prefers_the_booked_figure_over_any_derivation() -> None:
    """§18. `positions.realized_pnl` is what the account actually received."""
    parts = [
        close_engine.Close(
            NOW, Decimal("1"), Decimal("1.2"), "take_profit", Decimal("95"), None, None
        )
    ]
    value, source = close_engine.realized_from(parts, Decimal("100"))
    assert value == Decimal("100")
    assert "positions.realized_pnl" in source


def test_realized_falls_back_to_the_last_running_total_not_a_sum() -> None:
    """Summing the running totals would count every earlier close again."""
    parts = [
        close_engine.Close(NOW, Decimal("0.4"), Decimal("1.2"), "x", Decimal("40"), None, None),
        close_engine.Close(
            NOW + timedelta(minutes=1),
            Decimal("0.6"),
            Decimal("1.15"),
            "y",
            Decimal("70"),
            None,
            None,
        ),
    ]
    value, _source = close_engine.realized_from(parts, None)
    assert value == Decimal("70")


def test_a_missing_realized_figure_is_none_not_zero() -> None:
    value, source = close_engine.realized_from([], None)
    assert value is None
    assert "NOT zero" in source


async def test_partial_closes_weight_the_exit_and_do_not_double_count(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§14 end to end. One trade, weighted exit, P&L booked once."""
    async with sessions() as db:
        position = await make_position(
            db, initial=Decimal("1"), closed_quantity=Decimal("1"), realized=Decimal("120")
        )
        await add_close(
            db,
            position,
            at=NOW - timedelta(minutes=10),
            price="1.2000",
            quantity="0.4",
            reason="partial_take_profit",
            running="60",
            kind="partially_closed",
        )
        await add_close(db, position, at=NOW, price="1.1500", quantity="0.6", running="120")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)

    assert recorded.trade is not None
    trade = recorded.trade
    assert trade.volume == Decimal("1.0")
    assert trade.exit_price == Decimal("1.17")
    # The booked total, ONCE. Not 60 + 120, and not recomputed from the average.
    assert trade.net_profit == Decimal("120")
    assert trade.exit_reason == "take_profit"


async def test_a_close_with_no_fill_price_is_skipped_not_defaulted(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§51. A close valued at a price nobody reported is a fabrication."""
    async with sessions() as db:
        position = await make_position(db)
        db.add(
            PositionEvent(
                position_id=position.id,
                event_type="closed",
                occurred_at=NOW,
                payload={"reason": "stop_loss", "closed_quantity": "1", "fill_price": None},
            )
        )
        await db.flush()
        events = list(
            (
                await db.scalars(
                    select(PositionEvent).where(PositionEvent.position_id == position.id)
                )
            ).all()
        )
    assert close_engine.closes_from(events) == []


# ============================================================= idempotency


async def test_a_duplicate_close_does_not_create_a_second_trade(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§27. The core failure test of the level."""
    service = TradeJournalService()
    async with sessions() as db:
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        first = await service.record_close(db, position)
        await db.commit()
        second = await service.record_close(db, position)
        await db.commit()
        rows = list((await db.scalars(select(Trade))).all())
    assert first.created is True
    assert second.created is False
    assert second.trade is not None
    assert second.trade.id == first.trade.id  # type: ignore[union-attr]
    assert len(rows) == 1


async def test_the_database_refuses_a_second_row_for_one_position(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The guarantee is in the schema, not only in the service."""
    async with sessions() as db:
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        await TradeJournalService().record_close(db, position)
        await db.commit()
        db.add(
            Trade(
                mode="demo",
                position_id=position.id,
                symbol_id="sym1",
                side="long",
                volume=Decimal("1"),
                entry_price=Decimal("1.1"),
                exit_price=Decimal("1.2"),
                opened_at=NOW,
                closed_at=NOW,
                gross_profit=Decimal("1"),
                net_profit=Decimal("1"),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_imported_rows_are_not_collapsed_by_the_unique_index(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """NULLs are DISTINCT, and here that is the WANTED behaviour."""
    async with sessions() as db:
        for index in range(3):
            db.add(
                Trade(
                    mode="demo",
                    position_id=None,
                    symbol_id="sym1",
                    side="long",
                    volume=Decimal("0.01"),
                    entry_price=Decimal("1.1"),
                    exit_price=Decimal("1.2"),
                    opened_at=NOW - timedelta(hours=index),
                    closed_at=NOW,
                    gross_profit=Decimal("1"),
                    net_profit=Decimal("1"),
                    source="jsonl_import",
                )
            )
        await db.commit()
        rows = list((await db.scalars(select(Trade))).all())
    assert len(rows) == 3


async def test_the_sweep_records_every_finished_position_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§40. A sweep cannot miss a close site the way a hook can."""
    service = TradeJournalService()
    async with sessions() as db:
        for index in range(3):
            position = await make_position(db, broker_position_id=f"bp-{index}")
            await add_close(
                db,
                position,
                at=NOW + timedelta(minutes=index),
                price="1.2",
                quantity="1",
                running="100",
            )
        await db.commit()
        first = await service.record_pending(db)
        await db.commit()
        second = await service.record_pending(db)
        await db.commit()
        rows = list((await db.scalars(select(Trade))).all())
    assert sum(1 for r in first if r.created) == 3
    assert second == []
    assert len(rows) == 3


# ===================================================== never finalise early


async def test_an_open_position_produces_no_journal_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        position = await make_position(
            db, status="open", quantity=Decimal("1"), closed_quantity=Decimal("0")
        )
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
    assert recorded.created is False
    assert recorded.trade is None
    assert "episode has ended" in recorded.reason


async def test_a_position_the_venue_never_confirmed_is_not_closed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§50. `unknown` is recorded as `unknown`, never promoted to `closed`."""
    async with sessions() as db:
        position = await make_position(db, status="unknown")
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
    assert recorded.trade is not None
    assert recorded.trade.status == str(TradeStatus.unknown)
    assert recorded.trade.status != str(TradeStatus.closed)


async def test_nothing_is_invented_when_no_close_was_confirmed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§51. No exit price and no booked P&L means no row, not a guessed one."""
    async with sessions() as db:
        position = await make_position(db, realized=None)
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
        rows = list((await db.scalars(select(Trade))).all())
    assert recorded.created is False
    assert "substituted values" in recorded.reason
    assert rows == []


async def test_internal_closed_against_broker_open_is_reconciliation_required(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§28 and §50. The disagreement is recorded; nothing is resolved."""
    service = TradeJournalService()
    async with sessions() as db:
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await service.record_close(db, position)
        await db.commit()
        trade = recorded.trade
        assert trade is not None
        before = (trade.exit_price, trade.net_profit, trade.volume)
        await service.mark_reconciliation_required(db, trade, detail="the venue still holds bp-1")
        await db.commit()
    assert trade.status == str(TradeStatus.reconciliation_required)
    # §32. Only the status and the quality block changed.
    assert (trade.exit_price, trade.net_profit, trade.volume) == before
    assert trade.data_quality is not None
    assert trade.data_quality["reconciliation_detail"] == "the venue still holds bp-1"
    assert any(f["code"] == "reconciliation_required" for f in trade.data_quality["findings"])


# ============================================================== attribution


async def test_the_exact_model_version_is_recorded_never_latest(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§9. A review that cannot say which weights decided cannot review."""
    async with sessions() as db:
        order = await make_order(db)
        decision = AiDecisionRecord(
            id="ai1",
            strategy_key="breakout",
            symbol="EURUSD",
            timeframe="H1",
            bar_time=NOW - timedelta(hours=3),
            side="long",
            model_key="trade_probability",
            model_version="2.3",
            model_version_id="mv-2-3",
            mode="AI_FILTER",
            policy="AI_OPTIONAL",
            decision="ACCEPT",
            status="ok",
            probability=Decimal("0.78"),
            regime="TRENDING",
            order_id=order.id,
            bot_id="bot1",
        )
        db.add(decision)
        position = await make_position(db, strategy="sv-1")
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
        await db.commit()
        context = await TradeJournalService().context_for(db, recorded.trade)  # type: ignore[arg-type]

    assert recorded.trade is not None
    assert recorded.trade.ai_decision_id == "ai1"
    assert recorded.trade.bot_id == "bot1"
    assert context["ai"]["model_version"] == "2.3"
    assert context["ai"]["model_version_id"] == "mv-2-3"
    assert "latest" not in str(context["ai"]).lower().replace("never 'latest'", "")


async def test_historical_risk_context_is_read_never_recomputed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§10. The snapshot written then, not the platform now."""
    async with sessions() as db:
        order = await make_order(db)
        assert order.id
        db.add(
            RiskEvent(
                id="risk1",
                order_id=order.id,
                decision="approve",
                reason="within limits",
                occurred_at=NOW - timedelta(hours=3),
                snapshot={"equity": "100000", "open_positions": 2, "daily_loss": "0"},
                configuration_version="cfg-7",
                mode="demo",
            )
        )
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
        await db.commit()
        context = await TradeJournalService().context_for(db, recorded.trade)  # type: ignore[arg-type]

    assert context["risk"]["available"] is True
    assert context["risk"]["snapshot"]["equity"] == "100000"
    assert context["risk"]["configuration_version"] == "cfg-7"
    assert "Never recalculated" in context["risk"]["note"]


async def test_sizing_is_read_from_the_order_never_re_run(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§11. Re-running it would use today's equity and tick value."""
    async with sessions() as db:
        await make_order(
            db,
            sizing={
                "mode": "risk_percent",
                "equity": "100000",
                "risk_pct": "0.005",
                "risk_amount": "500",
                "stop_distance": "0.0100",
                "tick_value": "1",
                "volume_step": "0.01",
                "final_quantity": "1.00",
            },
        )
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
        await db.commit()
        context = await TradeJournalService().context_for(db, recorded.trade)  # type: ignore[arg-type]

    assert context["sizing"]["available"] is True
    assert context["sizing"]["sizing"]["risk_amount"] == "500"
    assert "never re-run" in context["sizing"]["note"]


async def test_an_absent_ai_block_says_which_kind_of_absent(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§51. Not evidence the AI was bypassed, and it says so."""
    async with sessions() as db:
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
        await db.commit()
        context = await TradeJournalService().context_for(db, recorded.trade)  # type: ignore[arg-type]
    assert context["ai"]["available"] is False
    assert "AI_DISABLED" in context["ai"]["why"]
    assert "NOT evidence the AI was bypassed" in context["ai"]["why"]


async def test_the_account_is_carried_on_the_trade_not_only_the_position(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§30. A deleted position must not take the attribution with it."""
    async with sessions() as db:
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
    assert recorded.trade is not None
    assert recorded.trade.broker_account_id == "broker1"
    assert recorded.trade.paper_account_id is None


# ============================================================== execution


async def test_the_requested_price_is_recorded_beside_the_actual(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§12 and the 279-point live trade `CLAUDE.md` records."""
    async with sessions() as db:
        await make_order(db, requested=Decimal("0.59752"))
        position = await make_position(db, entry=Decimal("0.59473"))
        await add_close(db, position, at=NOW, price="0.59638", quantity="1", running="-165")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
        await db.commit()
        context = await TradeJournalService().context_for(db, recorded.trade)  # type: ignore[arg-type]

    assert recorded.trade is not None
    assert recorded.trade.requested_entry_price == Decimal("0.59752")
    assert recorded.trade.entry_price == Decimal("0.59473")
    assert (
        context["execution"]["requested_entry_price"] != context["execution"]["actual_entry_price"]
    )


def test_costs_are_reported_separately_and_never_folded_together() -> None:
    """§18."""

    class Row:
        gross_profit = Decimal("120")
        commission = Decimal("3")
        swap = Decimal("1")
        fees = Decimal("0.5")
        net_profit = Decimal("115.5")
        currency = "USD"
        r_multiple = Decimal("1.2")

    block = context_engine.costs_block(Row())
    assert block["commission"] == "3"
    assert block["fees"] == "0.5"
    assert block["swap"] == "1"
    assert "Read R rather than net currency" in block["note"]


async def test_commission_and_swap_are_summed_from_the_close_events(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        position = await make_position(db, realized=Decimal("100"))
        await add_close(
            db, position, at=NOW, price="1.2", quantity="1", running="100", commission="3", swap="1"
        )
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
    assert recorded.trade is not None
    assert recorded.trade.commission == Decimal("3")
    assert recorded.trade.swap == Decimal("1")
    # gross = net + costs, so the trade reconciles under `quality.check`.
    assert recorded.trade.gross_profit == Decimal("104")
    assert recorded.trade.data_quality is not None
    assert recorded.trade.data_quality["errors"] == 0


# ================================================================ timeline


async def test_the_timeline_is_chronological_and_names_its_sources(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        webhook = WebhookEvent(
            id="wh1",
            provider="tradingview",
            idempotency_key="k1",
            received_at=NOW - timedelta(hours=4),
            auth_strength="strong",
            payload={"secret": SECRET, "ticker": "EURUSD"},
            status="accepted",
        )
        db.add(webhook)
        signal = Signal(
            id="sig1",
            signal_key="s1",
            source="tradingview",
            webhook_event_id="wh1",
            symbol_id="sym1",
            direction="buy",
            mode="demo",
            signal_time=NOW - timedelta(hours=3, minutes=30),
            received_at=NOW - timedelta(hours=3, minutes=30),
            auth_strength="strong",
            status="executed",
        )
        db.add(signal)
        order = await make_order(db, signal_id="sig1")
        db.add(
            OrderEvent(
                order_id=order.id,
                event_type="filled",
                occurred_at=NOW - timedelta(hours=2, minutes=59),
                retcode=10009,
                sequence=1,
            )
        )
        db.add(
            Execution(
                order_id=order.id,
                executed_at=NOW - timedelta(hours=2, minutes=59),
                price=Decimal("1.1000"),
                quantity=Decimal("1"),
                fill_source="broker",
                broker_deal_id="deal-1",
            )
        )
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
        await db.commit()
        timeline = await TradeJournalService().timeline_for(db, recorded.trade)  # type: ignore[arg-type]

    times = [event["at"] for event in timeline["events"]]
    assert times == sorted(times)
    sources = {event["source"] for event in timeline["events"]}
    assert {
        "webhook",
        "signal",
        "order",
        "order_event",
        "execution",
        "position",
        "journal",
    } <= sources
    assert timeline["count"] == len(timeline["events"])
    assert "Nothing is stored" in timeline["note"]


async def test_no_webhook_secret_reaches_the_timeline(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§45. The provider payload stays where it is."""
    async with sessions() as db:
        db.add(
            WebhookEvent(
                id="wh1",
                provider="tradingview",
                idempotency_key="k1",
                received_at=NOW - timedelta(hours=4),
                auth_strength="strong",
                payload={"secret": SECRET},
                status="accepted",
            )
        )
        signal = Signal(
            id="sig1",
            signal_key="s1",
            source="tradingview",
            webhook_event_id="wh1",
            symbol_id="sym1",
            direction="buy",
            mode="demo",
            signal_time=NOW - timedelta(hours=3),
            received_at=NOW - timedelta(hours=3),
            auth_strength="strong",
            status="executed",
        )
        db.add(signal)
        await make_order(db, signal_id="sig1")
        position = await make_position(db)
        await add_close(db, position, at=NOW, price="1.2", quantity="1", running="100")
        await db.commit()
        recorded = await TradeJournalService().record_close(db, position)
        await db.commit()
        timeline = await TradeJournalService().timeline_for(db, recorded.trade)  # type: ignore[arg-type]
        context = await TradeJournalService().context_for(db, recorded.trade)  # type: ignore[arg-type]

    assert SECRET not in str(timeline)
    assert SECRET not in str(context)
    assert context["strategy"]["tradingview"]["payload"] is None


def test_an_empty_timeline_names_every_gap() -> None:
    """A blank is ambiguous; a named gap is not."""

    class Row:
        id = "t1"
        closed_at = NOW
        status = "closed"
        exit_reason = "stop_loss"
        net_profit = Decimal("-10")

    events = timeline_engine.derive(trade=Row())
    gaps = timeline_engine.gaps_in(events)
    assert len(events) == 1
    assert len(gaps) == 6
    assert any("NOT evidence the AI was bypassed" in gap for gap in gaps)


def test_events_in_the_same_second_keep_their_causal_order() -> None:
    """A fill rendered before the submission that caused it is unreadable."""

    class Row:
        id = "t1"
        closed_at = NOW + timedelta(hours=1)
        status = "closed"
        exit_reason = "take_profit"
        net_profit = Decimal("1")

    class Order_:
        created_at = NOW
        intent_id = "i1"
        order_type = "market"
        quantity = Decimal("1")
        requested_price = Decimal("1.1")
        stop_loss = None
        take_profit = None

    class Fill:
        executed_at = NOW
        price = Decimal("1.1")
        quantity = Decimal("1")
        commission = Decimal("0")
        swap = Decimal("0")
        slippage_points = Decimal("0")
        broker_deal_id = "d1"
        fill_source = "broker"

    events = timeline_engine.derive(trade=Row(), order=Order_(), executions=[Fill()])
    assert [e.source for e in events][:2] == ["order", "execution"]


# =========================================================== data quality


def test_a_negative_duration_is_reported_never_reordered() -> None:
    """§44 and §32. Swapping them would produce a trade that did not happen."""

    class Row:
        mode = "paper"
        entry_price = Decimal("1.1")
        exit_price = Decimal("1.2")
        opened_at = NOW
        closed_at = NOW - timedelta(hours=1)
        volume = Decimal("1")
        gross_profit = Decimal("10")
        net_profit = Decimal("10")
        commission = Decimal("0")
        swap = Decimal("0")
        fees = None
        status = "closed"
        exit_reason = "take_profit"
        broker_position_id = None
        position_id = None

    codes = {finding.code for finding in quality_engine.check(Row())}
    assert "negative_duration" in codes


def test_a_pnl_that_does_not_reconcile_is_detected() -> None:
    """§18's double-counting, made visible."""

    class Row:
        mode = "paper"
        entry_price = Decimal("1.1")
        exit_price = Decimal("1.2")
        opened_at = NOW - timedelta(hours=1)
        closed_at = NOW
        volume = Decimal("1")
        gross_profit = Decimal("100")
        commission = Decimal("3")
        swap = Decimal("1")
        fees = Decimal("0")
        net_profit = Decimal("100")  # costs never subtracted
        status = "closed"
        exit_reason = "take_profit"
        broker_position_id = None
        position_id = None

    codes = {finding.code for finding in quality_engine.check(Row())}
    assert "pnl_does_not_reconcile" in codes


def test_a_paper_trade_is_not_faulted_for_having_no_broker_id() -> None:
    class Row:
        mode = "paper"
        entry_price = Decimal("1.1")
        exit_price = Decimal("1.2")
        opened_at = NOW - timedelta(hours=1)
        closed_at = NOW
        volume = Decimal("1")
        gross_profit = Decimal("10")
        commission = Decimal("0")
        swap = Decimal("0")
        fees = Decimal("0")
        net_profit = Decimal("10")
        status = "closed"
        exit_reason = "take_profit"
        broker_position_id = None
        position_id = None

    codes = {finding.code for finding in quality_engine.check(Row())}
    assert "missing_broker_id" not in codes
    assert codes == set()


def test_checked_with_no_findings_is_not_the_same_as_unchecked() -> None:
    summary = quality_engine.summarise([])
    assert summary["checked"] is True
    assert summary["findings"] == []
    assert summary["errors"] == 0


def test_the_holding_period_is_exact_and_never_bucketed() -> None:
    """§20."""
    described = Holding.as_dict(NOW, NOW + timedelta(hours=1, minutes=30))
    assert described["seconds"] == 5400.0
    assert described["minutes"] == 90.0
    assert described["hours"] == 1.5
    assert "Never rounded or bucketed" in described["note"]


def test_a_missing_timestamp_makes_the_duration_unknown_not_zero() -> None:
    described = Holding.as_dict(NOW, None)
    assert described["seconds"] is None
    assert Holding.between(None, NOW) is None


# ========================================================= security, §45, §55


def _names(path: Path) -> set[str]:
    """Parsed, never grepped: a docstring explaining a rule fails a grep of it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
    return found


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _sources() -> list[Path]:
    return sorted(JOURNAL.glob("*.py")) + [ROUTER, QUERIES]


def test_the_journal_cannot_place_or_modify_anything() -> None:
    """§55. The strict list, parsed."""
    forbidden = {
        "place",
        "place_order",
        "submit_order",
        "cancel_order",
        "modify_order",
        "close_position",
        "modify_position",
        "open_position",
        "send_order",
        "order_send",
    }
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


def test_the_journal_imports_no_execution_path() -> None:
    """§29 and §55: MT5 is reached through the adapter, and not by this."""
    forbidden = (
        "app.oms",
        "app.orders",
        "app.sizing",
        "app.execution",
        "app.brokers",
        "app.risk.engine",
        "app.risk.service",
        "app.positions.manager",
        "app.positions.executor",
        "app.positions.reconciler",
        "MetaTrader5",
    )
    for path in _sources():
        for module in _imports(path):
            assert not any(module == bad or module.startswith(bad + ".") for bad in forbidden), (
                f"{path.name} imports {module}"
            )


def test_the_journal_cannot_enable_live_trading() -> None:
    forbidden = {"LIVE_TRADING", "live_trading", "LIVE_GATES", "live_execution_allowed"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"
        assert "app.core.settings" not in _imports(path)


def test_the_journal_never_deletes_history() -> None:
    """§31's NEVER list, parsed."""
    forbidden = {"delete", "drop_all", "drop_table", "truncate"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


def test_no_secret_name_is_referenced_anywhere_in_the_journal() -> None:
    """§45."""
    forbidden = {"password", "api_key", "secret", "webhook_secret", "credential"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


# ================================================================== the API


@pytest.fixture
async def app(settings: Any) -> Any:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401
    from app.main import create_app

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    async with application.state.session_factory() as db:
        db.add(Symbol(id="sym1", code="EURUSD", asset_class="fx"))
        await db.commit()
    yield application
    await engine.dispose()


@pytest.fixture
async def api(app: Any) -> Any:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        await c.post(
            "/auth/register", json={"email": "alice@example.com", "password": "Sufficient-1-pass"}
        )
        yield c


async def seed_trades(app: Any) -> None:
    """Two paper trades and one demo trade, with distinct attribution."""
    async with app.state.session_factory() as db:
        for index, (mode, account, reason, net, r) in enumerate(
            [
                ("paper", "acc-paper", "take_profit", "50", "1.0"),
                ("paper", "acc-paper", "stop_loss", "-25", "-1.0"),
                ("demo", "acc-broker", "trailing_stop", "10", "0.4"),
            ]
        ):
            db.add(
                Trade(
                    id=f"trade-{index}",
                    mode=mode,
                    status="closed",
                    symbol_id="sym1",
                    paper_account_id=account if mode == "paper" else None,
                    broker_account_id=account if mode != "paper" else None,
                    side="long",
                    volume=Decimal("1"),
                    entry_price=Decimal("1.1"),
                    exit_price=Decimal("1.2"),
                    opened_at=NOW - timedelta(hours=index + 1),
                    closed_at=NOW - timedelta(minutes=index),
                    gross_profit=Decimal(net),
                    commission=Decimal("0"),
                    swap=Decimal("0"),
                    net_profit=Decimal(net),
                    r_multiple=Decimal(r),
                    exit_reason=reason,
                    currency="USD",
                    source="pipeline",
                )
            )
        await db.commit()


async def test_the_static_routes_are_not_captured_by_the_id_route(api: Any) -> None:
    """Starlette matches in registration order; both must still resolve."""
    stats = await api.get("/v1/trades/statistics")
    export = await api.get("/v1/trades/export")
    assert stats.status_code == 200
    assert export.status_code == 200
    assert "trades" in stats.json()


async def test_every_journal_route_refuses_anonymous_access(app: Any) -> None:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
        for path in (
            "/v1/trades",
            "/v1/trades/statistics",
            "/v1/trades/export",
            "/v1/trades/trade-0",
            "/v1/trades/trade-0/timeline",
            "/v1/trades/trade-0/decisions",
            "/v1/trades/trade-0/executions",
            "/v1/trades/trade-0/analysis",
        ):
            assert (await anon.get(path)).status_code == 401, path


async def test_the_list_does_not_default_to_one_environment(app: Any, api: Any) -> None:
    """Section 34, read honestly: every row carries its own mode."""
    await seed_trades(app)
    body = (await api.get("/v1/trades")).json()
    assert {row["mode"] for row in body["items"]} == {"paper", "demo"}


async def test_filtering_by_mode_and_account_and_exit_reason(app: Any, api: Any) -> None:
    await seed_trades(app)
    paper = (await api.get("/v1/trades", params={"mode": "paper"})).json()
    assert {row["mode"] for row in paper["items"]} == {"paper"}

    account = (await api.get("/v1/trades", params={"account_id": "acc-broker"})).json()
    assert [row["id"] for row in account["items"]] == ["trade-2"]

    stopped = (await api.get("/v1/trades", params={"exit_reason": "stop_loss"})).json()
    assert [row["id"] for row in stopped["items"]] == ["trade-1"]


async def test_an_unknown_exit_reason_is_refused_not_ignored(app: Any, api: Any) -> None:
    """A filter that silently stops applying returns the wrong set as the right one."""
    await seed_trades(app)
    response = await api.get("/v1/trades", params={"exit_reason": "not-a-reason"})
    assert response.status_code == 422


async def test_search_finds_a_trade_by_its_own_id(app: Any, api: Any) -> None:
    await seed_trades(app)
    body = (await api.get("/v1/trades", params={"search": "trade-1"})).json()
    assert [row["id"] for row in body["items"]] == ["trade-1"]


async def test_the_list_paginates(app: Any, api: Any) -> None:
    await seed_trades(app)
    body = (await api.get("/v1/trades", params={"limit": 2})).json()
    assert len(body["items"]) == 2
    assert body["page"]["total"] == 3


async def test_statistics_report_r_and_say_which_figure_to_pool(app: Any, api: Any) -> None:
    await seed_trades(app)
    body = (await api.get("/v1/trades/statistics")).json()
    assert body["trades"] == 3
    assert body["wins"] == 2
    assert body["losses"] == 1
    assert body["by_exit_reason"]["stop_loss"] == 1
    assert body["by_mode"] == {"paper": 2, "demo": 1}
    assert "figure to pool" in body["r_multiple"]["note"]
    assert "L32" in body["not_computed"]["sharpe"]


async def test_statistics_on_an_empty_set_have_no_win_rate(api: Any) -> None:
    """Nought wins from nought trades is not a nought percent win rate."""
    body = (await api.get("/v1/trades/statistics")).json()
    assert body["trades"] == 0
    assert body["win_rate"] is None


async def test_statistics_respect_the_account_scope(app: Any, api: Any) -> None:
    await seed_trades(app)
    body = (await api.get("/v1/trades/statistics", params={"account_id": "acc-broker"})).json()
    assert body["trades"] == 1
    assert body["by_mode"] == {"demo": 1}


async def test_the_export_is_csv_with_mode_in_the_second_column(app: Any, api: Any) -> None:
    await seed_trades(app)
    response = await api.get("/v1/trades/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0].split(",")[:3] == ["trade_id", "mode", "status"]
    assert len(lines) == 4


async def test_the_export_respects_its_filters(app: Any, api: Any) -> None:
    await seed_trades(app)
    response = await api.get("/v1/trades/export", params={"mode": "demo"})
    lines = response.text.strip().splitlines()
    assert len(lines) == 2
    assert "demo" in lines[1]


async def test_an_unknown_symbol_filter_returns_nothing_not_everything(app: Any, api: Any) -> None:
    await seed_trades(app)
    response = await api.get("/v1/trades/export", params={"symbol": "NOTASYMBOL"})
    assert response.text.strip().splitlines() == [",".join(EXPORT_HEADER)]


async def test_the_export_carries_no_credential_shaped_column(app: Any, api: Any) -> None:
    await seed_trades(app)
    header = (await api.get("/v1/trades/export")).text.splitlines()[0].lower()
    for forbidden in ("password", "secret", "api_key", "login", "payload"):
        assert forbidden not in header


async def test_the_timeline_route_answers_for_an_imported_trade(app: Any, api: Any) -> None:
    """Two events is the honest length, not a reconstruction."""
    await seed_trades(app)
    body = (await api.get("/v1/trades/trade-0/timeline")).json()
    assert body["trade_id"] == "trade-0"
    assert body["count"] >= 1
    assert body["gaps"]


async def test_the_decisions_route_marks_every_absent_block(app: Any, api: Any) -> None:
    await seed_trades(app)
    body = (await api.get("/v1/trades/trade-0/decisions")).json()
    for block in ("strategy", "ai", "risk", "sizing"):
        assert body[block]["available"] is False
        assert body[block]["why"]
    assert body["holding"]["seconds"] is not None


async def test_the_analysis_route_separates_recorded_from_recomputed(app: Any, api: Any) -> None:
    await seed_trades(app)
    body = (await api.get("/v1/trades/trade-0/analysis")).json()
    assert body["recorded"] is None  # the seeded rows were never checked
    assert body["recomputed"]["checked"] is True
    assert "was edited" in body["note"]


async def test_a_missing_trade_is_a_404_on_every_detail_route(api: Any) -> None:
    for suffix in ("", "/timeline", "/decisions", "/executions", "/analysis"):
        response = await api.get(f"/v1/trades/does-not-exist{suffix}")
        assert response.status_code == 404, suffix
