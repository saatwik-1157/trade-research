from __future__ import annotations

import pytest
from app.core.settings import LIVE_GATES, Environment, Settings, TradingMode
from pydantic import ValidationError


def test_defaults_fail_closed(settings: Settings) -> None:
    assert settings.environment is Environment.development
    assert settings.trading_mode is TradingMode.paper
    assert settings.live_trading is False
    assert settings.live_execution_allowed is False


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("TRADING_MODE", "demo")
    s = Settings(_env_file=None)
    assert s.environment is Environment.test
    assert s.trading_mode is TradingMode.demo
    assert s.live_execution_allowed is False


def test_live_mode_without_flag_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ValidationError, match="LIVE_TRADING=true"):
        Settings(_env_file=None)


def test_live_mode_with_flag_is_still_blocked_by_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING", "true")
    s = Settings(_env_file=None)
    blockers = s.live_execution_blockers()
    assert s.live_execution_allowed is False
    assert "LIVE_TRADING is false" not in blockers
    assert any(b.startswith("gate not built") for b in blockers)


def test_no_live_gate_is_built_at_this_level() -> None:
    # A later level flips a gate deliberately, with the test that proves it.
    # If this fails, someone flipped one without doing that.
    assert all(v is False for v in LIVE_GATES.values())
    assert "live_broker_adapter" in LIVE_GATES


def test_unknown_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "real")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_public_summary_carries_no_urls(settings: Settings) -> None:
    summary = settings.public_summary()
    flat = " ".join(str(v) for v in summary.values())
    assert "postgresql" not in flat
    assert "redis://" not in flat
    assert summary["live_execution_allowed"] is False
