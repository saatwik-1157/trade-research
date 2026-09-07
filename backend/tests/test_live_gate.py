"""The live activation layer: allowlist, gate, state machine.

Every test here is a refusal test. The layer's only power is to say no, so the
things worth asserting are that it says no in each case it must, that it can
still say yes when everything genuinely passes (a gate that always failed would
be indistinguishable from a broken one), and that no path through it reaches a
venue.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.live.allowlist import Allowlist
from app.live.gate import (
    CONFIRMATION_PHRASE,
    CapitalLimits,
    Check,
    ConnectedAccount,
    GateReport,
    LiveContext,
    LiveTradingGate,
    ModelLock,
    StrategyLock,
    SymbolState,
    Verdict,
    soften_optional_failures,
)
from app.live.state import (
    ActivationMachine,
    ActivationState,
    IllegalActivationTransition,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _settings(**over: object) -> SimpleNamespace:
    """A settings object shaped like the real one, with live fully configured."""
    base = {
        "environment": SimpleNamespace(value="production"),
        "trading_mode": SimpleNamespace(value="live"),
        "live_trading": True,
        "enable_live_trading_confirmation": CONFIRMATION_PHRASE,
        "live_execution_blockers": lambda: [],
        "step_up_required": True,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _mode(value: str) -> SimpleNamespace:
    return _settings(trading_mode=SimpleNamespace(value=value))


def _env(value: str) -> SimpleNamespace:
    return _settings(environment=SimpleNamespace(value=value))


def _confirm(value: str) -> SimpleNamespace:
    return _settings(enable_live_trading_confirmation=value)


def _blocked() -> SimpleNamespace:
    return _settings(live_execution_blockers=lambda: ["gate not built: x"])


def _account(**over: object) -> ConnectedAccount:
    base = {
        "account_id": "900123",
        "server": "RealBroker-Live",
        "broker": "RealBroker Ltd",
        "account_type": "REAL",
        "currency": "USD",
        "balance": Decimal("10000"),
        "equity": Decimal("10000"),
        "free_margin": Decimal("9500"),
        "leverage": 30,
        "trade_allowed": True,
    }
    base.update(over)
    return ConnectedAccount(**base)  # type: ignore[arg-type]


def _passing_context(**over: object) -> LiveContext:
    """A context in which every mandatory check passes.

    It is deliberately verbose. Each value here is a thing somebody has to be
    able to point at before real money moves, and a fixture that defaulted them
    would be hiding exactly what the gate exists to demand.
    """
    base = dict(
        settings=_settings(),
        now=NOW,
        allowlist=Allowlist.of(["900123"]),
        expected_account_id="900123",
        expected_server="RealBroker-Live",
        connected_account=_account(),
        adapter_registered=True,
        adapter_mode="live",
        adapter_healthy=True,
        terminal_reachable=True,
        symbols=(
            SymbolState(
                internal="EURUSD",
                venue_symbol="EURUSD",
                bid=Decimal("1.1000"),
                ask=Decimal("1.1001"),
                quoted_at=NOW - timedelta(seconds=2),
                market_open=True,
                max_spread=Decimal("0.0005"),
            ),
        ),
        market_data_max_age_seconds=60.0,
        strategies=(
            StrategyLock(
                strategy_id="s1",
                version_id="v1",
                status="approved",
                fingerprint="abc123",
                symbols=("EURUSD",),
                has_stop=True,
                has_target=True,
            ),
        ),
        ai_enabled=False,
        models=(),
        risk_engine_healthy=True,
        risk_limits_configured=True,
        capital=CapitalLimits(
            approved_capital=Decimal("10000"),
            risk_budget=Decimal("100"),
            max_daily_loss=Decimal("50"),
            max_drawdown=Decimal("500"),
            max_exposure=Decimal("2000"),
            max_position_size=Decimal("0.10"),
            max_open_positions=2,
            max_daily_orders=10,
        ),
        kill_switch_engaged=None,
        kill_switches_available=True,
        position_sizer_healthy=True,
        oms_healthy=True,
        position_manager_healthy=True,
        unresolved_orders=0,
        unknown_orders=0,
        positions_reconciled=True,
        orders_reconciled=True,
        unexpected_positions=0,
        safe_mode_engaged=False,
        recovery_available=True,
        monitoring_active=True,
        notifications_healthy=True,
        database_healthy=True,
        redis_healthy=True,
        workers_healthy=True,
        audit_logging_active=True,
        step_up_required=True,
        release_stamped=True,
    )
    base.update(over)
    return LiveContext(**base)  # type: ignore[arg-type]


def _verdict_of(report: GateReport, name: str) -> Verdict:
    for check in report.checks:
        if check.name == name:
            return check.verdict
    raise AssertionError(f"no check named {name!r} in the report")


# ------------------------------------------------------------------ allowlist


def test_an_empty_allowlist_permits_nothing():
    empty = Allowlist()
    assert empty.configured is False
    assert empty.permits("900123") is False
    assert empty.permits("") is False
    assert empty.permits(None) is False


def test_an_allowlist_permits_only_what_it_lists():
    lst = Allowlist.of(["900123", " 900124 "])
    assert lst.permits("900123") is True
    assert lst.permits(900124) is True  # an int login is compared as text
    assert lst.permits("900125") is False


def test_a_blank_entry_is_dropped_rather_than_stored():
    # An allowlist holding "" would permit an account whose identifier could
    # not be read, which is the case that must refuse hardest.
    lst = Allowlist.of(["", "  ", "900123"])
    assert lst.accounts == frozenset({"900123"})
    assert lst.permits(None) is False


# ----------------------------------------------------------------- the gate


def test_a_fully_configured_context_is_ready():
    # If this ever fails the gate has become unpassable, which is a different
    # bug from being too permissive and needs to be visible.
    report = LiveTradingGate().evaluate(_passing_context())
    assert report.blockers == (), [c.as_dict() for c in report.blockers]
    assert report.ready is True
    assert report.verdict == "READY_FOR_LIVE"


def test_an_empty_report_is_not_ready():
    # Zero checks is a gate that did not run, not a gate that passed.
    assert GateReport(checks=(), generated_at=NOW).ready is False


def test_todays_real_settings_are_not_ready():
    """The deployment as it actually stands must refuse."""
    from app.core.settings import Settings

    report = LiveTradingGate().evaluate(_passing_context(settings=Settings()))
    assert report.ready is False
    names = {c.name for c in report.blockers}
    assert "trading_mode_is_live" in names
    assert "live_trading_flag_set" in names
    assert "live_gates_all_built" in names


@pytest.mark.parametrize(
    ("override", "expected_failure"),
    [
        ({"settings": _mode("paper")}, "trading_mode_is_live"),
        ({"settings": _mode("demo")}, "trading_mode_is_live"),
        ({"settings": _settings(live_trading=False)}, "live_trading_flag_set"),
        ({"settings": _env("development")}, "environment_is_production"),
        ({"settings": _confirm("")}, "explicit_confirmation_phrase"),
        ({"settings": _confirm("yes")}, "explicit_confirmation_phrase"),
        ({"settings": _blocked()}, "live_gates_all_built"),
        ({"allowlist": Allowlist()}, "allowlist_configured"),
        ({"allowlist": Allowlist.of(["999999"])}, "account_allowlisted"),
        ({"expected_account_id": "900999"}, "account_matches_expected"),
        ({"expected_account_id": None}, "account_matches_expected"),
        ({"expected_server": "OtherBroker-Live"}, "server_matches_expected"),
        ({"connected_account": _account(account_type="DEMO")}, "account_type_is_live"),
        ({"connected_account": _account(account_type="CONTEST")}, "account_type_is_live"),
        ({"connected_account": _account(account_type="UNKNOWN(7)")}, "account_type_is_live"),
        ({"connected_account": _account(trade_allowed=False)}, "account_trading_permitted"),
        ({"connected_account": _account(equity=Decimal("0"))}, "account_has_equity"),
        ({"adapter_registered": False}, "broker_adapter_registered"),
        ({"adapter_mode": "demo"}, "broker_adapter_mode_is_live"),
        ({"adapter_mode": "paper"}, "broker_adapter_mode_is_live"),
        ({"adapter_healthy": False}, "broker_connection_healthy"),
        ({"terminal_reachable": False}, "terminal_reachable"),
        ({"symbols": ()}, "symbols_present"),
        ({"strategies": ()}, "strategy_authorised"),
        ({"risk_engine_healthy": False}, "risk_engine_healthy"),
        ({"risk_limits_configured": False}, "risk_limits_configured"),
        ({"capital": None}, "capital_limits_configured"),
        ({"capital": CapitalLimits(approved_capital=Decimal("1"))}, "capital_limits_configured"),
        ({"kill_switch_engaged": "global kill switch"}, "no_kill_switch_engaged"),
        ({"kill_switches_available": False}, "kill_switches_available"),
        ({"oms_healthy": False}, "oms_healthy"),
        ({"position_sizer_healthy": False}, "position_sizer_healthy"),
        ({"position_manager_healthy": False}, "position_manager_healthy"),
        ({"unresolved_orders": 1}, "no_unresolved_orders"),
        ({"unknown_orders": 1}, "no_unknown_order_states"),
        ({"positions_reconciled": False}, "positions_reconciled"),
        ({"orders_reconciled": False}, "orders_reconciled"),
        ({"unexpected_positions": 2}, "no_unexpected_positions"),
        ({"safe_mode_engaged": True}, "safe_mode_clear"),
        ({"recovery_available": False}, "recovery_available"),
        ({"monitoring_active": False}, "monitoring_active"),
        ({"database_healthy": False}, "database_healthy"),
        ({"redis_healthy": False}, "redis_healthy"),
        ({"workers_healthy": False}, "workers_healthy"),
        ({"audit_logging_active": False}, "audit_logging_active"),
        ({"step_up_required": False}, "step_up_required"),
    ],
)
def test_each_condition_blocks_on_its_own(override: dict, expected_failure: str):
    report = LiveTradingGate().evaluate(_passing_context(**override))
    assert report.ready is False, f"{expected_failure} did not block"
    assert _verdict_of(report, expected_failure) is Verdict.failed
    assert expected_failure in {c.name for c in report.blockers}


def test_a_demo_account_cannot_pass_as_live():
    """INVARIANT 12: demo mode cannot trade live."""
    report = LiveTradingGate().evaluate(
        _passing_context(
            connected_account=_account(account_type="DEMO", account_id="5055473926"),
            allowlist=Allowlist.of(["5055473926"]),
            expected_account_id="5055473926",
        )
    )
    assert report.ready is False
    assert _verdict_of(report, "account_type_is_live") is Verdict.failed


def test_an_unread_account_fails_every_identity_check_rather_than_omitting_them():
    # A check that vanishes from a report is a check nobody notices is missing.
    report = LiveTradingGate().evaluate(_passing_context(connected_account=None))
    names = {c.name for c in report.checks}
    for expected in (
        "account_allowlisted",
        "account_matches_expected",
        "server_matches_expected",
        "account_type_is_live",
        "account_trading_permitted",
    ):
        assert expected in names
        assert _verdict_of(report, expected) is Verdict.failed


def test_missing_evidence_fails_rather_than_skips():
    """Unknown is not permission -- the RiskEngine's rule, applied here."""
    report = LiveTradingGate().evaluate(_passing_context(oms_healthy=None))
    check = next(c for c in report.checks if c.name == "oms_healthy")
    assert check.verdict is Verdict.failed
    assert "nothing was supplied" in check.detail


def test_a_stale_quote_blocks_new_entries():
    """INVARIANT: stale critical market data blocks new entries."""
    stale = SymbolState(
        internal="EURUSD",
        venue_symbol="EURUSD",
        bid=Decimal("1.1000"),
        ask=Decimal("1.1001"),
        quoted_at=NOW - timedelta(seconds=600),
        market_open=True,
    )
    report = LiveTradingGate().evaluate(_passing_context(symbols=(stale,)))
    assert report.ready is False
    assert _verdict_of(report, "quotes_fresh") is Verdict.failed


def test_a_closed_market_and_a_crossed_quote_both_block():
    ctx = _passing_context()
    closed = replace(ctx.symbols[0], market_open=False)
    crossed = replace(ctx.symbols[0], bid=Decimal("1.2000"), ask=Decimal("1.1000"))
    assert (
        _verdict_of(LiveTradingGate().evaluate(_passing_context(symbols=(closed,))), "market_open")
        is Verdict.failed
    )
    assert (
        _verdict_of(
            LiveTradingGate().evaluate(_passing_context(symbols=(crossed,))),
            "quotes_not_crossed",
        )
        is Verdict.failed
    )


def test_a_strategy_without_a_stop_cannot_go_live():
    unprotected = StrategyLock(
        strategy_id="s1",
        version_id="v1",
        status="approved",
        fingerprint="abc",
        has_stop=False,
        has_target=True,
    )
    report = LiveTradingGate().evaluate(_passing_context(strategies=(unprotected,)))
    assert _verdict_of(report, "strategy_has_protection") is Verdict.failed


def test_an_unapproved_strategy_cannot_go_live():
    """INVARIANT 13."""
    draft = StrategyLock(
        strategy_id="s1",
        version_id="v1",
        status="draft",
        fingerprint="abc",
        has_stop=True,
        has_target=True,
    )
    report = LiveTradingGate().evaluate(_passing_context(strategies=(draft,)))
    assert _verdict_of(report, "strategy_authorised") is Verdict.failed


def test_an_unpinned_strategy_version_cannot_go_live():
    unpinned = StrategyLock(
        strategy_id="s1",
        version_id="v1",
        status="approved",
        fingerprint=None,
        has_stop=True,
        has_target=True,
    )
    report = LiveTradingGate().evaluate(_passing_context(strategies=(unpinned,)))
    assert _verdict_of(report, "strategy_version_locked") is Verdict.failed


def test_an_unapproved_model_cannot_go_live():
    """INVARIANT 14."""
    ctx = _passing_context(
        ai_enabled=True,
        ai_fallback_defined=True,
        models=(ModelLock(model_id="m1", version="1", status="draft", fingerprint="f"),),
    )
    report = LiveTradingGate().evaluate(ctx)
    assert _verdict_of(report, "ai_model_approved") is Verdict.failed


def test_ai_enabled_with_no_model_and_no_fallback_blocks():
    report = LiveTradingGate().evaluate(_passing_context(ai_enabled=True))
    assert report.ready is False
    assert _verdict_of(report, "ai_model_present") is Verdict.failed
    assert _verdict_of(report, "ai_fallback_defined") is Verdict.failed


def test_a_disabled_ai_seat_is_not_a_blocker():
    # No opinion is not approval, and it is not a refusal either.
    report = LiveTradingGate().evaluate(_passing_context(ai_enabled=False))
    assert _verdict_of(report, "ai_seat_configured") is Verdict.passed


def test_a_non_mandatory_failure_does_not_block():
    report = LiveTradingGate().evaluate(
        _passing_context(notifications_healthy=False, release_stamped=False)
    )
    assert report.ready is True
    assert _verdict_of(report, "notifications_healthy") is Verdict.failed
    assert "notifications_healthy" not in {c.name for c in report.blockers}


def test_a_mandatory_warning_still_blocks():
    # "Warning" on something that can make an order unsafe is a FAIL wearing a
    # softer word, and the softer word is how it gets waved through.
    warned = Check("x", Verdict.warning, "hmm", "risk", mandatory=True)
    report = GateReport(checks=(warned,), generated_at=NOW)
    assert report.ready is False
    assert report.blockers == (warned,)


def test_softening_cannot_rescue_a_mandatory_failure():
    report = LiveTradingGate().evaluate(_passing_context(oms_healthy=False))
    softened = soften_optional_failures(report)
    assert softened.ready is False
    assert _verdict_of(softened, "oms_healthy") is Verdict.failed


# -------------------------------------------------------- the state machine


def test_a_new_machine_starts_disabled():
    """§20: a restart must not resume live trading."""
    assert ActivationMachine().state is ActivationState.disabled
    assert ActivationMachine().live is False


@pytest.mark.parametrize(
    "target",
    [
        ActivationState.active,
        ActivationState.armed,
        ActivationState.activating,
        ActivationState.ready,
        ActivationState.paused,
    ],
)
def test_disabled_cannot_jump_anywhere_but_precheck(target: ActivationState):
    machine = ActivationMachine()
    with pytest.raises(IllegalActivationTransition):
        machine.to(target, actor="ops", reason="a reason long enough")
    assert machine.state is ActivationState.disabled


def test_ready_cannot_skip_arming():
    machine = ActivationMachine()
    machine.begin_precheck(actor="ops", reason="starting the preflight")
    machine.record_precheck(
        GateReport(checks=(Check("x", Verdict.passed, "ok", "g"),), generated_at=NOW),
        actor="ops",
        reason="preflight passed",
    )
    assert machine.state is ActivationState.ready
    with pytest.raises(IllegalActivationTransition):
        machine.to(ActivationState.active, actor="ops", reason="skipping ahead")


def test_a_failing_preflight_lands_in_precheck_failed():
    machine = ActivationMachine()
    machine.begin_precheck(actor="ops", reason="starting the preflight")
    machine.record_precheck(
        GateReport(checks=(), generated_at=NOW), actor="ops", reason="preflight ran"
    )
    assert machine.state is ActivationState.precheck_failed
    # And from there the only moves are back to precheck or away entirely.
    with pytest.raises(IllegalActivationTransition):
        machine.to(ActivationState.armed, actor="ops", reason="try anyway please")


def test_arming_refuses_a_failing_report():
    machine = ActivationMachine()
    machine.begin_precheck(actor="ops", reason="starting the preflight")
    machine.record_precheck(
        GateReport(checks=(Check("x", Verdict.passed, "ok", "g"),), generated_at=NOW),
        actor="ops",
        reason="preflight passed",
    )
    failing = GateReport(
        checks=(Check("oms_healthy", Verdict.failed, "down", "execution"),),
        generated_at=NOW,
    )
    with pytest.raises(IllegalActivationTransition) as exc:
        machine.arm(failing, actor="ops", reason="arming the session")
    assert "oms_healthy" in str(exc.value)
    assert machine.state is ActivationState.ready


def test_activation_requires_a_fresh_passing_report_and_armed_state():
    passing = GateReport(checks=(Check("x", Verdict.passed, "ok", "g"),), generated_at=NOW)
    machine = ActivationMachine()
    machine.begin_precheck(actor="ops", reason="starting the preflight")
    machine.record_precheck(passing, actor="ops", reason="preflight passed")

    with pytest.raises(IllegalActivationTransition):
        machine.activate(passing, actor="ops", reason="not armed yet")

    machine.arm(passing, actor="ops", reason="arming the session")
    failing = GateReport(checks=(), generated_at=NOW)
    with pytest.raises(IllegalActivationTransition):
        machine.activate(failing, actor="ops", reason="activating anyway")
    assert machine.state is ActivationState.armed

    moves = machine.activate(passing, actor="ops", reason="operator authorised")
    assert [m.to for m in moves] == [ActivationState.activating, ActivationState.active]
    assert machine.live is True


def test_every_transition_needs_an_actor_and_a_reason():
    machine = ActivationMachine()
    with pytest.raises(IllegalActivationTransition):
        machine.to(ActivationState.precheck, actor="", reason="a good long reason")
    with pytest.raises(IllegalActivationTransition):
        machine.to(ActivationState.precheck, actor="ops", reason="short")
    assert machine.state is ActivationState.disabled


def test_emergency_stop_is_reachable_from_every_live_state():
    for start in (
        ActivationState.armed,
        ActivationState.activating,
        ActivationState.active,
        ActivationState.paused,
    ):
        machine = ActivationMachine(state=start)
        machine.emergency_stop(actor="ops", reason="something went wrong")
        assert machine.state is ActivationState.emergency_stop


def test_emergency_stop_cannot_return_straight_to_trading():
    machine = ActivationMachine(state=ActivationState.active)
    machine.emergency_stop(actor="ops", reason="something went wrong")
    for target in (ActivationState.active, ActivationState.armed, ActivationState.ready):
        with pytest.raises(IllegalActivationTransition):
            machine.to(target, actor="ops", reason="resuming after the stop")


def test_the_history_records_who_and_why():
    machine = ActivationMachine()
    move = machine.begin_precheck(actor="ops@example.org", reason="nightly preflight")
    assert move.actor == "ops@example.org"
    assert machine.history[0].reason == "nightly preflight"
    assert machine.describe()["state"] == "PRECHECK"


# ------------------------------------------------------------- containment


def test_the_live_package_cannot_reach_a_venue():
    """No module here imports the execution machinery, at module level or not.

    The activation layer's job is to refuse. A module in it that could import
    an order manager could also call one, and this package would have become a
    second execution path -- the exact thing the brief forbids.
    """
    package = pathlib.Path(__file__).resolve().parents[1] / "app" / "live"
    forbidden = (
        "app.oms",
        "app.brokers",
        "app.risk",
        "app.execution",
        "app.paper",
        "app.positions",
        "app.bots",
    )
    offences: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == bad or name.startswith(bad + ".") for bad in forbidden):
                    offences.append(f"{path.name} imports {name}")
    assert offences == [], offences


def test_no_live_setting_can_enable_live_execution(monkeypatch):
    """The L70 settings give the gate something to compare against.

    They cannot widen what the platform may do, and this is the assertion that
    keeps that true: with every one of them set as favourably as possible,
    `live_execution_allowed` is still False and every LIVE_GATES entry is still
    a blocker. A setting can widen what the platform WANTS; only code that has
    been tested widens what it CAN.
    """
    from app.core.settings import Settings

    for name, value in (
        ("LIVE_ALLOWED_ACCOUNT_IDS", "900123,900124"),
        ("LIVE_EXPECTED_ACCOUNT_ID", "900123"),
        ("LIVE_EXPECTED_SERVER", "RealBroker-Live"),
        ("ENABLE_LIVE_TRADING_CONFIRMATION", CONFIRMATION_PHRASE),
        ("LIVE_QUOTE_MAX_AGE_SECONDS", "30"),
    ):
        monkeypatch.setenv(name, value)

    settings = Settings()
    assert settings.live_allowed_accounts == ("900123", "900124")
    assert settings.enable_live_trading_confirmation == CONFIRMATION_PHRASE
    assert settings.live_execution_allowed is False
    assert len(settings.live_execution_blockers()) == 12


def test_the_allowlist_setting_survives_untidy_spelling(monkeypatch):
    from app.core.settings import Settings

    monkeypatch.setenv("LIVE_ALLOWED_ACCOUNT_IDS", " 900123 , 900124 ,, ")
    assert Settings().live_allowed_accounts == ("900123", "900124")


def test_the_gate_reads_the_confirmation_phrase_exactly():
    # A near miss must not pass: this is the check standing between a typo in
    # a deploy script and a live session.
    for near in ("yes_i_understand", "YES I UNDERSTAND", " YES_I_UNDERSTAND", "YES"):
        report = LiveTradingGate().evaluate(
            _passing_context(settings=_settings(enable_live_trading_confirmation=near))
        )
        assert _verdict_of(report, "explicit_confirmation_phrase") is Verdict.failed
