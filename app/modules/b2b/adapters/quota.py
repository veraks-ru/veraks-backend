"""Суточная квота ключа на счётчиках Redis (по UTC-дню).

Fail-closed: при недоступности Redis запрос БЛОКИРУЕТСЯ. Для платного API это
безопаснее fail-open — иначе сбой Redis снимал бы лимиты и открывал бесплатное
безлимитное потребление. Отказ отдаётся как исчерпание квоты (429).

Счётчик и его TTL выставляются АТОМАРНО в одной транзакции ``MULTI/EXEC``
(``INCR`` + безусловный ``EXPIRE``). Прежняя схема (``INCR``, затем ``EXPIRE``
только при ``count == 1``) теряла TTL, если процесс падал между командами, —
ключ оставался вечным. Здесь ``EXPIRE`` идёт при каждом инкременте, поэтому TTL
установлен всегда; окно суток задаётся именем ключа (в нём — UTC-день), а TTL
лишь подчищает вчерашние счётчики.
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
            # MULTI/EXEC: INCR и EXPIRE применяются вместе — TTL не может
            # «потеряться» из-за падения между командами.
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(bucket)
                pipe.expire(bucket, _DAY_SECONDS)
                incr_result, _expire_result = await pipe.execute()
            count = int(incr_result)
        except Exception:  # noqa: BLE001 — Redis недоступен → fail-closed (429)
            return False, 0
        return count <= daily_quota, count

    async def used_today(self, key_id: uuid.UUID) -> int:
        try:
            value = await self._redis.get(_bucket(key_id))
        except Exception:  # noqa: BLE001 — информационный вызов, не гейт доступа
            return 0
        return int(value) if value is not None else 0
