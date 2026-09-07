"""Portfolio management and exposure (L30).

The ones that matter most, and they are the rules the brief calls strict:

  * `test_gross_and_net_are_different_numbers` — §11 and §12.
  * `test_a_notional_without_a_contract_size_is_refused` — §13, the metals lesson.
  * `test_currency_exposure_is_withheld_when_metadata_is_missing` — §14.
  * `test_the_peak_never_falls_when_the_window_shortens` — §22.
  * `test_an_unstopped_position_has_no_risk_not_zero_risk` — §26.
  * `test_a_disconnected_broker_reports_unavailable_not_stale_values` — §31.
  * `test_paper_and_live_positions_are_never_pooled` — §41.
  * `test_the_portfolio_engine_cannot_place_or_modify_anything` — §63.
  * `test_no_response_carries_a_credential` — §55.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.db.base import Base
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.execution import Position, Trade
from app.models.journal import PortfolioSnapshot
from app.models.market import Symbol, SymbolMapping
from app.portfolio import events as portfolio_events
from app.portfolio import exposure as exposure_engine
from app.portfolio import pnl as pnl_engine
from app.portfolio import state as account_state
from app.portfolio.service import (
    PortfolioService,
    from_broker_account,
    from_paper_account,
)
from app.portfolio.state import AccountState, Freshness, PortfolioHealth, Reconciliation, Source
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PORTFOLIO = Path(__file__).resolve().parents[1] / "app" / "portfolio"
ROUTER = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "portfolio.py"

NOW = datetime(2026, 9, 4, 12, 0, 0)


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
                balance=Decimal("100500"),
                equity=Decimal("100650"),
                realized_pnl=Decimal("500"),
                unrealized_pnl=Decimal("150"),
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
        await session.commit()
    yield factory
    await engine.dispose()


async def make_symbol(
    db: AsyncSession,
    *,
    code: str = "EURUSD",
    base: str | None = "EUR",
    quote: str | None = "USD",
    contract_size: Decimal | None = Decimal("100000"),
    tick_size: Decimal | None = Decimal("0.00001"),
    tick_value: Decimal | None = Decimal("1"),
) -> Symbol:
    symbol = Symbol(code=code, asset_class="fx", base_currency=base, quote_currency=quote)
    db.add(symbol)
    await db.flush()
    db.add(
        SymbolMapping(
            symbol_id=symbol.id,
            provider="mt5",
            provider_symbol=code,
            contract_size=contract_size,
            tick_size=tick_size,
            tick_value=tick_value,
        )
    )
    await db.flush()
    return symbol


async def make_position(
    db: AsyncSession,
    symbol: Symbol,
    *,
    side: str = "long",
    quantity: Decimal = Decimal("1"),
    entry: Decimal = Decimal("1.1000"),
    stop: Decimal | None = Decimal("1.0900"),
    mode: str = "paper",
    account: str = "paper1",
    strategy: str | None = None,
    status: str = "open",
) -> Position:
    row = Position(
        mode=mode,
        paper_account_id=account if mode == "paper" else None,
        broker_account_id=account if mode != "paper" else None,
        symbol_id=symbol.id,
        strategy_version_id=strategy,
        side=side,
        quantity=quantity,
        initial_quantity=quantity,
        entry_price=entry,
        stop_loss=stop,
        status=status,
        opened_at=NOW_UTC - timedelta(hours=2),
    )
    db.add(row)
    await db.flush()
    return row


def view_of(
    symbol: str = "EURUSD",
    *,
    side: str = "long",
    quantity: Decimal = Decimal("1"),
    entry: Decimal = Decimal("1.1000"),
    contract_size: Decimal | None = Decimal("100000"),
    base: str | None = "EUR",
    quote: str | None = "USD",
    stop: Decimal | None = Decimal("1.0900"),
    tick_size: Decimal | None = Decimal("0.00001"),
    tick_value: Decimal | None = Decimal("1"),
    strategy: str | None = None,
    bot: str | None = None,
    current: Decimal | None = None,
) -> exposure_engine.PositionView:
    return exposure_engine.PositionView(
        position_id=f"{symbol}-{side}-{quantity}",
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=entry,
        current_price=current,
        stop_loss=stop,
        strategy_id=strategy,
        bot_id=bot,
        asset_class="fx",
        base_currency=base,
        quote_currency=quote,
        contract_size=contract_size,
        tick_size=tick_size,
        tick_value=tick_value,
    )


class FakeAccount:
    currency = "USD"
    server = "Broker-Demo"
    balance = Decimal("50000")
    equity = Decimal("50250")
    margin = Decimal("1000")
    free_margin = Decimal("49250")
    # Present on the real dataclass; never copied into an AccountState.
    login = "8675309"


class FakeAdapter:
    """A broker that answers reads. It has no write verb by construction."""

    def __init__(self, positions: list[Any] | None = None, quotes: dict[str, Any] | None = None):
        self._positions = positions or []
        self._quotes = quotes or {}

    async def get_account(self) -> FakeAccount:
        return FakeAccount()

    async def get_positions(self) -> list[Any]:
        return self._positions

    async def get_quote(self, symbol: str) -> Any:
        if symbol not in self._quotes:
            raise KeyError(symbol)
        return self._quotes[symbol]


class Quote:
    def __init__(self, bid: Decimal, ask: Decimal) -> None:
        self.bid, self.ask = bid, ask


class Held:
    def __init__(self, position_id: str) -> None:
        self.position_id = position_id


# ========================================================= gross versus net


def test_gross_and_net_are_different_numbers() -> None:
    """§11 and §12. Long 50,000 and short 30,000 is 80,000 gross, +20,000 net."""
    report = exposure_engine.compute(
        [
            view_of(quantity=Decimal("0.5"), entry=Decimal("1.0"), contract_size=Decimal("100000")),
            view_of(
                symbol="GBPUSD",
                side="short",
                quantity=Decimal("0.3"),
                entry=Decimal("1.0"),
                base="GBP",
                contract_size=Decimal("100000"),
            ),
        ]
    )
    assert report.total.gross == Decimal("80000.0")
    assert report.total.net == Decimal("20000.0")
    assert report.total.gross != report.total.net


def test_every_bucket_carries_both_gross_and_net() -> None:
    report = exposure_engine.compute([view_of(), view_of(side="short")])
    for grouping in (report.by_symbol, report.by_strategy, report.by_bot, report.by_asset_class):
        for bucket in grouping.values():
            assert "gross" in bucket.as_dict()
            assert "net" in bucket.as_dict()


def test_a_flat_book_is_gross_positive_and_net_zero() -> None:
    """The case a single 'exposure' number would render as no exposure at all."""
    report = exposure_engine.compute([view_of(), view_of(side="short")])
    assert report.total.net == Decimal("0")
    assert report.total.gross > 0


# ================================================== notional, and the refusal


def test_a_notional_without_a_contract_size_is_refused() -> None:
    """§13 and the metals lesson: a notional in the wrong unit corrupts the total."""
    notional = exposure_engine.notional_of(view_of(contract_size=None))
    assert notional.computable is False
    assert notional.value is None
    assert "contract size" in notional.reason


def test_an_uncomputable_position_is_counted_never_zeroed() -> None:
    """§59. A total that silently excluded a position would be wrong invisibly."""
    report = exposure_engine.compute([view_of(), view_of(symbol="XAUUSD", contract_size=None)])
    assert report.total.uncomputable == 1
    assert report.total.positions == 1
    assert len(report.uncomputable) == 1
    assert report.uncomputable[0]["symbol"] == "XAUUSD"


def test_a_position_with_no_price_at_all_is_refused() -> None:
    position = view_of()
    object.__setattr__(position, "entry_price", None)
    assert exposure_engine.notional_of(position).computable is False


def test_notional_says_whether_it_used_the_current_price_or_the_entry() -> None:
    assert exposure_engine.notional_of(view_of()).priced_at == "entry"
    assert exposure_engine.notional_of(view_of(current=Decimal("1.2"))).priced_at == "current"


# ============================================================ currency, §14


def test_a_eurusd_long_is_eur_long_and_usd_short() -> None:
    report = exposure_engine.compute(
        [view_of(quantity=Decimal("1"), entry=Decimal("1.0"), contract_size=Decimal("100000"))]
    )
    assert report.currency_available is True
    assert report.by_currency["EUR"] == Decimal("100000.0")
    assert report.by_currency["USD"] == Decimal("-100000.0")


def test_currency_exposure_is_withheld_when_metadata_is_missing() -> None:
    """§14. Splitting a ticker in half is the error, not the fallback."""
    report = exposure_engine.compute([view_of(), view_of(symbol="US500", base=None, quote=None)])
    assert report.currency_available is False
    assert report.by_currency == {}
    assert report.as_dict()["by_currency"] is None
    assert "wrong for everything else" in report.as_dict()["currency_note"]


def test_a_short_reverses_the_currency_signs() -> None:
    report = exposure_engine.compute(
        [view_of(side="short", quantity=Decimal("1"), entry=Decimal("1.0"))]
    )
    assert report.by_currency["EUR"] < 0
    assert report.by_currency["USD"] > 0


# ============================================================== attribution


def test_a_position_with_no_bot_is_unattributed_not_guessed() -> None:
    """§17 and §59: an acknowledged gap beats an invented owner."""
    report = exposure_engine.compute([view_of()])
    assert "unattributed" in report.by_bot
    assert "unattributed" in report.by_strategy


def test_concentration_is_reported_not_enforced() -> None:
    report = exposure_engine.compute(
        [view_of(quantity=Decimal("3")), view_of(symbol="GBPUSD", base="GBP")]
    )
    concentration = report.concentration()
    assert concentration["largest"]["symbol"] == "EURUSD"
    assert "RISK ENGINE decides" in concentration["note"]


def test_concentration_of_an_empty_book_is_not_a_division() -> None:
    assert exposure_engine.compute([]).concentration()["largest"] is None


# ============================================================== open risk §26


def test_an_unstopped_position_has_no_risk_not_zero_risk() -> None:
    """§26. Zero would say the position cannot lose, which is the opposite."""
    risk = exposure_engine.open_risk([view_of(stop=None)])
    assert risk["positions"][0]["risk"] is None
    assert risk["unstopped_positions"] == 1
    assert risk["complete"] is False
    assert "NOT zero" in risk["positions"][0]["reason"]


def test_risk_to_stop_without_a_tick_value_is_refused() -> None:
    risk = exposure_engine.open_risk([view_of(tick_value=None)])
    assert risk["positions"][0]["risk"] is None
    assert risk["uncomputable_positions"] == 1


def test_risk_to_stop_uses_the_platforms_own_tick_arithmetic() -> None:
    """1.1000 to 1.0900 is 1,000 ticks of 0.00001, at 1 per tick, on 1 lot."""
    risk = exposure_engine.open_risk([view_of()])
    assert Decimal(risk["total"]) == Decimal("1000")
    assert risk["complete"] is True


# =================================================== marking, and its refusal


def test_a_position_with_no_mark_is_unmarked_not_valued_at_entry() -> None:
    assert exposure_engine.mark_to_market(view_of()) is None


def test_a_position_with_no_tick_value_cannot_be_marked() -> None:
    assert exposure_engine.mark_to_market(view_of(current=Decimal("1.11"), tick_value=None)) is None


def test_marking_uses_the_single_shared_conversion() -> None:
    """The same `value_per_price_unit` sizing and the paper engine use."""
    marked = exposure_engine.mark_to_market(view_of(current=Decimal("1.1100")))
    assert marked == Decimal("1000.00")


def test_a_short_gains_when_the_price_falls() -> None:
    marked = exposure_engine.mark_to_market(view_of(side="short", current=Decimal("1.0900")))
    assert marked is not None
    assert marked > 0


# ================================================================== P&L, §20


def test_unrealized_is_all_or_nothing() -> None:
    """§59. A partial total is a number a reader will treat as the whole."""
    total, missing = pnl_engine.unrealized_of([Decimal("10"), None, Decimal("5")])
    assert total is None
    assert missing == 1


def test_unrealized_sums_when_every_position_is_marked() -> None:
    total, missing = pnl_engine.unrealized_of([Decimal("10"), Decimal("5")])
    assert total == Decimal("15")
    assert missing == 0


def test_a_total_with_an_unknown_half_is_none_not_the_known_half() -> None:
    profit = pnl_engine.PnL(
        realized=Decimal("100"),
        unrealized=None,
        realized_today=Decimal("10"),
        trades_today=1,
        day_start=NOW,
    )
    assert profit.total is None
    assert profit.as_dict()["total"] is None


def test_realized_and_unrealized_are_not_double_counted() -> None:
    profit = pnl_engine.PnL(
        realized=Decimal("100"),
        unrealized=Decimal("25"),
        realized_today=Decimal("10"),
        trades_today=1,
        day_start=NOW,
    )
    assert profit.total == Decimal("125")
    assert "disjoint" in profit.as_dict()["no_double_counting"]


# ============================================================ drawdown, §22


def test_the_peak_never_falls_when_the_window_shortens() -> None:
    """§22. A peak from a rolling window falls as the window passes the high."""
    drawdown = pnl_engine.Drawdown()
    drawdown.observe(Decimal("100"), NOW)
    drawdown.observe(Decimal("140"), NOW + timedelta(days=1))
    drawdown.observe(Decimal("120"), NOW + timedelta(days=2))
    assert drawdown.peak_equity == Decimal("140")
    drawdown.observe(Decimal("110"), NOW + timedelta(days=3))
    assert drawdown.peak_equity == Decimal("140")
    assert drawdown.current == Decimal("30")


def test_a_new_high_is_not_a_drawdown() -> None:
    drawdown = pnl_engine.from_curve(
        [(NOW, Decimal("100")), (NOW + timedelta(days=1), Decimal("150"))]
    )
    assert drawdown.current == Decimal("0")
    assert drawdown.recovered is True


def test_recovery_is_unknown_rather_than_false_without_the_figures() -> None:
    assert pnl_engine.Drawdown().recovered is None


def test_the_maximum_drawdown_is_kept_after_recovery() -> None:
    drawdown = pnl_engine.from_curve(
        [
            (NOW, Decimal("100")),
            (NOW + timedelta(days=1), Decimal("60")),
            (NOW + timedelta(days=2), Decimal("100")),
        ]
    )
    assert drawdown.max_drawdown == Decimal("40")
    assert drawdown.current == Decimal("0")


# =============================================================== margin, §24


def test_margin_utilisation_is_none_when_either_input_is() -> None:
    assert pnl_engine.margin_utilisation(None, Decimal("100")).ratio is None
    assert pnl_engine.margin_utilisation(Decimal("50"), None).ratio is None


def test_margin_utilisation_is_labelled_as_not_being_risk() -> None:
    described = pnl_engine.margin_utilisation(Decimal("50"), Decimal("100")).as_dict()
    assert described["ratio"] == 0.5
    assert "not portfolio risk" in described["note"]


# ========================================== freshness and health, §44 and §45


def test_a_reading_that_never_happened_is_unknown_not_stale() -> None:
    assert account_state.freshness_of(None, now=NOW, tolerance=timedelta(seconds=60)) is (
        Freshness.unknown
    )


def test_an_old_reading_is_stale() -> None:
    old = NOW - timedelta(minutes=10)
    assert account_state.freshness_of(old, now=NOW, tolerance=timedelta(seconds=60)) is (
        Freshness.stale
    )


def test_health_is_a_precedence_never_a_score() -> None:
    """§45. A weighted composite can be tuned until it hides what mattered."""
    assert (
        account_state.worst_health(
            [PortfolioHealth.healthy, PortfolioHealth.stale, PortfolioHealth.error]
        )
        is PortfolioHealth.error
    )
    assert (
        account_state.worst_health([PortfolioHealth.stale, PortfolioHealth.reconciliation_required])
        is PortfolioHealth.reconciliation_required
    )


def test_a_disagreement_outranks_a_stale_figure() -> None:
    health, reasons = account_state.health_of(
        account=AccountState(
            account_id="a",
            environment="paper",
            balance=Decimal("1"),
            as_of=NOW - timedelta(hours=1),
            source=Source.paper_engine,
            freshness=Freshness.stale,
        ),
        reconciliation=Reconciliation(checked=True, agrees=False, mismatches=("p1: mismatch",)),
    )
    assert health is PortfolioHealth.reconciliation_required
    assert any("disagree" in reason for reason in reasons)


def test_health_states_carry_their_reasons() -> None:
    health, reasons = account_state.health_of(
        account=account_state.unavailable("a", "demo", "the terminal is not connected"),
        reconciliation=account_state.not_checked("no adapter."),
    )
    assert health is PortfolioHealth.error
    assert reasons and "not connected" in reasons[0]


def test_an_unchecked_reconciliation_does_not_read_as_agreement() -> None:
    """§30. An unchecked portfolio and a matching one must not look alike."""
    unchecked = account_state.not_checked("no adapter.")
    assert unchecked.checked is False
    assert unchecked.agrees is False
    assert "not the same as agreement" in unchecked.note


# ================================================== never fabricate, §31, §59


def test_an_unavailable_account_carries_no_figures_at_all() -> None:
    state = account_state.unavailable("a", "live", "the terminal is not connected")
    assert state.balance is None
    assert state.equity is None
    assert state.margin_used is None
    assert state.freshness is Freshness.unknown
    assert state.source is Source.unavailable


def test_an_absent_figure_serialises_as_null_never_zero() -> None:
    described = account_state.unavailable("a", "live", "down").as_dict()
    assert described["balance"] is None
    assert described["equity"] is None
    assert "never zero" in described["rule"]


def test_money_is_never_rendered_as_a_float() -> None:
    described = AccountState(
        account_id="a", environment="paper", balance=Decimal("100000.12345678")
    ).as_dict()
    assert described["balance"] == "100000.12345678"
    assert isinstance(described["balance"], str)


async def test_a_disconnected_broker_reports_unavailable_not_stale_values(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§31. The whole point of the level in one assertion."""
    service = PortfolioService()
    async with sessions() as db:
        db.add(
            PortfolioSnapshot(
                broker_account_id="broker1",
                mode="demo",
                taken_at=NOW_UTC - timedelta(days=1),
                balance=Decimal("50000"),
                equity=Decimal("50000"),
            )
        )
        await db.commit()
        state = await service.account_state_for(
            db, account_id="broker1", environment="demo", adapter=None
        )
    assert state.balance is None
    assert state.equity is None
    assert "no broker adapter is connected" in (state.unavailable_reason or "")


async def test_a_broker_read_that_raises_becomes_a_state_not_an_exception(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class Broken:
        async def get_account(self) -> Any:
            raise RuntimeError("terminal gone")

    async with sessions() as db:
        state = await PortfolioService().account_state_for(
            db, account_id="broker1", environment="demo", adapter=Broken()
        )
    assert state.source is Source.unavailable
    assert "RuntimeError" in (state.unavailable_reason or "")


# ============================================ paper and live are never pooled


async def test_paper_and_live_positions_are_never_pooled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§41. Different columns, different tables; not a flag anyone can forget."""
    service = PortfolioService()
    async with sessions() as db:
        symbol = await make_symbol(db)
        await make_position(db, symbol, mode="paper", account="paper1")
        await make_position(db, symbol, mode="demo", account="broker1")
        await db.commit()

        paper = await service.positions_for(db, account_id="paper1", environment="paper")
        broker = await service.positions_for(db, account_id="broker1", environment="demo")
    assert len(paper) == 1
    assert len(broker) == 1
    assert paper[0].position_id != broker[0].position_id


async def test_a_paper_account_state_comes_from_the_paper_engines_own_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        state = await PortfolioService().account_state_for(
            db, account_id="paper1", environment="paper"
        )
    assert state.source is Source.paper_engine
    assert state.balance == Decimal("100500.0000")
    assert state.equity == Decimal("100650.0000")
    # The simulator holds no margin: there is no counterparty to require it.
    assert state.margin_used is None


async def test_an_account_that_does_not_exist_is_unavailable_not_empty(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        state = await PortfolioService().account_state_for(
            db, account_id="nope", environment="paper"
        )
    assert state.source is Source.unavailable


# =================================================== positions from the table


async def test_positions_are_read_from_the_existing_table_not_a_second_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§9. One representation of a position, and it is L21's."""
    async with sessions() as db:
        symbol = await make_symbol(db)
        row = await make_position(db, symbol)
        await db.commit()
        found = await PortfolioService().positions_for(db, account_id="paper1", environment="paper")
    assert [p.position_id for p in found] == [row.id]
    assert found[0].symbol == "EURUSD"
    assert found[0].contract_size == Decimal("100000.00000000")


async def test_a_closed_position_is_not_open_exposure(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        symbol = await make_symbol(db)
        await make_position(db, symbol, status="closed")
        await db.commit()
        found = await PortfolioService().positions_for(db, account_id="paper1", environment="paper")
    assert found == []


async def test_a_symbol_with_no_contract_spec_yields_no_contract_size(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The refusal has to survive the round trip, not just the unit test."""
    async with sessions() as db:
        symbol = Symbol(code="XAUUSD", asset_class="metal")
        db.add(symbol)
        await db.flush()
        await make_position(db, symbol)
        await db.commit()
        found = await PortfolioService().positions_for(db, account_id="paper1", environment="paper")
    assert found[0].contract_size is None
    assert exposure_engine.notional_of(found[0]).computable is False


async def test_a_bot_is_attributed_through_the_bot_managers_own_link(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.bots import Bot

    async with sessions() as db:
        symbol = await make_symbol(db)
        await make_position(db, symbol)
        db.add(
            Bot(
                id="bot1",
                user_id="u1",
                name="b",
                mode="paper",
                paper_account_id="paper1",
            )
        )
        await db.commit()
        found = await PortfolioService().positions_for(db, account_id="paper1", environment="paper")
    assert found[0].bot_id == "bot1"


# ============================================================ the day, §21


async def test_the_trading_day_boundary_is_the_risk_engines_own(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§21. One definition, and it is L17's -- not a second `.replace(hour=0)`."""
    from app.risk.state import day_start

    async with sessions() as db:
        profit = await PortfolioService().pnl_for(
            db, account_id="paper1", environment="paper", positions=[], now=NOW
        )
    assert profit.day_start == day_start(NOW.replace(tzinfo=UTC)).replace(tzinfo=None)
    assert profit.day_start == datetime(2026, 9, 4, 0, 0)


def test_the_portfolio_package_defines_no_second_day_boundary() -> None:
    """The bug `CLAUDE.md` records is a `.replace(hour=0)` somebody wrote again."""
    for path in sorted(PORTFOLIO.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "replace":
                    keywords = {kw.arg for kw in node.keywords}
                    assert "hour" not in keywords, f"{path.name} defines its own day boundary"


async def test_realized_today_counts_only_trades_after_the_boundary(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        symbol = await make_symbol(db)
        # TWO positions, because L31 made one journal row per POSITION EPISODE a
        # database guarantee: `UNIQUE(position_id) WHERE position_id IS NOT
        # NULL`. Two closed trades are two episodes, and the shape this test
        # used to build -- two trades sharing a position -- is now correctly
        # impossible.
        for position, (closed_at, profit) in zip(
            [await make_position(db, symbol), await make_position(db, symbol)],
            (
                (NOW - timedelta(days=2), Decimal("100")),
                (NOW - timedelta(hours=1), Decimal("25")),
            ),
            strict=True,
        ):
            db.add(
                Trade(
                    mode="paper",
                    position_id=position.id,
                    symbol_id=symbol.id,
                    side="long",
                    volume=Decimal("1"),
                    entry_price=Decimal("1.1"),
                    exit_price=Decimal("1.2"),
                    opened_at=closed_at - timedelta(hours=1),
                    closed_at=closed_at,
                    gross_profit=profit,
                    net_profit=profit,
                )
            )
        await db.commit()
        profit_report = await PortfolioService().pnl_for(
            db, account_id="paper1", environment="paper", positions=[], now=NOW
        )
    assert profit_report.realized == Decimal("125")
    assert profit_report.realized_today == Decimal("25")
    assert profit_report.trades_today == 1


async def test_realized_comes_from_the_journal_of_one_account_only(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`trades` carries no account column, so the join is what keeps them apart."""
    async with sessions() as db:
        symbol = await make_symbol(db)
        mine = await make_position(db, symbol)
        theirs = await make_position(db, symbol, mode="demo", account="broker1")
        for position, profit in ((mine, Decimal("10")), (theirs, Decimal("999"))):
            db.add(
                Trade(
                    mode=position.mode,
                    position_id=position.id,
                    symbol_id=symbol.id,
                    side="long",
                    volume=Decimal("1"),
                    entry_price=Decimal("1.1"),
                    exit_price=Decimal("1.2"),
                    opened_at=NOW_UTC - timedelta(hours=2),
                    closed_at=NOW_UTC - timedelta(hours=1),
                    gross_profit=profit,
                    net_profit=profit,
                )
            )
        await db.commit()
        report = await PortfolioService().pnl_for(
            db, account_id="paper1", environment="paper", positions=[], now=NOW
        )
    assert report.realized == Decimal("10")


# ============================================================ the whole view


async def test_a_view_names_the_source_of_every_figure(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§29. The engine aggregates; it does not own."""
    async with sessions() as db:
        symbol = await make_symbol(db)
        await make_position(db, symbol)
        await db.commit()
        state = await PortfolioService().account_state_for(
            db, account_id="paper1", environment="paper"
        )
        view = await PortfolioService().build(db, account=state, now=NOW)
    described = view.as_dict()
    assert described["sources"]["account"] == "PAPER_ENGINE"
    assert described["sources"]["realized_pnl"] == "trades, the journal (L19)"
    assert "does not own" in described["sources"]["note"]
    assert "RISK ENGINE decides" in described["authority"]


async def test_a_view_with_no_reconciliation_says_so(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        state = await PortfolioService().account_state_for(
            db, account_id="paper1", environment="paper"
        )
        view = await PortfolioService().build(db, account=state, now=NOW)
    assert view.reconciliation.checked is False
    assert view.reconciliation.agrees is False


async def test_an_unmarked_position_leaves_unrealized_unavailable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        symbol = await make_symbol(db)
        await make_position(db, symbol)
        await db.commit()
        state = await PortfolioService().account_state_for(
            db, account_id="paper1", environment="paper"
        )
        view = await PortfolioService().build(db, account=state, now=NOW)
    assert view.pnl is not None
    assert view.pnl.unrealized is None
    assert view.pnl.total is None
    assert view.pnl.unrealized_unavailable == ("EURUSD",)


async def test_a_marked_position_produces_an_unrealized_total(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        symbol = await make_symbol(db)
        await make_position(db, symbol)
        await db.commit()
        state = await PortfolioService().account_state_for(
            db, account_id="paper1", environment="paper"
        )
        view = await PortfolioService().build(
            db, account=state, prices={"EURUSD": Decimal("1.1100")}, now=NOW
        )
    assert view.pnl is not None
    assert view.pnl.unrealized == Decimal("1000.00")


async def test_a_stale_account_makes_the_view_stale(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        state = AccountState(
            account_id="paper1",
            environment="paper",
            balance=Decimal("100"),
            equity=Decimal("100"),
            as_of=NOW - timedelta(hours=2),
            source=Source.paper_engine,
        )
        view = await PortfolioService().build(db, account=state, now=NOW)
    assert view.health is PortfolioHealth.stale
    assert view.account.freshness is Freshness.stale


# =========================================================== the risk handoff


async def test_the_risk_state_carries_nulls_rather_than_assumptions(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§25 and §47. L17 treats a null a limit needs as a veto."""
    async with sessions() as db:
        state = account_state.unavailable("paper1", "paper", "nothing could be read")
        view = await PortfolioService().build(db, account=state, now=NOW)
    handoff = view.to_risk_state()
    assert handoff["equity"] is None
    assert handoff["balance"] is None
    assert handoff["open_positions"] == 0
    assert "VETO" in handoff["note"]


async def test_the_risk_state_offers_exactly_what_the_engine_reads(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from app.risk.engine import PortfolioState

    async with sessions() as db:
        state = await PortfolioService().account_state_for(
            db, account_id="paper1", environment="paper"
        )
        view = await PortfolioService().build(db, account=state, now=NOW)
    from app.portfolio.service import NOT_SUPPLIED

    fields = set(PortfolioState.__dataclass_fields__)
    offered = set(view.to_risk_state())
    unaccounted = fields - offered - set(NOT_SUPPLIED)
    assert not unaccounted, (
        f"the risk engine reads {sorted(unaccounted)} and the portfolio engine neither "
        "supplies them nor declares who does"
    )
    # And the declaration is not a way to quietly stop supplying something.
    assert not (offered & set(NOT_SUPPLIED))


def test_what_the_portfolio_engine_does_not_own_is_named_not_silent() -> None:
    """L28's DECLINED pattern: an omission with a reason beats an absence."""
    from app.portfolio.service import NOT_SUPPLIED

    for field, owner in NOT_SUPPLIED.items():
        assert owner, f"{field} is declared unsupplied without saying who owns it"


# ================================================================ snapshots


async def test_a_snapshot_of_nothing_is_never_written(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§59. A fabricated point would enter the equity curve as a real one."""
    async with sessions() as db:
        state = account_state.unavailable("paper1", "paper", "nothing could be read")
        view = await PortfolioService().build(db, account=state, now=NOW)
        written = await PortfolioService().record(db, view)
    assert written is None


async def test_a_readable_view_is_recorded(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        state = await PortfolioService().account_state_for(
            db, account_id="paper1", environment="paper"
        )
        view = await PortfolioService().build(db, account=state, now=NOW)
        written = await PortfolioService().record(db, view, user_id="u1")
    assert written is not None
    assert written.mode == "paper"
    assert written.paper_account_id == "paper1"
    assert written.broker_account_id is None


async def test_the_peak_comes_from_the_recorded_history(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        for day, equity in ((3, "120000"), (2, "90000"), (1, "95000")):
            db.add(
                PortfolioSnapshot(
                    paper_account_id="paper1",
                    mode="paper",
                    taken_at=NOW_UTC - timedelta(days=day),
                    balance=Decimal(equity),
                    equity=Decimal(equity),
                )
            )
        await db.commit()
        drawdown = await PortfolioService().drawdown_for(
            db, account_id="paper1", environment="paper", equity=Decimal("100000")
        )
    assert drawdown.peak_equity == Decimal("120000.0000")
    assert drawdown.current == Decimal("20000.0000")


# =========================================================== reconciliation


async def test_a_paper_account_has_no_venue_to_reconcile_against(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        found = await PortfolioService().reconcile_for(db, account_id="paper1", environment="paper")
    assert found.checked is False
    assert "no external venue" in found.note


async def test_a_disagreement_is_reported_and_never_repaired(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§30 and §63: compared, not settled. Repair is L21's, behind its own route."""
    async with sessions() as db:
        symbol = await make_symbol(db)
        row = await make_position(db, symbol, mode="demo", account="broker1")
        await db.commit()
        found = await PortfolioService().reconcile_for(
            db,
            account_id="broker1",
            environment="demo",
            adapter=FakeAdapter(positions=[Held("someone-elses")]),
        )
        still_open = await PortfolioService().positions_for(
            db, account_id="broker1", environment="demo"
        )
    assert found.checked is True
    assert found.agrees is False
    assert len(found.mismatches) == 2
    assert "never repaired" in found.note
    # The position is untouched: this engine modifies nothing.
    assert [p.position_id for p in still_open] == [row.id]


async def test_an_agreeing_broker_reconciles_clean(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        symbol = await make_symbol(db)
        row = await make_position(db, symbol, mode="demo", account="broker1")
        await db.commit()
        found = await PortfolioService().reconcile_for(
            db, account_id="broker1", environment="demo", adapter=FakeAdapter([Held(row.id)])
        )
    assert found.agrees is True
    assert found.mismatches == ()


# ==================================================================== marks


async def test_a_symbol_the_broker_cannot_quote_is_left_unmarked() -> None:
    service = PortfolioService()
    prices, unpriced = await service.marks_for(
        [view_of(), view_of(symbol="GBPUSD")],
        adapter=FakeAdapter(quotes={"EURUSD": Quote(Decimal("1.10"), Decimal("1.12"))}),
    )
    assert prices == {"EURUSD": Decimal("1.11")}
    assert unpriced == ("GBPUSD",)


async def test_without_an_adapter_nothing_is_marked() -> None:
    prices, unpriced = await PortfolioService().marks_for([view_of()], adapter=None)
    assert prices == {}
    assert unpriced == ("EURUSD",)


# ================================================================== events


def make_view(
    *,
    equity: Decimal | None = Decimal("100000"),
    health: PortfolioHealth = PortfolioHealth.healthy,
    drawdown_pct: str | None = None,
    positions: list[exposure_engine.PositionView] | None = None,
) -> Any:
    from app.portfolio.service import PortfolioView

    held = positions or []
    drawdown = pnl_engine.Drawdown()
    if drawdown_pct is not None:
        peak = Decimal("100000")
        drawdown.observe(peak, NOW)
        drawdown.observe(peak * (Decimal("1") - Decimal(drawdown_pct)), NOW)
    return PortfolioView(
        account=AccountState(
            account_id="paper1",
            environment="paper",
            balance=equity,
            equity=equity,
            source=Source.paper_engine,
            freshness=Freshness.fresh,
            as_of=NOW,
        ),
        positions=held,
        exposure=exposure_engine.compute(held),
        pnl=pnl_engine.PnL(
            realized=Decimal("0"),
            unrealized=Decimal("0"),
            realized_today=Decimal("0"),
            trades_today=0,
            day_start=NOW,
        ),
        drawdown=drawdown,
        health=health,
        at=NOW,
    )


def test_the_first_view_reports_everything_once() -> None:
    types = [t for t, _ in portfolio_events.changes_between(None, make_view())]
    assert "PORTFOLIO_UPDATED" in types
    assert "PORTFOLIO_HEALTH_CHANGED" in types


def test_an_unchanged_portfolio_is_not_an_event() -> None:
    """§32. A stream that republishes on a timer is a poll wearing a socket."""
    view = make_view()
    assert portfolio_events.changes_between(view, make_view()) == []


def test_a_moved_figure_is_an_event() -> None:
    types = [
        t
        for t, _ in portfolio_events.changes_between(
            make_view(), make_view(equity=Decimal("99000"))
        )
    ]
    assert types == ["PORTFOLIO_UPDATED"]


def test_going_stale_is_an_event_even_though_no_figure_moved() -> None:
    before = make_view()
    after = make_view()
    object.__setattr__(after.account, "freshness", Freshness.stale)
    types = [t for t, _ in portfolio_events.changes_between(before, after)]
    assert "PORTFOLIO_UPDATED" in types


def test_a_drawdown_crossing_fires_once_not_every_refresh() -> None:
    crossed = make_view(drawdown_pct="0.12")
    first = portfolio_events.changes_between(make_view(drawdown_pct="0.01"), crossed)
    assert any(t == "DRAWDOWN_ALERT" for t, _ in first)
    again = portfolio_events.changes_between(crossed, make_view(drawdown_pct="0.13"))
    assert not any(t == "DRAWDOWN_ALERT" for t, _ in again)


def test_a_recovery_is_reported_as_well_as_a_deepening() -> None:
    events = portfolio_events.changes_between(
        make_view(drawdown_pct="0.12"), make_view(drawdown_pct="0.01")
    )
    alerts = [payload for t, payload in events if t == "DRAWDOWN_ALERT"]
    assert alerts and alerts[0]["direction"] == "recovered"


def test_a_drawdown_alert_says_it_is_not_a_risk_limit() -> None:
    events = portfolio_events.changes_between(make_view(), make_view(drawdown_pct="0.12"))
    alerts = [payload for t, payload in events if t == "DRAWDOWN_ALERT"]
    assert "RISK ENGINE" in alerts[0]["authority"]


def test_an_exposure_change_is_its_own_event() -> None:
    events = portfolio_events.changes_between(make_view(), make_view(positions=[view_of()]))
    types = [t for t, _ in events]
    assert "EXPOSURE_UPDATED" in types
    payload = next(p for t, p in events if t == "EXPOSURE_UPDATED")
    assert payload["positions"] == 1
    # One long, so gross and net agree here -- and both are carried anyway,
    # because a payload that shipped only the one that happened to match would
    # be wrong the moment a short opened.
    assert payload["gross"] == payload["net"]


def test_a_health_change_is_its_own_event() -> None:
    events = portfolio_events.changes_between(make_view(), make_view(health=PortfolioHealth.stale))
    types = [t for t, _ in events]
    assert "PORTFOLIO_HEALTH_CHANGED" in types


def test_every_portfolio_event_is_in_the_catalogue() -> None:
    from app.realtime.catalogue import PRODUCED_NOW, EventType, is_known, scope_of

    for name in (
        "PORTFOLIO_UPDATED",
        "EXPOSURE_UPDATED",
        "DRAWDOWN_ALERT",
        "PORTFOLIO_HEALTH_CHANGED",
    ):
        assert is_known(name)
        assert str(scope_of(name)) == "account"
        assert EventType(name) in PRODUCED_NOW


async def test_publishing_survives_a_broken_hub(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A read does not fail because a socket did."""

    class Broken:
        async def publish(self, event: Any) -> None:
            raise RuntimeError("bus down")

    sent = await PortfolioService().publish(Broken(), previous=None, current=make_view())
    assert sent == []


# =========================================================== security, §55, §63


def _names(path: Path) -> set[str]:
    """Every identifier the module actually references, parsed, never grepped.

    A docstring explaining a rule fails a grep of that rule -- the defect L27
    found and L28 tripped over.
    """
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
    return sorted(PORTFOLIO.glob("*.py")) + [ROUTER]


def test_the_portfolio_engine_cannot_place_or_modify_anything() -> None:
    """§63. Parsed, because a promise in a docstring is not a constraint."""
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


def test_the_portfolio_engine_imports_no_execution_path() -> None:
    """§63. The one-way direction: risk reads portfolio, never the reverse."""
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
    )
    for path in _sources():
        for module in _imports(path):
            assert not any(module == bad or module.startswith(bad + ".") for bad in forbidden), (
                f"{path.name} imports {module}"
            )


def test_the_portfolio_engine_cannot_enable_live_trading() -> None:
    """No level may flip this, and this one has no way to reach the switch."""
    forbidden = {"LIVE_TRADING", "live_trading", "LIVE_GATES", "live_execution_allowed"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"
        assert "app.core.settings" not in _imports(path)


def test_no_response_carries_a_credential() -> None:
    """§55. There is no field to redact, because none was ever copied."""
    forbidden = {"password", "login", "api_key", "secret", "token", "credential"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


def test_an_account_state_has_no_credential_field() -> None:
    assert not {"login", "password", "api_key", "token"} & set(AccountState.__dataclass_fields__)


def test_a_broker_account_is_copied_without_its_login() -> None:
    """The adapter's `Account` carries a login; the portfolio state does not."""
    state = from_broker_account(FakeAccount(), account_id="broker1", environment="demo", as_of=NOW)
    described = state.as_dict()
    assert "8675309" not in str(described)
    assert described["balance"] == "50000"
    assert described["broker"] == "Broker-Demo"


def test_a_broker_balance_is_never_recomputed() -> None:
    """§6 and §7. Deriving equity here would double-count what the broker did."""
    state = from_broker_account(FakeAccount(), account_id="broker1", environment="demo", as_of=NOW)
    assert state.equity == Decimal("50250")
    assert state.balance == Decimal("50000")


def test_the_portfolio_engine_never_deletes_history() -> None:
    """The brief's NEVER list: no drop, no truncate, no delete of a record."""
    forbidden = {"delete", "drop_all", "drop_table", "truncate", "execute"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


def test_the_router_is_read_only() -> None:
    """Every verb is GET; a POST here would be a portfolio that acts."""
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    verbs = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "router"
    }
    assert verbs == {"get"}


# ============================================================ correlation §27


def test_correlation_is_reported_unavailable_rather_than_approximated() -> None:
    """§27. Do not create a fake correlation engine.

    Asserts the PROPERTY -- unavailable, with a reason a reader can check and a
    named substitute -- rather than the wording. The first version required the
    string "market_bars" and failed at L66 when the reason was corrected: the
    table had stopped being empty (700 EURUSD bars) while the verdict stayed
    right, because correlation needs two instruments and there is one. A test
    that pins the sentence blocks the sentence being fixed.
    """
    note = exposure_engine.correlation_note()
    assert note["available"] is False
    assert len(note["reason"]) > 40, "an unavailable verdict must say why"
    assert "correlation" in note["reason"].lower()
    assert "currency breakdown" in note["closest_available"]


# =============================================================== paper state


def test_a_paper_row_is_stamped_with_when_it_was_last_written() -> None:
    """§44 applies to the simulator too: paper is held to the same rule."""
    row = PaperAccount(
        id="p",
        user_id="u1",
        name="p",
        currency="USD",
        starting_balance=Decimal("1"),
        balance=Decimal("1"),
        equity=Decimal("1"),
    )
    row.updated_at = NOW - timedelta(hours=1)
    state = from_paper_account(row)
    assert state.as_of == NOW - timedelta(hours=1)
    assert (
        account_state.freshness_of(state.as_of, now=NOW, tolerance=timedelta(seconds=60))
        is Freshness.stale
    )


# ================================================================ the API


@pytest.fixture
async def app(settings: Any) -> Any:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401
    from app.db.base import Base as _Base
    from app.main import create_app

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    yield application
    await engine.dispose()


@pytest.fixture
async def api(app: Any) -> Any:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.post(
            "/auth/register", json={"email": "alice@example.com", "password": "Sufficient-1-pass"}
        )
        yield client


async def seed_account(app: Any, *, owner_email: str = "alice@example.com") -> str:
    from app.auth.models import User
    from sqlalchemy import select as _select

    async with app.state.session_factory() as db:
        user = await db.scalar(_select(User).where(User.email == owner_email))
        assert user is not None
        account = PaperAccount(
            user_id=user.id,
            name="paper",
            currency="USD",
            starting_balance=Decimal("100000"),
            balance=Decimal("100000"),
            equity=Decimal("100000"),
            status="active",
        )
        db.add(account)
        await db.commit()
        return account.id


async def test_the_portfolio_route_serves_a_real_account(app: Any, api: Any) -> None:
    account_id = await seed_account(app)
    response = await api.get("/v1/portfolio", params={"account_id": account_id})
    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "paper"
    assert body["account"]["balance"] == "100000.0000"
    assert body["sources"]["account"] == "PAPER_ENGINE"


async def test_every_portfolio_route_refuses_anonymous_access(app: Any) -> None:
    from httpx import ASGITransport, AsyncClient

    account_id = "whatever"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        for path in (
            "/v1/portfolio",
            "/v1/portfolio/summary",
            "/v1/portfolio/account",
            "/v1/portfolio/positions",
            "/v1/portfolio/exposure",
            "/v1/portfolio/pnl",
            "/v1/portfolio/drawdown",
            "/v1/portfolio/margin",
            "/v1/portfolio/history",
            "/v1/portfolio/reconciliation",
            "/v1/portfolio/risk-state",
            "/v1/portfolio/by-symbol",
            "/v1/portfolio/by-strategy",
            "/v1/portfolio/by-bot",
        ):
            response = await anonymous.get(path, params={"account_id": account_id})
            assert response.status_code == 401, path


async def test_another_users_account_is_a_404_not_a_403(app: Any, api: Any) -> None:
    """§55. Distinguishing them would make this route a membership oracle."""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as other:
        await other.post(
            "/auth/register", json={"email": "bob@example.com", "password": "Sufficient-1-pass"}
        )
        theirs = await seed_account(app, owner_email="bob@example.com")

    mine = await api.get("/v1/portfolio", params={"account_id": theirs})
    missing = await api.get("/v1/portfolio", params={"account_id": "does-not-exist"})
    assert mine.status_code == 404
    assert missing.status_code == 404
    assert mine.json()["error"] == missing.json()["error"] or (
        mine.json()["error"]["code"] == missing.json()["error"]["code"]
    )


async def test_the_summary_route_replaces_its_501_stub(app: Any, api: Any) -> None:
    account_id = await seed_account(app)
    response = await api.get("/v1/portfolio/summary", params={"account_id": account_id})
    assert response.status_code == 200
    assert "health" in response.json()


async def test_an_empty_portfolio_is_zero_positions_not_an_error(app: Any, api: Any) -> None:
    """§51's shape: no positions is a fact, and it is reported as one."""
    account_id = await seed_account(app)
    response = await api.get("/v1/portfolio/positions", params={"account_id": account_id})
    assert response.status_code == 200
    assert response.json() == {
        "environment": "paper",
        "positions": [],
        "count": 0,
        "unmarked": [],
    }


async def test_history_paginates(app: Any, api: Any) -> None:
    account_id = await seed_account(app)
    async with app.state.session_factory() as db:
        for day in range(5):
            db.add(
                PortfolioSnapshot(
                    paper_account_id=account_id,
                    mode="paper",
                    taken_at=NOW_UTC - timedelta(days=day),
                    balance=Decimal("100000"),
                    equity=Decimal("100000") - Decimal(day * 10),
                )
            )
        await db.commit()
    response = await api.get("/v1/portfolio/history", params={"account_id": account_id, "limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["page"]["total"] == 5


async def test_no_route_response_contains_a_credential(app: Any, api: Any) -> None:
    account_id = await seed_account(app)
    for path in ("/v1/portfolio", "/v1/portfolio/account", "/v1/portfolio/summary"):
        body = (await api.get(path, params={"account_id": account_id})).text.lower()
        for forbidden in ("password", "api_key", "secret", '"login"'):
            assert forbidden not in body, f"{path} leaked {forbidden}"


async def test_the_risk_state_route_names_what_it_does_not_own(app: Any, api: Any) -> None:
    account_id = await seed_account(app)
    body = (await api.get("/v1/portfolio/risk-state", params={"account_id": account_id})).json()
    assert "RISK ENGINE decides" in body["authority"]
    assert "market_open" in body["risk_state"]["not_supplied"]


# ============ L76 §5: the two freshness states that did not exist


#: `Input.freshness` makes its own timestamp aware, so `now` must be too.
NOW_UTC = NOW.replace(tzinfo=UTC)

from app.portfolio.decision import DEFAULT_MAX_AGE  # noqa: E402


def _input(**over: object):  # noqa: ANN202
    """An `Input`, shaped by what `freshness` reads."""
    from app.portfolio.decision import Input

    base = {
        "name": "equity",
        "source": "broker",
        "value": Decimal("1000"),
        "at": NOW_UTC - timedelta(seconds=30),
    }
    base.update(over)
    return Input(**base)  # type: ignore[arg-type]


def test_a_contested_input_is_not_stale_and_not_missing() -> None:
    """The state that could not previously be expressed.

    An input whose sources disagree had to be mislabelled as one of the other
    three. The distinction is the one L74 §52 and L75 §6 both require: a stale
    figure can be refreshed by asking again, a contested one cannot.
    """
    from app.portfolio.decision import Freshness

    assert _input().freshness(now=NOW_UTC) is Freshness.FRESH
    assert _input(conflicted=True).freshness(now=NOW_UTC) is Freshness.CONFLICTED


def test_conflict_outranks_staleness_but_not_absence() -> None:
    """Ordered by how little the input can be relied on."""
    from app.portfolio.decision import Freshness

    old = NOW_UTC - timedelta(hours=3)
    assert _input(at=old, conflicted=True).freshness(now=NOW_UTC) is Freshness.CONFLICTED
    # Nothing to be in conflict about.
    assert _input(value=None, conflicted=True).freshness(now=NOW_UTC) is Freshness.MISSING
    assert _input(at=None, conflicted=True).freshness(now=NOW_UTC) is Freshness.INVALID


def test_aging_is_never_produced_without_an_explicit_threshold() -> None:
    """No default, deliberately.

    `DEFAULT_MAX_AGE` already records itself as an assumption rather than a
    measurement. A second unmeasured boundary inside the first would compound
    that rather than inform anything, so `AGING` exists and is opt-in.
    """
    from app.portfolio.decision import Freshness

    middling = _input(at=NOW_UTC - timedelta(minutes=3))
    assert middling.freshness(now=NOW_UTC) is Freshness.FRESH
    assert (
        middling.freshness(now=NOW_UTC, aging_after=timedelta(minutes=1)) is Freshness.AGING
    )


def test_aging_never_masks_stale() -> None:
    """A threshold for AGING must not make an out-of-date input look better."""
    from app.portfolio.decision import Freshness

    ancient = _input(at=NOW_UTC - timedelta(hours=1))
    assert (
        ancient.freshness(now=NOW_UTC, aging_after=timedelta(minutes=1)) is Freshness.STALE
    )


def test_every_new_state_is_degraded_never_permissive() -> None:
    """Adding states can only make a decision more conservative.

    The consumers test `is not Freshness.FRESH`, so a state nobody has taught
    them about is treated as degraded rather than ignored. That is the property
    that made this extension safe to make at all.
    """
    from app.portfolio.decision import Freshness

    for state in Freshness:
        assert (state is Freshness.FRESH) == (state.value == "FRESH")


# ================== L81 §6: one threshold per KIND, not one for everything


def test_a_quote_and_a_filed_quarter_do_not_age_at_the_same_rate() -> None:
    """L81 §6 states the rule: *"Do NOT use one universal freshness threshold
    for all data types."*

    A price is evidence about a moment. A filed quarter stays true until the
    next one is filed. One five-minute default called the first fresh when it
    had been superseded many times over, and the second stale within the hour.
    """
    from app.portfolio.decision import Freshness

    an_hour = NOW_UTC - timedelta(hours=1)
    assert _input(at=an_hour, kind="quote").freshness(now=NOW_UTC) is Freshness.STALE
    assert _input(at=an_hour, kind="fundamentals").freshness(now=NOW_UTC) is Freshness.FRESH
    # And the caller who names no kind is unaffected by any of it.
    assert _input(at=an_hour).freshness(now=NOW_UTC) is Freshness.STALE


def test_an_event_bounded_input_never_goes_stale_from_age_alone() -> None:
    """Guidance is invalidated by the company changing it, not by a clock.

    It is exactly as true the day before an earnings call as the day it was
    issued, and then worthless in a minute. A duration cannot express that, and
    one chosen anyway would be wrong in both directions.
    """
    from app.portfolio.decision import Freshness

    ancient = _input(at=NOW_UTC - timedelta(days=400), kind="guidance")
    assert ancient.freshness(now=NOW_UTC) is not Freshness.STALE
    # AGING, not FRESH: nobody has checked whether the event happened. That is
    # a statement about the platform, not about the guidance.
    assert ancient.freshness(now=NOW_UTC) is Freshness.AGING

    recent = _input(at=NOW_UTC - timedelta(days=3), kind="guidance")
    assert recent.freshness(now=NOW_UTC) is Freshness.FRESH


def test_an_explicit_argument_outranks_the_policy() -> None:
    """The caller who knows this reading's shelf life beats a table written for
    its kind in general."""
    from app.portfolio.decision import Freshness

    row = _input(at=NOW_UTC - timedelta(hours=1), kind="fundamentals")
    assert row.freshness(now=NOW_UTC) is Freshness.FRESH
    assert row.freshness(now=NOW_UTC, max_age=timedelta(minutes=1)) is Freshness.STALE


def test_an_unknown_kind_gets_the_strictest_thing_available() -> None:
    """Not an exception. A caller naming a kind nobody wrote a policy for is in
    a decision path, and raising there would turn a missing table entry into an
    outage."""
    from app.portfolio.decision import Freshness, staleness_for

    assert staleness_for("no_such_kind").max_age == DEFAULT_MAX_AGE
    old = _input(at=NOW_UTC - timedelta(hours=1), kind="no_such_kind")
    assert old.freshness(now=NOW_UTC) is Freshness.STALE


def test_every_policy_entry_says_why() -> None:
    """A table of confident-looking durations with no reasoning is worse than
    one default, because it looks measured. None of these is a measurement and
    each has to say what it rests on."""
    from app.portfolio.decision import POLICY

    for kind, rule in POLICY.items():
        assert rule.why.strip(), f"{kind} has no stated reason"
        assert len(rule.why) > 40, f"{kind}'s reason is too thin to argue with"
        # An event-bounded kind must still tell somebody to look.
        if rule.max_age is None:
            assert rule.aging_after is not None, f"{kind} can never prompt a check"


def test_missing_and_invalid_still_outrank_every_policy() -> None:
    """No threshold makes an absent fact fresh."""
    from app.portfolio.decision import Freshness

    assert _input(value=None, kind="fundamentals").freshness(now=NOW_UTC) is Freshness.MISSING
    assert _input(at=None, kind="guidance").freshness(now=NOW_UTC) is Freshness.INVALID
    conflicted = _input(kind="fundamentals", conflicted=True)
    assert conflicted.freshness(now=NOW_UTC) is Freshness.CONFLICTED
