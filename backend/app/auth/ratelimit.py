"""Rate limiting for authentication endpoints.

A fixed window per (key, bucket). Two backends behind one protocol:

  `InMemoryRateLimiter` per-process, the default. Correct for one process and
                        honest about not being shared: a multi-process
                        deployment needs the Redis backend or the limit is
                        per-worker rather than global.
  `RedisRateLimiter`    shared across processes, using INCR with an expiry.

Login, registration and password reset are limited because they are the three
endpoints an attacker can drive without a session. The limiter keys on the
client address **and** the submitted identifier, so one address cannot spray
many accounts and one account cannot be sprayed from many addresses without
one of the two counters catching it.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class RateLimit:
    """`limit` attempts per `window_seconds`."""

    limit: int
    window_seconds: int

    @property
    def describe(self) -> str:
        return f"{self.limit} per {self.window_seconds}s"


# Deliberately generous enough not to hinder development, tight enough that
# credential stuffing is not free. Tuned per endpoint at L39 with real traffic.
LOGIN_LIMIT = RateLimit(limit=10, window_seconds=300)
REGISTER_LIMIT = RateLimit(limit=5, window_seconds=3600)
RESET_LIMIT = RateLimit(limit=5, window_seconds=3600)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int
    limit: RateLimit

    @property
    def detail(self) -> str:
        return f"too many attempts ({self.limit.describe}); retry in {self.retry_after}s"


class RateLimiter(Protocol):
    kind: str

    async def hit(self, bucket: str, key: str, limit: RateLimit) -> Decision: ...

    async def reset(self, bucket: str, key: str) -> None: ...


@dataclass
class InMemoryRateLimiter:
    """Per-process fixed window. Not shared between workers, and says so."""

    kind: str = "in_memory"
    _hits: dict[tuple[str, str], list[float]] = field(default_factory=lambda: defaultdict(list))

    async def hit(self, bucket: str, key: str, limit: RateLimit) -> Decision:
        now = time.monotonic()
        cutoff = now - limit.window_seconds
        entry = self._hits[(bucket, key)]
        entry[:] = [t for t in entry if t > cutoff]

        if len(entry) >= limit.limit:
            oldest = min(entry)
            retry = max(1, int(limit.window_seconds - (now - oldest)))
            return Decision(False, 0, retry, limit)

        entry.append(now)
        return Decision(True, limit.limit - len(entry), 0, limit)

    async def reset(self, bucket: str, key: str) -> None:
        self._hits.pop((bucket, key), None)


class RedisRateLimiter:
    """Shared fixed window. INCR plus an expiry set on first hit."""

    kind = "redis"

    def __init__(self, url: str, prefix: str = "tr.rl") -> None:
        self.url = url
        self.prefix = prefix
        self._client: Any | None = None

    async def _connect(self):  # noqa: ANN202 - redis client type is dynamic
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self.url, decode_responses=True)
        return self._client

    def _key(self, bucket: str, key: str) -> str:
        return f"{self.prefix}:{bucket}:{key}"

    async def hit(self, bucket: str, key: str, limit: RateLimit) -> Decision:
        client = await self._connect()
        name = self._key(bucket, key)
        count = await client.incr(name)
        if count == 1:
            await client.expire(name, limit.window_seconds)
        ttl = await client.ttl(name)
        retry = max(1, int(ttl)) if ttl and ttl > 0 else limit.window_seconds
        if count > limit.limit:
            return Decision(False, 0, retry, limit)
        return Decision(True, limit.limit - int(count), 0, limit)

    async def reset(self, bucket: str, key: str) -> None:
        client = await self._connect()
        await client.delete(self._key(bucket, key))


def make_rate_limiter(redis_url: str | None) -> RateLimiter:
    return RedisRateLimiter(redis_url) if redis_url else InMemoryRateLimiter()
