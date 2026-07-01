"""Суточная квота ключа на счётчиках Redis (по UTC-дню).

Fail-open: при недоступности Redis запрос НЕ блокируется (сигнальный API важнее
жёсткого лимита; злоупотребление ловится позже по логам). Ключ живёт сутки.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from redis.asyncio import Redis

_DAY_SECONDS = 86_400


def _bucket(key_id: uuid.UUID) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"b2b:quota:{key_id}:{day}"


class RedisQuotaCounter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check_and_incr(
        self, key_id: uuid.UUID, *, daily_quota: int
    ) -> tuple[bool, int]:
        bucket = _bucket(key_id)
        try:
            count = int(await self._redis.incr(bucket))
            if count == 1:
                await self._redis.expire(bucket, _DAY_SECONDS)
        except Exception:  # noqa: BLE001 — Redis недоступен → fail-open
            return True, 0
        return count <= daily_quota, count

    async def used_today(self, key_id: uuid.UUID) -> int:
        try:
            value = await self._redis.get(_bucket(key_id))
        except Exception:  # noqa: BLE001
            return 0
        return int(value) if value is not None else 0
