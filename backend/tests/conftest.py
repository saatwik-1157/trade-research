from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.settings import Settings  # noqa: E402

# Environment variables that would change what the tests are measuring. They
# are cleared so a developer's shell cannot make a "live" test pass or fail.
_MODE_VARS = ("ENVIRONMENT", "TRADING_MODE", "LIVE_TRADING", "DATABASE_URL", "REDIS_URL")


@pytest.fixture(autouse=True)
def _clean_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _MODE_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    # _env_file=None: the test never reads a developer's .env
    return Settings(_env_file=None)
