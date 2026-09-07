"""Redis client factory. Redis carries signals and heartbeats between the API
and the workers from L07 onward; at this level it is only health-checked."""

from __future__ import annotations

import redis.asyncio as aioredis


def make_redis(url: str) -> aioredis.Redis:
    return aioredis.from_url(url, decode_responses=True)
