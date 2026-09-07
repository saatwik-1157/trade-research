"""Analytics and performance analytics (L32).

The ones that matter most:

  * `test_analytics_counts_exactly_what_the_journal_counts` — §41.
  * `test_a_ratio_from_too_few_trades_is_insufficient_data` — §9 and §25.
  * `test_profit_factor_with_no_losers_is_not_infinity` — §5.
  * `test_the_realized_curve_cannot_contain_a_deposit` — §7.
  * `test_two_environments_are_never_merged_into_one_curve` — §7 and §23.
  * `test_an_open_drawdown_is_not_reported_as_recovered` — §8.
  * `test_nothing_is_trimmed_from_a_distribution` — §18.
  * `test_a_comparison_shows_both_sample_sizes_and_claims_no_cause` — §14 and §24.
  * `test_analytics_cannot_place_or_modify_anything` — §49.
  * `test_latency_is_never_invented_from_a_missing_timestamp` — §16.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.analytics import equity as equity_engine
from app.analytics import metrics as metric_engine
from app.analytics import windows as window_engine
from app.analytics.metrics import INSUFFICIENT_DATA, Observation, Series, Unit, UnitMismatch
from app.analytics.service import (
    MIN_TRADES_FOR_COMPARISON,
    AnalyticsError,
    AnalyticsService,
    Scope,
)
from app.analytics.windows import Bucket, Preset, Window, WindowError
from app.db.base import Base
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.execution import Execution, Order, Trade
from app.models.journal import PortfolioSnapshot
from app.models.market import Symbol
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

ANALYTICS = Path(__file__).resolve().parents[1] / "app" / "analytics"
ROUTER = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "analytics.py"

NOW = datetime(2026, 9, 4, 12, 0, 0)
ALL_TIME = Window(None, None, "all_time")


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
        session.add(Symbol(id="sym2", code="GBPUSD", asset_class="fx"))
        await session.commit()
    yield factory
    await engine.dispose()


async def add_trade(
    db: AsyncSession,
    *,
    net: str,
    at: datetime,
    mode: str = "paper",
    account: str = "paper1",
    symbol: str = "sym1",
    r: str | None = "1.0",
    strategy: str | None = None,
    bot: str | None = None,
    exit_reason: str = "take_profit",
    commission: str = "0",
    swap: str = "0",
    held_hours: int = 1,
) -> Trade:
    row = Trade(
        mode=mode,
        status="closed",
        symbol_id=symbol,
        paper_account_id=account if mode == "paper" else None,
        broker_account_id=account if mode != "paper" else None,
        strategy_version_id=strategy,
        bot_id=bot,
        side="long",
        volume=Decimal("1"),
        entry_price=Decimal("1.1"),
        exit_price=Decimal("1.2"),
        opened_at=at - timedelta(hours=held_hours),
        closed_at=at,
        gross_profit=Decimal(net) + Decimal(commission) + Decimal(swap),
        commission=Decimal(commission),
        swap=Decimal(swap),
        net_profit=Decimal(net),
        r_multiple=None if r is None else Decimal(r),
        exit_reason=exit_reason,
        currency="USD",
        source="pipeline",
    )
    db.add(row)
    await db.flush()
    return row


def scope(**over: Any) -> Scope:
    return Scope(window=over.pop("window", ALL_TIME), **over)


# ================================================== the metric definitions §5


def test_win_rate_counts_breakeven_in_the_denominator_only() -> None:
    assert metric_engine.win_rate([1.0, -1.0, 0.0, 2.0]) == 0.5
    assert metric_engine.loss_rate([1.0, -1.0, 0.0, 2.0]) == 0.25


def test_win_rate_of_an_empty_set_is_insufficient_data_not_zero() -> None:
    """Nought wins from nought trades is not a nought percent win rate."""
    assert metric_engine.win_rate([]) == INSUFFICIENT_DATA
    assert metric_engine.expectancy([]) == INSUFFICIENT_DATA


def test_profit_factor_is_gross_profit_over_absolute_gross_loss() -> None:
    assert metric_engine.profit_factor([10.0, -5.0]) == 2.0
    assert metric_engine.gross_profit([10.0, -5.0]) == 10.0
    assert metric_engine.gross_loss([10.0, -5.0]) == 5.0


def test_profit_factor_with_no_losers_is_not_infinity() -> None:
    """§5. A big number here reads as a strong edge from a sample that never fell."""
    assert metric_engine.profit_factor([1.0, 2.0, 3.0]) == INSUFFICIENT_DATA


def test_expectancy_is_the_average_trade() -> None:
    assert metric_engine.expectancy([10.0, -5.0, 1.0]) == 2.0


def test_payoff_ratio_needs_both_a_winner_and_a_loser() -> None:
    assert metric_engine.payoff_ratio([10.0, -5.0]) == 2.0
    assert metric_engine.payoff_ratio([10.0, 5.0]) == INSUFFICIENT_DATA


def test_a_ratio_from_too_few_trades_is_insufficient_data() -> None:
    """§9 and §25. A Sharpe from six trades is noise wearing a decimal point."""
    small = [1.0, -1.0, 2.0, -0.5, 1.5, -1.2]
    assert metric_engine.sharpe_per_trade(small) == INSUFFICIENT_DATA
    assert metric_engine.sortino_per_trade(small) == INSUFFICIENT_DATA


def test_a_ratio_is_computed_once_the_sample_is_large_enough() -> None:
    values = [1.0, -0.5] * 15  # 30 trades
    assert isinstance(metric_engine.sharpe_per_trade(values), float)


def test_zero_variance_produces_insufficient_data_not_a_division() -> None:
    identical = [1.0] * 30
    assert metric_engine.sharpe_per_trade(identical) == INSUFFICIENT_DATA
    assert metric_engine.t_statistic(identical) == INSUFFICIENT_DATA


def test_a_single_trade_has_no_deviation_and_no_t() -> None:
    assert metric_engine.standard_deviation([1.0]) == INSUFFICIENT_DATA
    assert metric_engine.t_statistic([1.0]) == INSUFFICIENT_DATA


def test_all_winners_and_all_losers_both_compute() -> None:
    winners = metric_engine.core(Series.of([Observation(1.0, NOW)] * 5, Unit.r))
    losers = metric_engine.core(Series.of([Observation(-1.0, NOW)] * 5, Unit.r))
    assert winners["win_rate"] == 1.0
    assert losers["loss_rate"] == 1.0
    assert winners["profit_factor"] == INSUFFICIENT_DATA
    assert losers["profit_factor"] == 0.0


def test_streaks_are_broken_by_a_breakeven_trade() -> None:
    found = metric_engine.streaks([1.0, 1.0, 0.0, 1.0, -1.0, -1.0, -1.0])
    assert found.longest_win == 2
    assert found.longest_loss == 3
    assert found.current_loss == 3
    assert found.longest_breakeven == 1


def test_streaks_of_nothing_are_zero() -> None:
    assert metric_engine.streaks([]).longest_win == 0


def test_nothing_is_trimmed_from_a_distribution() -> None:
    """§18. The largest figures this repository produced were the unit errors."""
    values = [1.0, 1.0, 1.0, 1.0, 1.0, 500.0]
    found = metric_engine.distribution(values)
    assert found["max"] == 500.0
    assert found["count"] == 6
    assert "nothing is trimmed" in found["note"]


def test_a_distribution_below_the_floor_reports_insufficient_data() -> None:
    found = metric_engine.distribution([1.0, 2.0])
    assert found["median"] == INSUFFICIENT_DATA


def test_durations_are_exact_and_absent_when_unmeasurable() -> None:
    series = Series.of([Observation(1.0, NOW, opened_at=NOW - timedelta(hours=2))], Unit.currency)
    assert metric_engine.durations(series)["average_seconds"] == 7200.0
    assert metric_engine.durations(Series.of([Observation(1.0, NOW)], Unit.r))["available"] is False


# ==================================================== the unit, made a type


def test_two_units_cannot_be_pooled() -> None:
    """The metals-points lesson, as a type rather than a comment."""
    points = Series.of([Observation(1.0, NOW)], Unit.points)
    currency = Series.of([Observation(1.0, NOW)], Unit.currency)
    with pytest.raises(UnitMismatch):
        points.concat(currency)


def test_only_r_and_percent_are_poolable_across_instruments() -> None:
    assert Unit.r in metric_engine.POOLABLE
    assert Unit.percent in metric_engine.POOLABLE
    assert Unit.currency not in metric_engine.POOLABLE
    assert Unit.points not in metric_engine.POOLABLE


def test_the_metric_block_says_whether_it_pools() -> None:
    block = metric_engine.core(Series.of([Observation(1.0, NOW)], Unit.currency))
    assert block["unit"] == "currency"
    assert block["poolable_across_instruments"] is False


# ==================================================== equity and drawdown §7, §8


def test_the_realized_curve_cannot_contain_a_deposit() -> None:
    """§7. Every point is the sum of trade results, so nothing else can enter."""
    curve = equity_engine.Curve.realized_from(
        [(NOW, 10.0, "t1"), (NOW + timedelta(hours=1), -4.0, "t2")], environment="paper"
    )
    assert [p.value for p in curve.points] == [10.0, 6.0]
    assert "cannot contain a deposit" in curve.as_dict()["note"]


def test_the_account_curve_says_it_cannot_tell_a_deposit_from_a_profit() -> None:
    curve = equity_engine.Curve.account_from([(NOW, 100000.0, "s1")], environment="paper")
    assert "a deposit and a profit look identical" in curve.as_dict()["note"]


def test_two_environments_are_never_merged_into_one_curve() -> None:
    """§7 and §23."""
    paper = equity_engine.Curve.realized_from([(NOW, 1.0, None)], environment="paper")
    live = equity_engine.Curve.realized_from([(NOW, 1.0, None)], environment="live")
    with pytest.raises(equity_engine.EnvironmentMismatch):
        equity_engine.combine(paper, live)


def test_a_curve_is_ordered_even_when_the_input_is_not() -> None:
    """§8: drawdown must not be computed from unordered data."""
    curve = equity_engine.Curve.realized_from(
        [(NOW + timedelta(hours=2), 5.0, None), (NOW, 10.0, None)], environment="paper"
    )
    assert [p.value for p in curve.points] == [10.0, 15.0]


def test_a_rising_curve_has_no_drawdown_period() -> None:
    curve = equity_engine.Curve.realized_from(
        [(NOW + timedelta(hours=i), 1.0, None) for i in range(5)], environment="paper"
    )
    assert equity_engine.periods(curve) == []
    assert equity_engine.analyse(curve)["max_drawdown"] == 0.0


def test_a_falling_curve_has_one_open_period() -> None:
    curve = equity_engine.Curve.realized_from(
        [(NOW + timedelta(hours=i), -1.0, None) for i in range(5)], environment="paper"
    )
    found = equity_engine.periods(curve)
    assert len(found) == 1
    assert found[0].recovered_at is None


def test_an_open_drawdown_is_not_reported_as_recovered() -> None:
    """§8. Saying it recovered at the last point says the account came back."""
    curve = equity_engine.Curve.realized_from(
        [(NOW, 10.0, None), (NOW + timedelta(hours=1), -4.0, None)], environment="paper"
    )
    analysis = equity_engine.analyse(curve)
    assert analysis["in_drawdown"] is True
    assert analysis["periods"][0]["recovered"] is False
    assert analysis["periods"][0]["recovery_seconds"] == INSUFFICIENT_DATA


def test_several_drawdown_periods_are_each_reported() -> None:
    curve = equity_engine.Curve.realized_from(
        [
            (NOW + timedelta(hours=0), 10.0, None),
            (NOW + timedelta(hours=1), -5.0, None),
            (NOW + timedelta(hours=2), 10.0, None),
            (NOW + timedelta(hours=3), -8.0, None),
        ],
        environment="paper",
    )
    found = equity_engine.periods(curve)
    assert len(found) == 2
    assert found[0].recovered_at is not None
    assert found[1].recovered_at is None


def test_a_curve_with_no_points_has_no_drawdown_rather_than_zero() -> None:
    curve = equity_engine.Curve.realized_from([], environment="paper")
    assert equity_engine.analyse(curve)["available"] is False
    assert equity_engine.periods(curve) == []


def test_one_point_produces_no_period() -> None:
    curve = equity_engine.Curve.realized_from([(NOW, 1.0, None)], environment="paper")
    assert equity_engine.periods(curve) == []


def test_recovery_factor_without_a_drawdown_is_insufficient_data() -> None:
    """Dividing by zero is undefined, not impressive."""
    curve = equity_engine.Curve.realized_from([(NOW, 10.0, None)], environment="paper")
    assert equity_engine.analyse(curve)["recovery_factor"] == INSUFFICIENT_DATA


def test_drawdown_percentage_of_a_non_positive_peak_is_refused() -> None:
    """The realized curve legitimately starts at zero and can go negative."""
    period = equity_engine.Period(NOW, 0.0, NOW + timedelta(hours=1), -5.0)
    assert period.depth_pct == INSUFFICIENT_DATA


# ============================================================== windows §10, §43


def test_the_timezone_policy_names_the_trading_day_boundary() -> None:
    policy = window_engine.TIMEZONE_POLICY
    assert "day_start" in policy["trading_day"]
    assert policy["database"] == "naive datetimes that mean UTC"


def test_a_reversed_window_is_refused_not_swapped() -> None:
    with pytest.raises(WindowError):
        window_engine.resolve(None, now=NOW, start=NOW, end=NOW - timedelta(days=1))


def test_an_unknown_preset_is_refused() -> None:
    with pytest.raises(WindowError):
        window_engine.resolve("last_millennium", now=NOW)


def test_every_preset_resolves() -> None:
    for preset in Preset:
        window = window_engine.resolve(str(preset), now=NOW)
        assert window.label == str(preset)


def test_today_starts_at_the_risk_engines_boundary() -> None:
    """§43. 23:59 UTC must not become the next trading day."""
    from app.risk.state import day_start

    late = datetime(2026, 9, 4, 23, 59, 0)
    window = window_engine.resolve("today", now=late)
    from datetime import UTC

    assert window.start == day_start(late.replace(tzinfo=UTC)).replace(tzinfo=None)
    assert window.start == datetime(2026, 9, 4, 0, 0)


def test_windows_are_half_open_so_adjacent_ones_cannot_double_count() -> None:
    today = window_engine.resolve("today", now=NOW)
    yesterday = window_engine.resolve("yesterday", now=NOW)
    assert yesterday.end == today.start
    assert today.as_dict()["bounds"] == "half-open [start, end)"


def test_bucket_keys_are_stable_and_computed_from_utc() -> None:
    assert window_engine.key_for(NOW, Bucket.day) == "2026-09-04"
    assert window_engine.key_for(NOW, Bucket.month) == "2026-09"
    assert window_engine.key_for(NOW, Bucket.quarter) == "2026-Q3"
    assert window_engine.key_for(NOW, Bucket.year) == "2026"
    assert window_engine.key_for(NOW, Bucket.hour) == "2026-09-04T12"


# ============================================================ the service


async def test_analytics_counts_exactly_what_the_journal_counts(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§41. A fan-out join would make analytics disagree with the journal."""
    async with sessions() as db:
        for index in range(5):
            await add_trade(db, net="10", at=NOW - timedelta(hours=index))
        await db.commit()
        journal_count = int(await db.scalar(select(func.count(Trade.id))) or 0)
        block = await AnalyticsService().summary(db, scope())
    assert block["trade_count"] == journal_count == 5
    assert block["currency"]["trades"] == 5


async def test_an_empty_filter_is_an_empty_block_not_an_error(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§48. An empty state, never a fabricated one."""
    async with sessions() as db:
        block = await AnalyticsService().summary(db, scope())
    assert block["trade_count"] == 0
    assert block["currency"]["trades"] == 0
    assert block["currency"]["win_rate"] == INSUFFICIENT_DATA
    assert "empty_reason" in block["currency"]


async def test_both_units_are_reported_and_labelled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW, r="1.5")
        await db.commit()
        block = await AnalyticsService().summary(db, scope())
    assert block["currency"]["unit"] == "currency"
    assert block["r_multiple"]["unit"] == "r"
    assert "R pools" in block["which_to_read"]


async def test_a_trade_without_an_r_is_excluded_from_r_not_given_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Deriving R from the realised loss makes every loser exactly -1R."""
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW, r="1.0")
        await add_trade(db, net="-5", at=NOW + timedelta(hours=1), r=None)
        await db.commit()
        block = await AnalyticsService().summary(db, scope())
    assert block["trade_count"] == 2
    assert block["currency"]["trades"] == 2
    assert block["r_multiple"]["trades"] == 1


async def test_costs_are_reported_separately_and_reconcile(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§6. Never assumed zero when the data exists, and never folded."""
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW, commission="2", swap="1")
        await db.commit()
        block = await AnalyticsService().summary(db, scope())
    costs = block["costs"]
    assert costs["commission"] == 2.0
    assert costs["swap"] == 1.0
    assert costs["gross_profit"] == 13.0
    assert costs["net_profit"] == 10.0
    assert costs["reconciles"] is True


async def test_the_environment_is_never_defaulted(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§23. Defaulting would hide live trades from somebody who asked for all."""
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW, mode="paper")
        await add_trade(db, net="20", at=NOW, mode="demo", account="broker1")
        await db.commit()
        everything = await AnalyticsService().summary(db, scope())
        paper = await AnalyticsService().summary(db, scope(environment="paper"))
    assert everything["environments"] == {"paper": 1, "demo": 1}
    assert paper["environments"] == {"paper": 1}


async def test_a_small_sample_carries_a_warning(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW)
        await db.commit()
        block = await AnalyticsService().summary(db, scope())
    assert "sample_warning" in block
    assert "9.33" in block["sample_warning"]


async def test_a_window_excludes_its_end_boundary(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§41 and §43. Summing adjacent windows must not double-count."""
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW)
        await db.commit()
        inside = await AnalyticsService().summary(
            db, scope(window=Window(NOW - timedelta(hours=1), NOW + timedelta(seconds=1)))
        )
        excluded = await AnalyticsService().summary(
            db, scope(window=Window(NOW - timedelta(hours=1), NOW))
        )
    assert inside["trade_count"] == 1
    assert excluded["trade_count"] == 0


async def test_an_unknown_symbol_filter_is_refused_not_ignored(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        with pytest.raises(AnalyticsError):
            await AnalyticsService().summary(db, scope(symbol="NOTREAL"))


# ============================================================== breakdowns


async def test_a_breakdown_carries_a_sample_size_per_group(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§24. A difference between 43 trades and 11 is not a finding."""
    async with sessions() as db:
        for _ in range(3):
            await add_trade(db, net="10", at=NOW, strategy="sv-a")
        await add_trade(db, net="-5", at=NOW, strategy="sv-b")
        await db.commit()
        block = await AnalyticsService().by(db, scope(), "strategy")
    assert block["groups"]["sv-a"]["trades"] == 3
    assert block["groups"]["sv-b"]["trades"] == 1
    assert block["groups"]["sv-b"]["below_comparison_floor"] is True
    assert block["total_trades"] == 4


async def test_grouped_totals_agree_with_the_ungrouped_summary(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The same rows, so they cannot disagree."""
    async with sessions() as db:
        for index in range(6):
            await add_trade(
                db,
                net="10",
                at=NOW - timedelta(hours=index),
                symbol="sym1" if index % 2 else "sym2",
            )
        await db.commit()
        summary = await AnalyticsService().summary(db, scope())
        grouped = await AnalyticsService().by(db, scope(), "symbol")
    assert sum(g["trades"] for g in grouped["groups"].values()) == summary["trade_count"]


async def test_an_unattributed_group_is_named_not_dropped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW, strategy=None)
        await db.commit()
        block = await AnalyticsService().by(db, scope(), "strategy")
    assert "unattributed" in block["groups"]


async def test_an_unknown_dimension_is_refused(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        with pytest.raises(AnalyticsError):
            await AnalyticsService().by(db, scope(), "phase_of_the_moon")


async def test_a_symbol_breakdown_uses_the_normalized_code(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§12. Not the broker's name and not TradingView's."""
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW, symbol="sym1")
        await db.commit()
        block = await AnalyticsService().by(db, scope(), "symbol")
    assert "EURUSD" in block["groups"]


async def test_time_breakdown_buckets_by_the_requested_grain(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        await add_trade(db, net="10", at=datetime(2026, 9, 4, 12))
        await add_trade(db, net="20", at=datetime(2026, 9, 5, 12))
        await db.commit()
        daily = await AnalyticsService().time_breakdown(db, scope(), Bucket.day)
        monthly = await AnalyticsService().time_breakdown(db, scope(), Bucket.month)
    assert set(daily["buckets"]) == {"2026-09-04", "2026-09-05"}
    assert set(monthly["buckets"]) == {"2026-09"}


# ============================================================ equity, service


async def test_the_equity_route_returns_both_curves(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        await add_trade(db, net="10", at=NOW)
        db.add(
            PortfolioSnapshot(
                paper_account_id="paper1",
                mode="paper",
                taken_at=NOW,
                balance=Decimal("100000"),
                equity=Decimal("100010"),
            )
        )
        await db.commit()
        block = await AnalyticsService().equity(db, scope(account_id="paper1", environment="paper"))
    assert block["realized"]["kind"] == "realized"
    assert block["account"]["available"] is True
    assert "never merged" in block["note"]


async def test_an_account_curve_needs_an_account(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        block = await AnalyticsService().equity(db, scope())
    assert block["account"]["available"] is False


async def test_the_drawdown_route_reports_periods(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        await add_trade(db, net="100", at=NOW)
        await add_trade(db, net="-40", at=NOW + timedelta(hours=1))
        await db.commit()
        block = await AnalyticsService().drawdown(db, scope())
    assert block["max_drawdown"] == 40.0
    assert block["in_drawdown"] is True


# ============================================================ execution §16


async def test_latency_is_never_invented_from_a_missing_timestamp(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§16. An order missing a stamp is excluded, never given an assumed one."""
    async with sessions() as db:
        db.add(
            Order(
                id="o1",
                intent_id="i1",
                mode="paper",
                paper_account_id="paper1",
                symbol_id="sym1",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                status="filled",
                source="pipeline",
                created_at=NOW,
                updated_at=NOW,
                submitted_at=NOW + timedelta(seconds=1),
                filled_at=NOW + timedelta(seconds=3),
            )
        )
        db.add(
            Order(
                id="o2",
                intent_id="i2",
                mode="paper",
                paper_account_id="paper1",
                symbol_id="sym1",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                status="rejected",
                source="pipeline",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await db.commit()
        block = await AnalyticsService().execution(
            db, scope(environment="paper", account_id="paper1")
        )
    assert block["orders"] == 2
    assert block["by_status"] == {"filled": 1, "rejected": 1}
    assert block["fill_ratio"] == 0.5
    assert block["rejection_ratio"] == 0.5
    assert "1 of 2 orders" in block["latency_seconds"]["measured_from"]


async def test_slippage_is_kept_in_points_and_not_summed_into_currency(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        db.add(
            Order(
                id="o1",
                intent_id="i1",
                mode="paper",
                paper_account_id="paper1",
                symbol_id="sym1",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                status="filled",
                source="pipeline",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        db.add(
            Execution(
                order_id="o1",
                executed_at=NOW,
                price=Decimal("1.1"),
                quantity=Decimal("1"),
                fill_source="broker",
                slippage_points=Decimal("3"),
            )
        )
        await db.commit()
        block = await AnalyticsService().execution(
            db, scope(environment="paper", account_id="paper1")
        )
        summary = await AnalyticsService().summary(db, scope())
    assert block["slippage_points"]["count"] == 1
    assert "279 points" in block["slippage_note"]
    assert isinstance(summary["costs"]["slippage"], str)


async def test_execution_with_no_orders_is_insufficient_data_not_zero(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        block = await AnalyticsService().execution(db, scope())
    assert block["orders"] == 0
    assert block["fill_ratio"] == INSUFFICIENT_DATA
    assert block["fills"] == 0


# =========================================================== comparison §24


async def test_a_comparison_shows_both_sample_sizes_and_claims_no_cause(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        for _ in range(3):
            await add_trade(db, net="10", at=NOW, strategy="sv-a")
        await add_trade(db, net="-5", at=NOW, strategy="sv-b")
        await db.commit()
        block = await AnalyticsService().compare(
            db,
            [
                scope(strategy_version_id="sv-a", label="A"),
                scope(strategy_version_id="sv-b", label="B"),
            ],
        )
    assert block["sample_sizes"] == [3, 1]
    assert block["comparable"] is False
    assert "observational" in block["language"]
    assert "never" in block["language"]
    assert block["warning"] is not None


async def test_a_comparison_needs_two_scopes(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        with pytest.raises(AnalyticsError):
            await AnalyticsService().compare(db, [scope()])


async def test_a_large_enough_comparison_is_marked_comparable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        for index in range(MIN_TRADES_FOR_COMPARISON):
            await add_trade(db, net="10", at=NOW - timedelta(minutes=index), strategy="sv-a")
            await add_trade(db, net="-5", at=NOW - timedelta(minutes=index), strategy="sv-b")
        await db.commit()
        block = await AnalyticsService().compare(
            db,
            [scope(strategy_version_id="sv-a"), scope(strategy_version_id="sv-b")],
        )
    assert block["comparable"] is True
    assert block["warning"] is None


# ================================================= portfolio delegation §54


async def test_exposure_is_delegated_to_the_portfolio_engine(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§54. A second portfolio state would eventually disagree with the first."""
    async with sessions() as db:
        block = await AnalyticsService().exposure(
            db, scope(account_id="paper1", environment="paper")
        )
    assert block["available"] is True
    assert "portfolio engine" in block["source"]
    assert "RISK ENGINE decides" in block["authority"]


async def test_exposure_without_an_account_says_so(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        block = await AnalyticsService().exposure(db, scope())
    assert block["available"] is False


async def test_positions_are_counted_here_and_valued_elsewhere(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as db:
        block = await AnalyticsService().open_positions(db, scope())
    assert block["open_positions"] == 0
    assert "VALUED by the portfolio engine" in block["note"]


# ================================================== reuse, not duplication §2


def test_the_backtest_runner_uses_the_shared_definitions() -> None:
    """§2 and §5. Three copies of one formula do not stay agreeing."""
    from app.backtest import runner

    assert runner.MIN_TRADES_FOR_RATIO == metric_engine.MIN_TRADES_FOR_RATIO
    source = (Path(__file__).resolve().parents[1] / "app" / "backtest" / "runner.py").read_text(
        encoding="utf-8"
    )
    assert "from app.analytics import metrics as _metrics" in source


def test_the_training_metrics_use_the_shared_definitions() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "training" / "metrics.py").read_text(
        encoding="utf-8"
    )
    assert "from app.analytics import metrics as _metrics" in source


def test_the_shared_definitions_agree_with_the_backtest_output() -> None:
    """The refactor must not have changed a number."""
    values = [10.0, -5.0, 3.0, -1.0, 7.0]
    assert metric_engine.profit_factor(values) == 20.0 / 6.0
    assert metric_engine.win_rate(values) == 0.6


# ============================================================ security §49


def _names(path: Path) -> set[str]:
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
    return sorted(ANALYTICS.glob("*.py")) + [ROUTER]


def test_analytics_cannot_place_or_modify_anything() -> None:
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


def test_analytics_imports_no_execution_path() -> None:
    """§49 and §56. It may not bypass risk because it cannot reach it."""
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


def test_analytics_cannot_enable_live_trading() -> None:
    forbidden = {"LIVE_TRADING", "live_trading", "LIVE_GATES", "live_execution_allowed"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"
        assert "app.core.settings" not in _imports(path)


def test_analytics_never_writes() -> None:
    """It reads. A write here would make it an owner of state."""
    forbidden = {"add", "commit", "delete", "flush", "drop_all", "truncate", "merge"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


def test_no_secret_name_is_referenced() -> None:
    forbidden = {"password", "api_key", "secret", "credential", "login"}
    for path in _sources():
        overlap = _names(path) & forbidden
        assert not overlap, f"{path.name} references {sorted(overlap)}"


def test_the_router_is_read_only() -> None:
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


async def seed(app: Any, *, owner_email: str = "alice@example.com") -> str:
    """One paper account and four trades, two strategies, one loser."""
    from app.auth.models import User

    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == owner_email))
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
        await db.flush()
        for index, (net, strategy) in enumerate(
            [("50", "sv-a"), ("30", "sv-a"), ("-20", "sv-a"), ("10", "sv-b")]
        ):
            await add_trade(
                db,
                net=net,
                at=NOW - timedelta(hours=index),
                account=account.id,
                strategy=strategy,
            )
        await db.commit()
        return account.id


async def test_the_contract_route_names_every_definition(api: Any) -> None:
    body = (await api.get("/v1/analytics")).json()
    for name in ("win_rate", "profit_factor", "expectancy", "sharpe_per_trade"):
        assert name in body["definitions"]
    assert body["definitions"]["risk_free_rate"].startswith("zero")
    assert "Not annualised" in body["definitions"]["sharpe_per_trade"]
    assert "run a backtest -- the backtest engine does" in body["does_not"]


async def test_every_analytics_route_refuses_anonymous_access(app: Any) -> None:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
        for path in (
            "/v1/analytics",
            "/v1/analytics/summary",
            "/v1/analytics/equity",
            "/v1/analytics/drawdown",
            "/v1/analytics/breakdown?dimension=strategy",
            "/v1/analytics/time-breakdown",
            "/v1/analytics/execution",
            "/v1/analytics/exposure",
            "/v1/analytics/positions",
            "/v1/analytics/compare",
        ):
            assert (await anon.get(path)).status_code == 401, path


async def test_the_summary_route_serves_real_rows(app: Any, api: Any) -> None:
    account_id = await seed(app)
    body = (await api.get("/v1/analytics/summary", params={"account_id": account_id})).json()
    assert body["trade_count"] == 4
    assert body["currency"]["winning_trades"] == 3
    assert body["currency"]["losing_trades"] == 1
    assert body["currency"]["net_profit"] == 70.0
    assert body["currency"]["profit_factor"] == 90.0 / 20.0


async def test_the_summary_of_an_empty_account_is_an_empty_state(api: Any) -> None:
    body = (await api.get("/v1/analytics/summary")).json()
    assert body["trade_count"] == 0
    assert body["currency"]["win_rate"] == "INSUFFICIENT_DATA"


async def test_another_users_account_is_a_404(app: Any, api: Any) -> None:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as other:
        await other.post(
            "/auth/register", json={"email": "bob@example.com", "password": "Sufficient-1-pass"}
        )
        theirs = await seed(app, owner_email="bob@example.com")

    mine = await api.get("/v1/analytics/summary", params={"account_id": theirs})
    missing = await api.get("/v1/analytics/summary", params={"account_id": "nope"})
    assert mine.status_code == 404
    assert missing.status_code == 404


async def test_a_reversed_window_is_a_422(api: Any) -> None:
    response = await api.get(
        "/v1/analytics/summary",
        params={"from_time": "2026-09-04T00:00:00", "to_time": "2026-09-01T00:00:00"},
    )
    assert response.status_code == 422


async def test_an_unknown_period_is_a_422(api: Any) -> None:
    response = await api.get("/v1/analytics/summary", params={"period": "last_millennium"})
    assert response.status_code == 422


async def test_an_unknown_dimension_is_a_422(api: Any) -> None:
    response = await api.get("/v1/analytics/breakdown", params={"dimension": "nonsense"})
    assert response.status_code == 422


async def test_an_unknown_bucket_is_a_422(api: Any) -> None:
    response = await api.get("/v1/analytics/time-breakdown", params={"bucket": "fortnight"})
    assert response.status_code == 422


async def test_the_breakdown_route_carries_sample_sizes(app: Any, api: Any) -> None:
    account_id = await seed(app)
    body = (
        await api.get(
            "/v1/analytics/breakdown",
            params={"dimension": "strategy", "account_id": account_id},
        )
    ).json()
    assert body["groups"]["sv-a"]["trades"] == 3
    assert body["groups"]["sv-b"]["trades"] == 1
    assert body["groups"]["sv-b"]["below_comparison_floor"] is True


async def test_the_equity_route_returns_two_labelled_curves(app: Any, api: Any) -> None:
    account_id = await seed(app)
    body = (await api.get("/v1/analytics/equity", params={"account_id": account_id})).json()
    assert body["realized"]["kind"] == "realized"
    assert body["realized"]["count"] == 4
    assert "never merged" in body["note"]


async def test_the_drawdown_route_reports_an_open_period(app: Any, api: Any) -> None:
    account_id = await seed(app)
    body = (await api.get("/v1/analytics/drawdown", params={"account_id": account_id})).json()
    assert body["available"] is True
    assert body["period_count"] >= 1


async def test_the_compare_route_refuses_to_claim_a_cause(app: Any, api: Any) -> None:
    account_id = await seed(app)
    body = (
        await api.get(
            "/v1/analytics/compare",
            params={
                "account_id": account_id,
                "left_strategy_version_id": "sv-a",
                "right_strategy_version_id": "sv-b",
            },
        )
    ).json()
    assert body["sample_sizes"] == [3, 1]
    assert "observational" in body["language"]
    assert body["comparable"] is False


async def test_the_execution_route_answers_with_no_orders(api: Any) -> None:
    body = (await api.get("/v1/analytics/execution")).json()
    assert body["orders"] == 0
    assert body["fill_ratio"] == "INSUFFICIENT_DATA"


async def test_no_response_contains_a_credential(app: Any, api: Any) -> None:
    account_id = await seed(app)
    for path in ("/v1/analytics", "/v1/analytics/summary", "/v1/analytics/equity"):
        text = (await api.get(path, params={"account_id": account_id})).text.lower()
        for forbidden in ("password", "api_key", '"secret"', '"login"'):
            assert forbidden not in text, f"{path} leaked {forbidden}"


async def test_a_backtest_belonging_to_someone_else_is_a_404(api: Any) -> None:
    assert (await api.get("/v1/analytics/backtest/does-not-exist")).status_code == 404


async def test_the_summary_matches_the_journals_own_count(app: Any, api: Any) -> None:
    """§41 end to end: the two surfaces must agree."""
    account_id = await seed(app)
    analytics = (await api.get("/v1/analytics/summary", params={"account_id": account_id})).json()
    journal = (await api.get("/v1/trades", params={"account_id": account_id, "limit": 50})).json()
    assert analytics["trade_count"] == journal["page"]["total"] == 4
