from __future__ import annotations

import asyncio

from app.core.health import CheckResult, HealthStatus, check_database, check_redis
from app.core.settings import Settings
from app.main import create_app
from fastapi.testclient import TestClient


async def _ok(name: str) -> CheckResult:
    return CheckResult(name, HealthStatus.healthy, "test double: ok", 0.1)


async def _down(name: str) -> CheckResult:
    return CheckResult(name, HealthStatus.unavailable, "test double: unavailable", 0.1)


def test_health_reports_mode_and_no_secrets(settings: Settings) -> None:
    app = create_app(settings, checks={})
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["trading_mode"] == "paper"
    assert body["live_trading"] is False
    assert body["live_execution_allowed"] is False
    assert "postgresql" not in r.text and "redis://" not in r.text


def test_ready_when_all_checks_pass(settings: Settings) -> None:
    app = create_app(
        settings,
        checks={"database": lambda: _ok("database"), "redis": lambda: _ok("redis")},
    )
    with TestClient(app) as client:
        r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_a_critical_dependency_down_is_unavailable_and_503(settings: Settings) -> None:
    app = create_app(
        settings,
        checks={"database": lambda: _ok("database"), "redis": lambda: _down("redis")},
    )
    with TestClient(app) as client:
        r = client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["redis"]["ok"] is False
    assert body["checks"]["database"]["ok"] is True


def test_real_database_check_does_not_fake_success() -> None:
    # Port 1 on loopback is never a database. The check must say so.
    res = asyncio.run(check_database("postgresql+asyncpg://u:p@127.0.0.1:1/x", timeout=1.0))
    assert res.ok is False
    assert res.detail.startswith("unavailable:")
    assert "p@" not in res.detail  # password never echoed


def test_real_redis_check_does_not_fake_success() -> None:
    res = asyncio.run(check_redis("redis://127.0.0.1:1/0", timeout=1.0))
    assert res.ok is False
    assert res.detail.startswith("unavailable:")


def test_an_optional_dependency_down_is_degraded_not_unhealthy(settings: Settings) -> None:
    """Redis is optional until L07. The API still works without it, so the
    system is degraded rather than unavailable, and the probe stays 200."""
    from app.core.health import CheckResult, HealthStatus

    async def optional_down() -> CheckResult:
        return CheckResult("redis", HealthStatus.unavailable, "down", 0.1, critical=False)

    app = create_app(
        settings,
        checks={"database": lambda: _ok("database"), "redis": optional_down},
    )
    with TestClient(app) as client:
        r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["critical_unavailable"] == []


def test_a_dependency_that_answers_wrongly_is_degraded_not_unavailable() -> None:
    """Answering incorrectly and not answering at all are different faults."""
    from app.core.health import CheckResult, HealthStatus, overall_status

    wrong = CheckResult("database", HealthStatus.degraded, "unexpected result", 1.0, True)
    assert overall_status({"database": wrong}) is HealthStatus.degraded

    gone = CheckResult("database", HealthStatus.unavailable, "unavailable", 1.0, True)
    assert overall_status({"database": gone}) is HealthStatus.unavailable


def test_no_checks_at_all_is_healthy() -> None:
    from app.core.health import HealthStatus, overall_status

    assert overall_status({}) is HealthStatus.healthy


def test_worker_health_is_optional_and_empty_is_healthy() -> None:
    import asyncio

    from app.core.health import HealthStatus, WorkerHealth
    from app.workers.base import WorkerRegistry

    result = asyncio.run(WorkerHealth(WorkerRegistry())())
    assert result.status is HealthStatus.healthy
    assert result.critical is False
    assert "no workers registered" in result.detail


def test_a_stale_worker_degrades_but_does_not_take_the_system_down() -> None:
    import asyncio
    from datetime import timedelta

    from app.auth.models import utcnow
    from app.core.health import HealthStatus, WorkerHealth, overall_status
    from app.workers.base import Worker, WorkerRegistry

    registry = WorkerRegistry()
    worker = registry.register(Worker(name="stuck", interval_seconds=1.0))
    worker.status.running = True
    worker.status.last_heartbeat = utcnow() - timedelta(seconds=600)

    result = asyncio.run(WorkerHealth(registry)())
    assert result.status is HealthStatus.degraded
    assert "stuck" in result.detail
    assert overall_status({"workers": result}) is HealthStatus.degraded


def test_the_live_probe_touches_no_dependency(settings: Settings) -> None:

    async def explode() -> CheckResult:
        raise AssertionError("liveness must not run dependency checks")

    app = create_app(settings, checks={"database": explode})
    with TestClient(app) as client:
        r = client.get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "alive"}
