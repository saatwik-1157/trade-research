"""The Risk Engine as the central safety authority.

Five tests carry this level:

  * `test_the_oms_cannot_be_reached_without_an_approval` — the bypass test.
    Strategy, frontend and AI all fail to reach the OMS without Risk.
  * `test_two_concurrent_orders_cannot_both_take_the_same_headroom` — the race.
    Two individually-safe orders that are collectively unsafe.
  * `test_a_lock_survives_a_restart` — a new process reads the lock back.
  * `test_the_risk_engine_fails_closed` — every failure mode is a refusal.
  * `test_an_approval_does_not_authorise_a_different_order` — the binding.

`app/risk/engine.py` acquired its first tests at L16, in `test_paper.py`, when
that level became its first consumer. This file covers what L17 added: the
decision record, the configuration hierarchy, the latched state, persistence,
concurrency and the API.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.risk.config import (
    COMBINE,
    ConfigurationInvalid,
    Layer,
    Scope,
    check_valid,
    resolve,
    validate,
)
from app.risk.decision import (
    BOUND_FIELDS,
    PERMITTING,
    RejectionCode,
    RiskOutcome,
    request_hash,
)
from app.risk.engine import (
    Approval,
    LimitKind,
    OrderProposal,
    PortfolioState,
    RiskDecision,
    RiskEngine,
    RiskLimits,
)
from app.risk.service import CODE_FOR, LATCHING, RiskRequest, RiskService
from app.risk.state import (
    NEEDS_AUTHORISED_RESET,
    AccountRiskState,
    ResetNotPermitted,
    RiskState,
    day_start,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
BOB = {"email": "bob@tr-platform.io", "password": "another good passphrase"}


def proposal(**overrides: object) -> OrderProposal:
    base: dict[str, object] = {
        "symbol": "EURUSD",
        "side": "buy",
        "mode": "paper",
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


# ================================================== the decision vocabulary


def test_every_limit_has_a_rejection_code() -> None:
    """A caller never gets "risk rejected" and has to guess."""
    missing = [str(k) for k in LimitKind if k not in CODE_FOR]
    assert missing == [], f"limits with no rejection code: {missing}"


def test_every_limit_has_a_combination_rule() -> None:
    """A limit added later cannot pick its precedence behaviour by accident."""
    missing = [f.name for f in fields(RiskLimits) if f.name not in COMBINE]
    assert missing == [], f"limits with no combination rule: {missing}"


def test_permitting_outcomes_are_explicit() -> None:
    """Not `!= rejected`: a new outcome is refused until someone decides."""
    assert PERMITTING == {RiskOutcome.approved, RiskOutcome.approved_with_adjustment}
    assert RiskOutcome.error not in PERMITTING
    assert RiskOutcome.halted not in PERMITTING


# ====================================================== THE BYPASS TEST


def test_the_oms_cannot_be_reached_without_an_approval() -> None:
    """Strategy, frontend and AI all fail to reach the OMS without Risk.

    Server-side and by type: `PaperOMS.submit` takes a `risk.Approval` as its
    first positional argument, and `RiskEngine.approve` is the only producer of
    one in the codebase.
    """
    from app.backtest.config import CostModel
    from app.paper.execution import PaperExecution
    from app.paper.oms import OrderRefused, PaperOMS

    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))

    # A strategy with a signal.
    with pytest.raises(TypeError):
        oms.submit(order_id="o", intent_id="i", account_id="a")  # type: ignore[call-arg]

    # A frontend posting a plain dict.
    for impostor in ({"approved": True}, "approved", None, 1, object()):
        with pytest.raises(OrderRefused):
            oms.submit(impostor, order_id="o", intent_id="i", account_id="a")  # type: ignore[arg-type]

    # An AI layer that "decided" the trade is fine.
    class AiApproval:
        approved = True
        verdict = None

    with pytest.raises(OrderRefused):
        oms.submit(AiApproval(), order_id="o", intent_id="i", account_id="a")  # type: ignore[arg-type]


def test_a_veto_cannot_be_smuggled_inside_a_hand_built_approval() -> None:
    from app.backtest.config import CostModel
    from app.paper.execution import PaperExecution
    from app.paper.oms import OrderRefused, PaperOMS

    engine = RiskEngine(RiskLimits(max_open_positions=0))
    _, verdict = engine.approve(proposal(), PortfolioState(open_positions=3))
    assert not verdict.approved
    forged = Approval(verdict=verdict, approved_volume=Decimal("1"), approved_at=verdict.at)
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    with pytest.raises(OrderRefused, match="verdict"):
        oms.submit(forged, order_id="o", intent_id="i", account_id="a")


# ============================================== APPROVAL BINDING AND EXPIRY


def test_an_approval_does_not_authorise_a_different_order() -> None:
    """Risk approves order A; something submits a bigger order B. Refused."""
    from app.backtest.config import CostModel
    from app.paper.execution import PaperExecution
    from app.paper.oms import OrderRefused, PaperOMS

    engine = RiskEngine(RiskLimits(require_stop_loss=False))
    approval, verdict = engine.approve(proposal(volume=Decimal("1")))
    assert approval is not None and approval.request_hash

    # The same approval, now describing a 10x order.
    tampered = replace(
        approval,
        verdict=replace(verdict, proposal=proposal(volume=Decimal("10"))),
        approved_volume=Decimal("10"),
    )
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    with pytest.raises(OrderRefused, match="does not bind"):
        oms.submit(tampered, order_id="o", intent_id="i", account_id="acct-a")

    # The untampered one is fine.
    assert oms.submit(approval, order_id="o", intent_id="i", account_id="acct-a").created


@pytest.mark.parametrize("field", ["symbol", "side", "stop_loss", "take_profit", "strategy_id"])
def test_changing_any_bound_field_invalidates_the_approval(field: str) -> None:
    from app.backtest.config import CostModel
    from app.paper.execution import PaperExecution
    from app.paper.oms import OrderRefused, PaperOMS

    engine = RiskEngine(RiskLimits(require_stop_loss=False))
    approval, verdict = engine.approve(proposal())
    assert approval is not None
    changed = {
        "symbol": "GBPUSD",
        "side": "sell",
        "stop_loss": Decimal("1.0900"),
        "take_profit": Decimal("1.2000"),
        "strategy_id": "something_else",
    }[field]
    tampered = replace(approval, verdict=replace(verdict, proposal=proposal(**{field: changed})))
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    with pytest.raises(OrderRefused, match="does not bind"):
        oms.submit(tampered, order_id="o", intent_id="i", account_id="acct-a")


def test_an_expired_approval_is_refused() -> None:
    from app.backtest.config import CostModel
    from app.paper.execution import PaperExecution
    from app.paper.oms import OrderRefused, PaperOMS

    engine = RiskEngine(RiskLimits(require_stop_loss=False))
    approval, _ = engine.approve(proposal(), now=T0)
    assert approval is not None and approval.expires_at is not None
    oms = PaperOMS(PaperExecution(CostModel(spread_points=Decimal("0.0002"))))
    with pytest.raises(OrderRefused, match="expired"):
        oms.submit(
            approval,
            order_id="o",
            intent_id="i",
            account_id="acct-a",
            at=approval.expires_at + timedelta(seconds=1),
        )


def test_the_bound_fields_are_the_ones_that_make_an_order() -> None:
    assert set(BOUND_FIELDS) == {
        "account_id",
        "symbol",
        "side",
        "volume",
        "order_type",
        "entry_price",
        "stop_loss",
        "take_profit",
        "strategy_id",
        "mode",
    }


def test_equal_quantities_hash_equal() -> None:
    """`Decimal("1.0")` and `Decimal("1.00")` are the same quantity."""
    a = request_hash({"volume": Decimal("1.0")})
    b = request_hash({"volume": Decimal("1.00")})
    assert a == b
    assert a != request_hash({"volume": Decimal("2")})


# =================================================== THE CLOCK, FIXED AT L17


def test_the_risk_engine_uses_an_aware_clock() -> None:
    """It defaulted to the naive `utcnow()` until L17, and comparing that with
    an aware signal time raised at the first freshness check."""
    _, verdict = RiskEngine(RiskLimits()).approve(proposal())
    assert verdict.at.tzinfo is not None


def test_a_signal_freshness_limit_is_derived_from_the_timeframe() -> None:
    """The fixed 300s default vetoed every H1 strategy, live, at L16."""
    service = RiskService()
    signalled = RiskRequest(proposal(signal_time=T0), timeframe_seconds=3600)
    limits = service._with_signal_age(RiskLimits(), signalled)
    assert limits.max_signal_age_seconds == 7200.0

    # A caller who states one keeps it.
    stated = service._with_signal_age(RiskLimits(max_signal_age_seconds=30.0), signalled)
    assert stated.max_signal_age_seconds == 30.0

    # A MANUAL order has no signal and therefore no age. Deriving a limit here
    # would veto every hand-placed order for having no timestamp, which is a
    # different thing from having a stale one.
    manual = service._with_signal_age(RiskLimits(), RiskRequest(proposal(), timeframe_seconds=3600))
    assert manual.max_signal_age_seconds is None


def test_an_h1_signal_is_not_vetoed_for_being_an_hour_old() -> None:
    engine = RiskEngine(RiskLimits(require_stop_loss=False, max_signal_age_seconds=7200.0))
    approval, verdict = engine.approve(proposal(signal_time=T0 - timedelta(minutes=55)), now=T0)
    assert approval is not None, verdict.reason


# ============================================ THE CHECK THAT NEVER RAN


def test_market_open_is_now_actually_evaluated() -> None:
    """`LimitKind.market_open` was declared and never checked. From the
    outside, an enum member with no check reads exactly like an enforced one."""
    engine = RiskEngine(RiskLimits(require_market_open=True, require_stop_loss=False))
    approval, verdict = engine.approve(proposal(), PortfolioState(market_open=False))
    assert approval is None
    assert LimitKind.market_open in {c.limit for c in verdict.failed}

    ok, _ = engine.approve(proposal(), PortfolioState(market_open=True))
    assert ok is not None


def test_an_unknown_market_status_vetoes_rather_than_assuming_open() -> None:
    engine = RiskEngine(RiskLimits(require_market_open=True, require_stop_loss=False))
    approval, verdict = engine.approve(proposal(), PortfolioState(market_open=None))
    assert approval is None
    assert "unknown" in verdict.reason


# ============================================================== NEW CHECKS


@pytest.mark.parametrize(
    ("limits", "state", "expect"),
    [
        (
            RiskLimits(max_position_size=Decimal("0.5"), require_stop_loss=False),
            PortfolioState(),
            LimitKind.max_position_size,
        ),
        (
            RiskLimits(max_concentration_pct=Decimal("10"), require_stop_loss=False),
            PortfolioState(equity=Decimal("1000"), largest_position_value=Decimal("500")),
            LimitKind.max_concentration,
        ),
        (
            RiskLimits(max_margin_utilisation_pct=Decimal("50"), require_stop_loss=False),
            PortfolioState(equity=Decimal("1000"), margin_used=Decimal("900")),
            LimitKind.margin,
        ),
        (
            RiskLimits(max_trades_per_minute=2, require_stop_loss=False),
            PortfolioState(trades_last_minute=5),
            LimitKind.trade_frequency,
        ),
        (
            RiskLimits(max_trades_per_hour=10, require_stop_loss=False),
            PortfolioState(trades_last_hour=10),
            LimitKind.trade_frequency,
        ),
        (
            RiskLimits(min_risk_reward=Decimal("3"), require_stop_loss=False),
            PortfolioState(),
            LimitKind.risk_reward,
        ),
        (
            RiskLimits(market_data_max_age_seconds=60.0, require_stop_loss=False),
            PortfolioState(market_data_age_seconds=3600.0),
            LimitKind.market_data,
        ),
        (
            RiskLimits(require_stop_loss=False),
            PortfolioState(account_state="disabled"),
            LimitKind.account_state,
        ),
        (
            RiskLimits(require_stop_loss=False),
            PortfolioState(bot_state="paused"),
            LimitKind.bot_state,
        ),
        (
            RiskLimits(require_stop_loss=False),
            PortfolioState(strategy_enabled=False),
            LimitKind.strategy_state,
        ),
        (
            RiskLimits(require_stop_loss=False),
            PortfolioState(symbol_tradable=False),
            LimitKind.symbol_state,
        ),
    ],
)
def test_each_new_check_vetoes(
    limits: RiskLimits, state: PortfolioState, expect: LimitKind
) -> None:
    approval, verdict = RiskEngine(limits).approve(proposal(), state)
    assert approval is None, f"{expect} did not veto"
    assert expect in {c.limit for c in verdict.failed}, verdict.reason


def test_a_cooldown_blocks_a_trade_that_is_too_soon() -> None:
    engine = RiskEngine(RiskLimits(cooldown_seconds=300, require_stop_loss=False))
    blocked, verdict = engine.approve(
        proposal(), PortfolioState(last_trade_at=T0 - timedelta(seconds=30)), now=T0
    )
    assert blocked is None
    assert LimitKind.cooldown in {c.limit for c in verdict.failed}
    allowed, _ = engine.approve(
        proposal(), PortfolioState(last_trade_at=T0 - timedelta(seconds=600)), now=T0
    )
    assert allowed is not None


def test_a_runaway_strategy_is_stopped_by_the_frequency_limit() -> None:
    """500 signals in a second must not become 500 orders."""
    engine = RiskEngine(RiskLimits(max_trades_per_minute=3, require_stop_loss=False))
    approved = 0
    for i in range(500):
        ok, _ = engine.approve(proposal(), PortfolioState(trades_last_minute=i))
        approved += ok is not None
    assert approved == 3, approved


def test_an_unenforced_limit_is_never_read_as_a_pass() -> None:
    _, verdict = RiskEngine(RiskLimits(require_stop_loss=False)).approve(proposal())
    assert verdict.not_enforced
    assert "max_daily_loss" in verdict.not_enforced


# ================================================ CONFIGURATION HIERARCHY


def test_the_most_restrictive_limit_wins_not_the_most_specific() -> None:
    """The example from the brief: global 2%, account 1%, strategy asks 3%."""
    resolved = resolve(
        [
            Layer(Scope.global_, None, {"max_risk_per_trade": Decimal("2")}),
            Layer(Scope.paper_account, "acct-a", {"max_risk_per_trade": Decimal("1")}),
            Layer(Scope.strategy, "sma_cross", {"max_risk_per_trade": Decimal("3")}),
        ]
    )
    assert resolved.limits.max_risk_per_trade == Decimal("1")
    assert resolved.sources["max_risk_per_trade"] == "paper_account:acct-a"


def test_a_strategy_cannot_loosen_an_account_limit() -> None:
    tight = resolve(
        [
            Layer(Scope.paper_account, "a", {"max_open_positions": 1}),
            Layer(Scope.strategy, "s", {"max_open_positions": 99}),
        ]
    )
    assert tight.limits.max_open_positions == 1


def test_a_restricting_boolean_combines_by_or() -> None:
    resolved = resolve(
        [
            Layer(Scope.global_, None, {"require_stop_loss": False}),
            Layer(Scope.symbol, "EURUSD", {"require_stop_loss": True}),
        ]
    )
    assert resolved.limits.require_stop_loss is True


def test_a_floor_combines_by_maximum() -> None:
    """A higher minimum risk/reward is the more restrictive one."""
    resolved = resolve(
        [
            Layer(Scope.global_, None, {"min_risk_reward": Decimal("1.5")}),
            Layer(Scope.strategy, "s", {"min_risk_reward": Decimal("2.5")}),
        ]
    )
    assert resolved.limits.min_risk_reward == Decimal("2.5")


def test_the_order_of_the_layers_does_not_matter() -> None:
    """Min, max and OR are associative and commutative, so "most restrictive
    wins" is a property of the function rather than of the caller's ordering."""
    layers = [
        Layer(Scope.global_, None, {"max_daily_loss": Decimal("500"), "require_stop_loss": True}),
        Layer(Scope.paper_account, "a", {"max_daily_loss": Decimal("100")}),
        Layer(Scope.strategy, "s", {"max_open_positions": 2}),
    ]
    first = resolve(layers).limits
    for shuffled in ([layers[2], layers[0], layers[1]], [layers[1], layers[2], layers[0]]):
        assert resolve(shuffled).limits == first


def test_an_unset_limit_is_not_enforced() -> None:
    resolved = resolve([Layer(Scope.global_, None, {})])
    assert resolved.limits.max_daily_loss is None


def test_an_unknown_limit_is_refused_rather_than_ignored() -> None:
    """An ignored limit reads, to whoever set it, as an enforced one."""
    with pytest.raises(ConfigurationInvalid, match="unknown risk limits"):
        resolve([Layer(Scope.global_, None, {"max_hopes": 3})])


# ================================================ CONFIGURATION VALIDATION


@pytest.mark.parametrize(
    ("limits", "fragment"),
    [
        (RiskLimits(max_drawdown_pct=Decimal("150")), "cannot exceed 100"),
        (RiskLimits(max_daily_loss=Decimal("-5")), "must be positive"),
        (RiskLimits(max_risk_per_trade=Decimal("0")), "must be positive"),
        (RiskLimits(max_open_positions=-1), "cannot be negative"),
        (RiskLimits(max_trades_per_minute=100, max_trades_per_hour=10), "exceeds"),
        (RiskLimits(max_trades_per_hour=100, max_trades_per_day=10), "exceeds"),
    ],
)
def test_an_unsafe_configuration_is_refused(limits: RiskLimits, fragment: str) -> None:
    problems = validate(limits)
    assert problems, "no problem reported"
    assert any(fragment in p for p in problems), problems
    with pytest.raises(ConfigurationInvalid):
        check_valid(limits)


def test_validation_reports_every_problem_not_just_the_first() -> None:
    problems = validate(RiskLimits(max_drawdown_pct=Decimal("150"), max_daily_loss=Decimal("-1")))
    assert len(problems) >= 2


def test_a_valid_configuration_passes() -> None:
    assert validate(RiskLimits(max_drawdown_pct=Decimal("10"), max_open_positions=3)) == []


# ==================================================== LATCHED RISK STATE


def test_the_trading_day_is_explicit_utc() -> None:
    """Never the machine's local midnight: this project has shipped a daily
    limit that counted from 18:30 on a UTC+5:30 laptop."""
    assert day_start(T0) == datetime(2026, 5, 5, tzinfo=UTC)
    # A configured boundary, for a venue whose day starts at the rollover.
    assert day_start(T0, boundary_hour=22) == datetime(2026, 5, 4, 22, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        day_start(T0, boundary_hour=25)


def test_a_daily_lock_clears_only_when_its_day_has_ended() -> None:
    state = AccountRiskState("paper_account", "a")
    state.lock(RiskState.daily_loss_locked, "down 600", T0, day=day_start(T0))
    assert state.blocking

    state.refresh(T0 + timedelta(hours=6))
    assert state.blocking, "the lock cleared inside its own day"

    state.refresh(T0 + timedelta(days=1))
    assert not state.blocking


def test_a_drawdown_lock_does_not_clear_on_its_own() -> None:
    """A drawdown is still there tomorrow."""
    state = AccountRiskState("paper_account", "a")
    state.lock(RiskState.drawdown_locked, "down 20%", T0, day=day_start(T0))
    state.refresh(T0 + timedelta(days=30))
    assert state.blocking
    with pytest.raises(ResetNotPermitted):
        state.clear(T0 + timedelta(days=30))


def test_an_authorised_reset_must_name_who_authorised_it() -> None:
    state = AccountRiskState("paper_account", "a")
    state.lock(RiskState.emergency_stop, "drill", T0)
    with pytest.raises(ResetNotPermitted, match="who authorised"):
        state.clear(T0, force=True)
    state.clear(T0, authorised_by="admin-1", force=True)
    assert not state.blocking
    assert any("admin-1" in h["detail"] for h in state.history)


def test_a_weaker_lock_cannot_displace_an_emergency_stop() -> None:
    state = AccountRiskState("paper_account", "a")
    state.lock(RiskState.emergency_stop, "drill", T0)
    state.lock(RiskState.daily_loss_locked, "down 600", T0)
    assert state.state is RiskState.emergency_stop


def test_the_states_needing_authorised_reset_are_the_dangerous_ones() -> None:
    assert NEEDS_AUTHORISED_RESET == {
        RiskState.weekly_loss_locked,
        RiskState.drawdown_locked,
        RiskState.emergency_stop,
        RiskState.disabled,
    }
    # The daily lock is the exception, and the reason is that refresh() can
    # PROVE its day has ended. It cannot prove a week has -- that needs the
    # day the broker's week starts on, which varies by venue. So a weekly
    # lock is released by a person who has looked, which is the right
    # friction for a limit that took a week to breach.
    assert RiskState.daily_loss_locked not in NEEDS_AUTHORISED_RESET


def test_a_lock_with_no_day_recorded_never_clears_itself() -> None:
    state = AccountRiskState("paper_account", "a")
    state.lock(RiskState.daily_loss_locked, "down", T0, day=None)
    state.refresh(T0 + timedelta(days=365))
    assert state.blocking


def test_the_latching_limits_are_the_session_stopping_ones() -> None:
    assert LATCHING == {
        LimitKind.max_daily_loss: RiskState.daily_loss_locked,
        LimitKind.max_weekly_loss: RiskState.weekly_loss_locked,
        LimitKind.max_drawdown: RiskState.drawdown_locked,
    }
    # max_consecutive_losses is deliberately absent: a streak clears itself on
    # the next win, and latching it would turn a pause that ends by itself into
    # a lock that needs a person.
    assert LimitKind.max_consecutive_losses not in LATCHING


# ================================================ THE THREE ADDED AT P4


def _state(**kw: object) -> PortfolioState:
    base: dict[str, object] = {
        "equity": Decimal("10000"),
        "balance": Decimal("10000"),
        "open_positions": 0,
        "realised_today": Decimal("0"),
        "trades_today": 0,
    }
    base.update(kw)
    return PortfolioState(**base)  # type: ignore[arg-type]


def _fails(verdict, kind) -> bool:
    return any(c.limit is kind and not c.passed for c in verdict.checks)


def test_a_breached_week_halts_rather_than_vetoes() -> None:
    """A week that has breached does not un-breach before the next order."""
    engine = RiskEngine(RiskLimits(require_stop_loss=False, max_weekly_loss=Decimal("500")))
    verdict = engine.evaluate(proposal(), _state(realised_week=Decimal("-501")), now=T0)
    assert verdict.decision is RiskDecision.halt
    assert _fails(verdict, LimitKind.max_weekly_loss)


def test_an_unknown_week_fails_rather_than_passes() -> None:
    """The rule the daily limit already follows: missing is never safe."""
    engine = RiskEngine(RiskLimits(require_stop_loss=False, max_weekly_loss=Decimal("500")))
    verdict = engine.evaluate(proposal(), _state(realised_week=None), now=T0)
    assert _fails(verdict, LimitKind.max_weekly_loss)


def test_a_losing_streak_vetoes_but_does_not_halt() -> None:
    """The distinction that matters.

    A halt stops the session managing what is already open. A streak is a
    reason to stop OPENING, never a reason to stop watching -- and it clears
    itself on the next win, so latching it would turn a pause that ends by
    itself into a lock that needs a person.
    """
    engine = RiskEngine(RiskLimits(require_stop_loss=False, max_consecutive_losses=3))
    verdict = engine.evaluate(proposal(), _state(consecutive_losses=3), now=T0)
    assert verdict.decision is RiskDecision.veto
    assert verdict.decision is not RiskDecision.halt
    assert _fails(verdict, LimitKind.max_consecutive_losses)


def test_a_streak_under_the_cap_passes() -> None:
    engine = RiskEngine(RiskLimits(require_stop_loss=False, max_consecutive_losses=3))
    verdict = engine.evaluate(proposal(), _state(consecutive_losses=2), now=T0)
    assert not _fails(verdict, LimitKind.max_consecutive_losses)


def test_an_uncounted_streak_fails_rather_than_passes() -> None:
    """None is not zero. It means the outcomes could not be counted."""
    engine = RiskEngine(RiskLimits(require_stop_loss=False, max_consecutive_losses=3))
    verdict = engine.evaluate(proposal(), _state(consecutive_losses=None), now=T0)
    assert _fails(verdict, LimitKind.max_consecutive_losses)


def test_a_correlation_limit_with_no_correlation_data_refuses() -> None:
    """The one of the three with no data source, and it fails closed.

    A correlation needs a common window across two or more instruments and
    this repository has 705 bars across 2 symbols. An unmeasurable limit that
    APPROVED would be worse than no limit, because it reads as one that was
    checked.
    """
    engine = RiskEngine(
        RiskLimits(require_stop_loss=False, max_correlated_exposure=Decimal("5000"))
    )
    verdict = engine.evaluate(proposal(), _state(correlated_exposure=None), now=T0)
    assert _fails(verdict, LimitKind.max_correlated_exposure)
    detail = next(c.detail for c in verdict.checks if c.limit is LimitKind.max_correlated_exposure)
    assert "two or more instruments" in detail


def test_correlated_exposure_within_the_limit_passes() -> None:
    engine = RiskEngine(
        RiskLimits(require_stop_loss=False, max_correlated_exposure=Decimal("5000"))
    )
    verdict = engine.evaluate(proposal(), _state(correlated_exposure=Decimal("4999")), now=T0)
    assert not _fails(verdict, LimitKind.max_correlated_exposure)


def test_none_of_the_three_fires_when_it_is_not_configured() -> None:
    """All three are opt-in and must not refuse anything when unset.

    Note what this does NOT assert. The engine records a check for every
    limit kind whether or not it is configured -- `max_daily_loss` is in
    the list too, unconfigured -- so 'the check is absent' would be the
    wrong property and asserting it would only prove the assertion was
    written without running it. What matters is that an unset limit
    PASSES rather than refuses.
    """
    engine = RiskEngine(RiskLimits(require_stop_loss=False))
    verdict = engine.evaluate(proposal(), _state(), now=T0)
    for kind in (
        LimitKind.max_weekly_loss,
        LimitKind.max_consecutive_losses,
        LimitKind.max_correlated_exposure,
    ):
        assert not _fails(verdict, kind), kind
    assert verdict.decision is RiskDecision.approve


# ========================================================== THE SERVICE


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=eng)
    yield application
    await eng.dispose()


@pytest.fixture
async def db(app: FastAPI) -> AsyncIterator:
    async with app.state.session_factory() as session:
        yield session


@pytest.fixture
def service(app: FastAPI) -> RiskService:
    svc: RiskService = app.state.risk
    return svc


async def test_a_decision_is_recorded_with_its_codes_and_version(service: RiskService, db) -> None:
    from app.models.risk import RiskEvent
    from sqlalchemy import select

    decision = await service.evaluate(
        db,
        RiskRequest(
            proposal=proposal(volume=Decimal("5")),
            portfolio=PortfolioState(open_positions=9),
        ),
    )
    await db.commit()
    assert decision.decision_id
    rows = (await db.scalars(select(RiskEvent))).all()
    assert len(rows) == 1
    assert rows[0].decision_id == decision.decision_id
    assert rows[0].paper_account_id == "acct-a"
    assert rows[0].mode == "paper"
    assert rows[0].snapshot["outcome"] == str(decision.outcome)


async def test_an_approval_is_recorded_too(service: RiskService, db) -> None:
    """A veto that leaves no trace is indistinguishable from a check that never
    ran -- and so is an approval."""
    from app.models.risk import RiskEvent
    from sqlalchemy import select

    decision = await service.evaluate(db, RiskRequest(proposal=proposal()))
    await db.commit()
    assert decision.approved
    rows = (await db.scalars(select(RiskEvent))).all()
    assert [r.decision for r in rows] == ["approve"]


async def test_a_rejection_carries_an_explicit_code(service: RiskService, db) -> None:
    await service.set_limits(
        db,
        scope=Scope.paper_account,
        scope_ref="acct-a",
        limits={"max_open_positions": 1},
        name="tight",
        actor="test",
    )
    await db.commit()
    decision = await service.evaluate(
        db, RiskRequest(proposal=proposal(), portfolio=PortfolioState(open_positions=4))
    )
    assert not decision.approved
    assert RejectionCode.max_open_positions_exceeded in decision.codes


# ================================================ THE DAILY LOSS LOCK


async def test_a_breached_daily_loss_latches_a_lock(service: RiskService, db) -> None:
    await service.set_limits(
        db,
        scope=Scope.paper_account,
        scope_ref="acct-a",
        limits={"max_daily_loss": Decimal("100")},
        name="daily",
        actor="test",
    )
    await db.commit()

    breach = await service.evaluate(
        db,
        RiskRequest(proposal=proposal(), portfolio=PortfolioState(realised_today=Decimal("-500"))),
    )
    await db.commit()
    assert breach.outcome is RiskOutcome.halted
    assert RejectionCode.max_daily_loss_exceeded in breach.codes
    assert service.state_for("acct-a").state is RiskState.daily_loss_locked

    # And the NEXT order is refused by the LOCK, not by re-running the check --
    # so a recovering portfolio cannot reopen trading on a breached limit.
    recovered = await service.evaluate(
        db,
        RiskRequest(proposal=proposal(), portfolio=PortfolioState(realised_today=Decimal("1000"))),
    )
    assert not recovered.approved
    assert RejectionCode.daily_loss_locked in recovered.codes


async def test_a_lock_survives_a_restart(app: FastAPI, service: RiskService, db) -> None:
    """A brand-new service reads the lock back before evaluating anything."""
    await service.lock(
        db,
        account_id="acct-a",
        state=RiskState.drawdown_locked,
        reason="down 22%",
        at=datetime.now(UTC),
    )
    await db.commit()

    fresh = RiskService(app.state.session_factory)
    assert fresh.loaded is False
    decision = await fresh.evaluate(db, RiskRequest(proposal=proposal()))
    assert fresh.loaded is True
    assert not decision.approved
    assert RejectionCode.drawdown_locked in decision.codes


async def test_a_kill_switch_survives_a_restart(app: FastAPI, service: RiskService, db) -> None:
    await service.engage_kill_switch(
        db, scope=Scope.global_, scope_ref=None, reason="maintenance", actor="admin"
    )
    await db.commit()

    fresh = RiskService(app.state.session_factory)
    await fresh.load(db)
    assert fresh.switches.global_stop is True
    decision = await fresh.evaluate(db, RiskRequest(proposal=proposal()))
    assert not decision.approved
    assert RejectionCode.kill_switch_active in decision.codes


async def test_releasing_a_kill_switch_lets_trading_resume(service: RiskService, db) -> None:
    await service.engage_kill_switch(
        db, scope=Scope.paper_account, scope_ref="acct-a", reason="drill", actor="admin"
    )
    await db.commit()
    blocked = await service.evaluate(db, RiskRequest(proposal=proposal()))
    assert not blocked.approved

    await service.release_kill_switch(
        db, scope=Scope.paper_account, scope_ref="acct-a", actor="admin"
    )
    await db.commit()
    allowed = await service.evaluate(db, RiskRequest(proposal=proposal()))
    assert allowed.approved, allowed.reason


# ==================================================== THE RACE CONDITION


async def test_two_concurrent_orders_cannot_both_take_the_same_headroom(
    service: RiskService, db
) -> None:
    """The brief's example: 40% exposure, a 50% limit, two 8% orders.

    Without a reservation both are evaluated against the same snapshot, both
    see 8% of headroom and both pass, landing at 56%. The approval reserves its
    exposure, so the second is evaluated against a book that already contains
    the first.
    """
    await service.set_limits(
        db,
        scope=Scope.paper_account,
        scope_ref="acct-a",
        limits={"max_exposure_per_currency": Decimal("50")},
        name="exposure",
        actor="test",
    )
    await db.commit()

    def request() -> RiskRequest:
        return RiskRequest(
            proposal=proposal(volume=Decimal("8"), entry_price=Decimal("1")),
            portfolio=PortfolioState(exposure_by_currency={"USD": Decimal("40")}),
        )

    first, second = await asyncio.gather(
        service.evaluate(db, request()), service.evaluate(db, request())
    )
    await db.commit()
    approved = [d for d in (first, second) if d.approved]
    assert len(approved) == 1, (
        f"both orders were approved; 40 + 8 + 8 = 56 exceeds the 50 limit. "
        f"{[str(d.outcome) for d in (first, second)]}"
    )
    refused = [d for d in (first, second) if not d.approved][0]
    assert RejectionCode.max_exposure_exceeded in refused.codes


async def test_a_reservation_is_released_when_the_order_resolves(service: RiskService, db) -> None:
    await service.set_limits(
        db,
        scope=Scope.paper_account,
        scope_ref="acct-a",
        limits={"max_exposure_per_currency": Decimal("50")},
        name="exposure",
        actor="test",
    )
    await db.commit()
    request = RiskRequest(
        proposal=proposal(volume=Decimal("8"), entry_price=Decimal("1")),
        portfolio=PortfolioState(exposure_by_currency={"USD": Decimal("40")}),
    )
    first = await service.evaluate(db, request)
    assert first.approved
    blocked = await service.evaluate(db, request)
    assert not blocked.approved

    assert service.release(first.decision_id, "acct-a")
    freed = await service.evaluate(db, request)
    assert freed.approved, freed.reason


async def test_a_preview_reserves_nothing_and_writes_nothing(service: RiskService, db) -> None:
    """`/risk/check` must not consume headroom a real order needs."""
    from app.models.risk import RiskEvent
    from sqlalchemy import select

    request = RiskRequest(
        proposal=proposal(volume=Decimal("8"), entry_price=Decimal("1")),
        portfolio=PortfolioState(exposure_by_currency={"USD": Decimal("40")}),
    )
    for _ in range(5):
        preview = await service.check(db, request)
        assert preview.approved
    await db.commit()
    assert (await db.scalars(select(RiskEvent))).all() == []
    assert not service._reservations.get("acct-a")


# ========================================================== FAIL CLOSED


async def test_the_risk_engine_fails_closed(service: RiskService, db) -> None:
    """Every failure mode is a refusal. There is no fail-open path."""

    class Exploding:
        def __getattr__(self, name: str):  # noqa: ANN204
            raise RuntimeError("the risk engine is broken")

    # 1. A check that raises.
    original = service.configuration
    service.configuration = _raiser  # type: ignore[assignment]
    decision = await service.evaluate(db, RiskRequest(proposal=proposal()))
    assert not decision.approved
    assert decision.outcome is RiskOutcome.error
    assert RejectionCode.risk_engine_error in decision.codes
    service.configuration = original  # type: ignore[method-assign]

    # 2. A configuration that will not load.
    await service.set_limits(
        db,
        scope=Scope.global_,
        scope_ref=None,
        limits={"max_drawdown_pct": Decimal("10")},
        name="ok",
        actor="test",
    )
    await db.commit()
    from app.models.risk import RiskRule
    from sqlalchemy import select

    row = await db.scalar(select(RiskRule).where(RiskRule.rule_type == "limits"))
    assert row is not None
    row.params = {"max_drawdown_pct": -5}  # invalid, past validation
    await db.flush()
    broken = await service.evaluate(db, RiskRequest(proposal=proposal()))
    assert not broken.approved
    assert broken.outcome is RiskOutcome.error


async def _raiser(*args: object, **kwargs: object) -> None:
    raise RuntimeError("configuration unavailable")


async def test_a_persistence_failure_is_not_an_approval(service: RiskService, db) -> None:
    """An approval nobody can audit is not an approval this platform makes."""

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database is gone")

    service._record = boom  # type: ignore[method-assign]
    decision = await service.evaluate(db, RiskRequest(proposal=proposal()))
    assert not decision.approved
    assert decision.outcome is RiskOutcome.error


# =============================================== RISK RUNS IN EVERY MODE


@pytest.mark.parametrize("mode", ["backtest", "replay", "paper", "live"])
async def test_risk_runs_in_every_execution_mode(service: RiskService, db, mode: str) -> None:
    """The difference between modes is the execution destination, not whether
    safety applies."""
    decision = await service.evaluate(
        db,
        RiskRequest(
            proposal=proposal(mode="paper"),
            execution_mode=mode,
            portfolio=PortfolioState(open_positions=0),
        ),
    )
    assert decision.checks, f"no checks ran in {mode}"
    assert decision.execution_mode == mode


def test_a_live_proposal_is_refused_while_live_trading_is_off() -> None:
    """The engine is the last of three independent gates, and it still refuses."""
    approval, verdict = RiskEngine(RiskLimits(require_stop_loss=False)).approve(
        proposal(mode="live")
    )
    assert approval is None
    assert LimitKind.trading_mode in {c.limit for c in verdict.failed}


# ==================================== THE PAPER PIPELINE USES THIS ENGINE


async def test_the_paper_pipeline_writes_a_coded_versioned_decision(
    app: FastAPI, service: RiskService
) -> None:
    """L16's pipeline must go through L17's authority, not around it.

    Asserts the wiring rather than the components: a `PaperService` holding a
    `RiskService` runs one pass and the decision lands in `risk_events` with
    its account, its mode and the configuration version it was made under.
    """
    from datetime import timedelta

    from app.backtest.config import CostModel
    from app.marketdata.types import Timeframe
    from app.models.risk import RiskEvent
    from app.paper.engine import PaperEngine
    from app.paper.portfolio import AccountState, PaperPortfolio
    from app.paper.service import PaperService, RunningBot
    from sqlalchemy import select

    from tests.test_paper import AlwaysLong, bars, spec, wave

    async with app.state.session_factory() as db:
        await service.set_limits(
            db,
            scope=Scope.paper_account,
            scope_ref="acct-a",
            limits={"max_open_positions": 3},
            name="account",
            actor="test",
        )
        await db.commit()

    portfolio = PaperPortfolio(
        account_id="acct-a", currency="USD", starting_balance=Decimal("100000")
    )
    portfolio.state = AccountState.active
    engine = PaperEngine(
        portfolio=portfolio,
        strategy=AlwaysLong(),
        spec=spec(),
        risk=RiskEngine(RiskLimits(require_stop_loss=False)),
        costs=CostModel(spread_points=Decimal("0.0002")),
        timeframe=Timeframe.H1,
        quantity=Decimal("1"),
    )
    data = bars(wave(120))

    paper = PaperService(app.state.session_factory, None, None, None, None, service)
    bot = RunningBot(
        id="bot-1",
        user_id="u1",
        account_id="acct-a",
        engine=engine,
        symbol="EURUSD",
        timeframe=Timeframe.H1,
        provider="simulator",  # type: ignore[arg-type]
        run_id="run-1",
        status="running",
    )

    async with app.state.session_factory() as db:
        resolved = await paper.risk.limits_for(  # type: ignore[union-attr]
            db,
            account_id=bot.account_id,
            strategy_id=engine.strategy_id,
            symbol=bot.symbol,
            caller=engine.risk.limits,
        )
        engine.risk = RiskEngine(resolved.limits, paper.risk.switches)  # type: ignore[union-attr]
        engine.configuration_version = resolved.version
        assert resolved.limits.max_open_positions == 3

        for i in range(60, 120):
            engine.pending_verdicts.clear()
            engine.process(data[: i + 1], now=data[i].bar_time + timedelta(minutes=1))
            for verdict in engine.pending_verdicts:
                await paper.risk.record_verdict(  # type: ignore[union-attr]
                    db,
                    verdict,
                    account_id=bot.account_id,
                    execution_mode="paper",
                    configuration_version=engine.configuration_version,
                )
            if engine.pending_verdicts:
                break
        await db.commit()

    async with app.state.session_factory() as db:
        rows = (
            await db.scalars(select(RiskEvent).where(RiskEvent.paper_account_id == "acct-a"))
        ).all()
    assert rows, "the paper pipeline wrote no risk decision"
    row = rows[0]
    assert row.mode == "paper"
    assert row.decision_id
    assert row.request_hash
    assert row.configuration_version == resolved.version
    assert row.snapshot["checks"]


async def test_a_latched_lock_stops_a_paper_bot_before_it_works(
    app: FastAPI, service: RiskService
) -> None:
    """`precheck` is the runner's gate: a locked account costs one state read
    rather than a full evaluation it was always going to refuse."""
    async with app.state.session_factory() as db:
        await service.lock(
            db,
            account_id="acct-a",
            state=RiskState.drawdown_locked,
            reason="down 30%",
            at=datetime.now(UTC),
        )
        await db.commit()

        locked = await service.precheck(db, account_id="acct-a")
        assert locked is not None
        assert locked.state is RiskState.drawdown_locked
        assert locked.code is RejectionCode.drawdown_locked

        clear = await service.precheck(db, account_id="acct-other")
        assert clear is None


async def test_a_bot_cannot_loosen_its_accounts_limits(app: FastAPI, service: RiskService) -> None:
    """The bot's frozen configuration goes in as a LAYER, not as an override."""
    async with app.state.session_factory() as db:
        await service.set_limits(
            db,
            scope=Scope.paper_account,
            scope_ref="acct-a",
            limits={"max_open_positions": 1},
            name="tight account",
            actor="test",
        )
        await db.commit()
        resolved = await service.limits_for(
            db,
            account_id="acct-a",
            strategy_id="greedy",
            symbol="EURUSD",
            caller=RiskLimits(max_open_positions=50),
        )
    assert resolved.limits.max_open_positions == 1


# ==================================================================== API


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER

    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _promote(app: FastAPI, email: str, role: str) -> None:
    from app.auth.models import User
    from sqlalchemy import select

    async with app.state.session_factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role
        await session.commit()


@pytest.fixture
async def trader(client: AsyncClient, app: FastAPI) -> AsyncClient:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], "trader")
    return client


async def test_the_preview_creates_no_order(trader: AsyncClient) -> None:
    r = await trader.post(
        "/v1/risk/check",
        json={
            "symbol": "EURUSD",
            "side": "buy",
            "quantity": "1",
            "entry_price": "1.1",
            "stop_loss": "1.09",
            "account_id": "acct-a",
        },
        headers=_csrf(trader),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preview"] is True
    assert "No order was created" in body["note"]
    assert body["checks"]
    assert body["decision_id"]


async def test_the_preview_explains_a_rejection(trader: AsyncClient) -> None:
    r = await trader.post(
        "/v1/risk/check",
        json={
            "symbol": "EURUSD",
            "side": "buy",
            "quantity": "1",
            "entry_price": "1.1",
            "account_id": "acct-a",
        },
        headers=_csrf(trader),
    )
    body = r.json()
    assert body["approved"] is False
    assert "STOP_LOSS_REQUIRED" in body["codes"]
    assert body["reason"]


async def test_the_status_route_reports_the_failure_mode(trader: AsyncClient) -> None:
    body = (await trader.get("/v1/risk/status")).json()
    assert "closed" in body["failure_mode"]
    assert "not a distributed lock" in body["concurrency"]
    assert body["kill_switches"]["global"] is False


async def test_the_limits_route_cannot_drift_from_the_engine(trader: AsyncClient) -> None:
    body = (await trader.get("/v1/risk/limits")).json()
    assert set(body["checks"]) == {str(k) for k in LimitKind}
    assert "MAX_DAILY_LOSS_EXCEEDED" in body["rejection_codes"]
    assert "most restrictive" in body["precedence"]["rule"]


async def test_an_unsafe_configuration_is_refused_over_the_api(trader: AsyncClient) -> None:
    r = await trader.put(
        "/v1/risk/config",
        json={
            "scope": "global",
            "name": "bad",
            "limits": {"max_drawdown_pct": "150"},
        },
        headers=_csrf(trader),
    )
    assert r.status_code == 422
    assert "cannot exceed 100" in r.text


async def test_storing_a_layer_bumps_its_version(trader: AsyncClient) -> None:
    first = await trader.put(
        "/v1/risk/config",
        json={"scope": "global", "name": "v1", "limits": {"max_open_positions": 5}},
        headers=_csrf(trader),
    )
    assert first.status_code == 200, first.text
    second = await trader.put(
        "/v1/risk/config",
        json={"scope": "global", "name": "v2", "limits": {"max_open_positions": 3}},
        headers=_csrf(trader),
    )
    assert second.json()["version"] > first.json()["version"]
    effective = (await trader.get("/v1/risk/config")).json()["effective"]
    assert effective["limits"]["max_open_positions"] == "3"


async def test_a_kill_switch_over_the_api_blocks_and_reports(trader: AsyncClient) -> None:
    r = await trader.post(
        "/v1/risk/kill-switch",
        json={"scope": "global", "reason": "maintenance"},
        headers=_csrf(trader),
    )
    assert r.status_code == 200, r.text
    assert "never closes a position" in r.json()["positions"]
    assert r.json()["status"]["kill_switches"]["global"] is True

    preview = await trader.post(
        "/v1/risk/check",
        json={
            "symbol": "EURUSD",
            "side": "buy",
            "quantity": "1",
            "entry_price": "1.1",
            "stop_loss": "1.09",
            "account_id": "acct-a",
        },
        headers=_csrf(trader),
    )
    assert preview.json()["approved"] is False
    assert "KILL_SWITCH_ACTIVE" in preview.json()["codes"]


async def test_clearing_a_lock_must_be_confirmed(trader: AsyncClient) -> None:
    r = await trader.post(
        "/v1/risk/locks/clear",
        json={"confirm": False, "reason": "x"},
        headers=_csrf(trader),
    )
    assert r.status_code == 422


async def test_a_plain_user_cannot_change_risk_configuration(
    client: AsyncClient, app: FastAPI
) -> None:
    await client.post("/auth/register", json=BOB)
    for verb, path, body in (
        ("PUT", "/v1/risk/config", {"scope": "global", "name": "x", "limits": {}}),
        ("POST", "/v1/risk/kill-switch", {"scope": "global", "reason": "x"}),
        ("GET", "/v1/risk/decisions", None),
    ):
        r = await client.request(verb, path, json=body, headers=_csrf(client))
        assert r.status_code == 403, f"{verb} {path} -> {r.status_code}"


async def test_the_risk_pending_stubs_are_gone(trader: AsyncClient) -> None:
    """L06 promised /risk/rules and /risk/events at level 17."""
    for path in ("/v1/risk/rules", "/v1/risk/events"):
        r = await trader.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_only_the_risk_engine_can_mint_an_approval() -> None:
    """The unforgeable-token property, restated after L45 C-2.

    `Approval` is what the OMS requires and what nothing else may build. Until
    L45 there was exactly one constructor call, in `RiskEngine.approve`, and
    "one method" was the invariant people carried in their heads. C-2's fix
    added a second — `approve_close`, which mints an approval for a
    risk-reducing order that opening limits must not be able to veto.

    So the property is no longer "one method". It is **one class**, and this
    test is what keeps it that way: a future close path, bot or route that
    constructed its own `Approval` would bypass the Risk Engine entirely while
    satisfying every signature the OMS checks.
    """
    import ast
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Approval"
            ):
                if path.name != "engine.py" or path.parent.name != "risk":
                    offenders.append(f"{path.relative_to(app_dir)}:{node.lineno}")

    assert offenders == [], (
        "an Approval is constructed outside app/risk/engine.py: "
        + ", ".join(offenders)
        + ". The OMS accepts any Approval, so anything that can build one has "
        "bypassed the Risk Engine."
    )


def test_every_risk_limit_is_reachable_from_the_api() -> None:
    """A limit the API cannot set is a limit that does not exist.

    `RiskLimits` had 23 fields and `LimitsBody` exposed 20. The three missing
    ones -- max_weekly_loss, max_consecutive_losses, max_correlated_exposure
    -- were built at P4 with their vetoes, their latching and their
    fail-closed behaviour, and then had no way in. A limit no layer states is
    reported `not_enforced` on every decision, so the weekly lock and the
    correlation check were dead on the platform path: present in the code,
    absent from every verdict.

    Asserted on the types rather than by listing names, so the next field
    added to `RiskLimits` fails here instead of being quietly unreachable for
    another release. That is the whole point -- the gap was silent for as
    long as it existed and nothing would have reported it.
    """
    from app.api.v1.risk import LimitsBody

    declared = {f.name for f in fields(RiskLimits)}
    exposed = set(LimitsBody.model_fields)

    missing = declared - exposed
    assert not missing, (
        "RiskLimits fields with no API field, so nothing can ever set them: "
        + ", ".join(sorted(missing))
        + ". A limit that cannot be stated is reported not_enforced forever."
    )

    # And nothing in the body that the engine would ignore, which would be the
    # opposite failure: an API that accepts a limit and silently drops it.
    phantom = exposed - declared
    assert not phantom, (
        "LimitsBody accepts fields RiskLimits does not have, so they are "
        "silently discarded: " + ", ".join(sorted(phantom))
    )


def test_the_three_p4_limits_actually_reach_the_engine() -> None:
    """Reachable is not the same as wired. Round-trip them through the body.

    `to_limits()` builds the dataclass the engine reads, so a field present
    on the body but dropped in translation would still be dead. This asserts
    the value arrives, which is the property the API field exists for.
    """
    from app.api.v1.risk import LimitsBody

    body = LimitsBody(
        max_weekly_loss=Decimal("500"),
        max_consecutive_losses=3,
        max_correlated_exposure=Decimal("2.5"),
    )
    limits = body.to_limits()

    assert limits.max_weekly_loss == Decimal("500")
    assert limits.max_consecutive_losses == 3
    assert limits.max_correlated_exposure == Decimal("2.5")

    # Unstated stays None, which is what `not_enforced` is derived from.
    assert LimitsBody().to_limits().max_weekly_loss is None
