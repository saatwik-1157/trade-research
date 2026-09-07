"""Readiness checks with three states.

    HEALTHY      the dependency answered correctly
    DEGRADED     it answered, but not the way it should have
    UNAVAILABLE  it could not be reached

The distinction between DEGRADED and UNAVAILABLE matters operationally: one
is a service behaving oddly, the other is a service that is not there, and
they call for different responses.

Overall status is derived, not asserted:

  * any CRITICAL dependency unavailable -> UNAVAILABLE (HTTP 503)
  * any critical degraded, or any optional not healthy -> DEGRADED (HTTP 200)
  * everything healthy -> HEALTHY (HTTP 200)

**The system is never reported healthy while a critical dependency is down.**
Criticality is a property of the check rather than a global rule, because it
changes as levels land: Redis is optional today, since nothing depends on it,
and becomes critical at L07 when the event bus carries trading events.

Each check reports what it actually observed. A dependency that cannot be
reached is reported unavailable with the exception type; it is never reported
healthy because the code path exists.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


class HealthStatus(StrEnum):
    healthy = "healthy"
    degraded = "degraded"
    unavailable = "unavailable"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: HealthStatus
    detail: str
    latency_ms: float
    critical: bool = True

    @property
    def ok(self) -> bool:
        return self.status is HealthStatus.healthy

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["status"] = str(self.status)
        data["ok"] = self.ok
        return data


CheckFn = Callable[[], Awaitable[CheckResult]]


def overall_status(results: dict[str, CheckResult]) -> HealthStatus:
    if not results:
        return HealthStatus.healthy
    critical = [r for r in results.values() if r.critical]
    if any(r.status is HealthStatus.unavailable for r in critical):
        return HealthStatus.unavailable
    if any(r.status is not HealthStatus.healthy for r in results.values()):
        return HealthStatus.degraded
    return HealthStatus.healthy


def _describe(exc: BaseException) -> str:
    # Driver exception text can carry the host and user; it never carries the
    # password, which is the one thing that must not leak.
    text_ = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text_}"[:200]


async def check_database(url: str, timeout: float, critical: bool = True) -> CheckResult:
    started = time.perf_counter()
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as conn:
                value = (await conn.execute(text("SELECT 1"))).scalar_one()
        if value == 1:
            status, detail = HealthStatus.healthy, "SELECT 1 ok"
        else:
            # It answered, but wrongly. That is degraded, not unavailable.
            status, detail = HealthStatus.degraded, f"unexpected result {value!r}"
    except (TimeoutError, Exception) as exc:  # noqa: BLE001 - reported, not hidden
        status, detail = HealthStatus.unavailable, f"unavailable: {_describe(exc)}"
    finally:
        await engine.dispose()
    return CheckResult(
        "database", status, detail, round((time.perf_counter() - started) * 1000, 1), critical
    )


async def check_redis(url: str, timeout: float, critical: bool = False) -> CheckResult:
    import redis.asyncio as aioredis

    started = time.perf_counter()
    client = aioredis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)
    try:
        async with asyncio.timeout(timeout):
            pong = await client.ping()
        if pong:
            status, detail = HealthStatus.healthy, "PING ok"
        else:
            status, detail = HealthStatus.degraded, f"unexpected reply {pong!r}"
    except (TimeoutError, Exception) as exc:  # noqa: BLE001
        status, detail = HealthStatus.unavailable, f"unavailable: {_describe(exc)}"
    finally:
        await client.aclose()
    return CheckResult(
        "redis", status, detail, round((time.perf_counter() - started) * 1000, 1), critical
    )


@dataclass
class WorkerHealth:
    """Reads the worker registry. Never critical: an API with no workers is a
    working API, and 'none registered' is a fact rather than a failure."""

    registry: object = field(default=None)

    async def __call__(self) -> CheckResult:
        started = time.perf_counter()
        registry = self.registry
        if registry is None or not getattr(registry, "workers", {}):
            return CheckResult(
                "workers",
                HealthStatus.healthy,
                "no workers registered in this process",
                round((time.perf_counter() - started) * 1000, 1),
                critical=False,
            )
        stale = registry.stale()  # type: ignore[attr-defined]
        total = len(registry.workers)  # type: ignore[attr-defined]
        if stale:
            status = HealthStatus.degraded
            detail = f"{len(stale)} of {total} workers stale: {', '.join(sorted(stale))}"
        else:
            status, detail = HealthStatus.healthy, f"{total} worker(s) running"
        return CheckResult(
            "workers", status, detail, round((time.perf_counter() - started) * 1000, 1), False
        )


async def run_checks(checks: dict[str, CheckFn]) -> dict[str, CheckResult]:
    names = list(checks)
    results = await asyncio.gather(*(checks[n]() for n in names))
    return dict(zip(names, results, strict=True))
