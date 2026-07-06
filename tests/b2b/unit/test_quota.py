"""Юнит-тесты суточной квоты Redis (M-QUOTA): fail-closed + атомарный TTL."""

from __future__ import annotations

import uuid

from app.modules.b2b.adapters.quota import _DAY_SECONDS, RedisQuotaCounter
from tests.b2b.fakes import FakeRedis


async def test_incr_sets_ttl_on_every_call() -> None:
    redis = FakeRedis()
    counter = RedisQuotaCounter(redis)
    key_id = uuid.uuid4()

    allowed, count = await counter.check_and_incr(key_id, daily_quota=5)
    assert allowed is True
    assert count == 1
    # TTL выставлен атомарно вместе с INCR (не отдельным условным EXPIRE).
    assert redis.expire_count == 1
    assert set(redis.ttls.values()) == {_DAY_SECONDS}

    # Второй инкремент СНОВА ставит TTL (безусловно) — ключ не может остаться
    # без срока жизни, даже если первый EXPIRE был потерян.
    await counter.check_and_incr(key_id, daily_quota=5)
    assert redis.expire_count == 2


async def test_quota_boundary_allows_up_to_limit() -> None:
    redis = FakeRedis()
    counter = RedisQuotaCounter(redis)
    key_id = uuid.uuid4()

    results = [
        await counter.check_and_incr(key_id, daily_quota=2) for _ in range(3)
    ]
    assert [allowed for allowed, _ in results] == [True, True, False]
    assert [count for _, count in results] == [1, 2, 3]


async def test_fail_closed_when_redis_unavailable() -> None:
    # Сбой Redis → запрос БЛОКИРУЕТСЯ (fail-closed), а не пропускается.
    counter = RedisQuotaCounter(FakeRedis(fail=True))
    allowed, count = await counter.check_and_incr(uuid.uuid4(), daily_quota=1000)
    assert allowed is False
    assert count == 0


async def test_used_today_reads_counter() -> None:
    redis = FakeRedis()
    counter = RedisQuotaCounter(redis)
    key_id = uuid.uuid4()
    await counter.check_and_incr(key_id, daily_quota=10)
    await counter.check_and_incr(key_id, daily_quota=10)
    assert await counter.used_today(key_id) == 2


async def test_used_today_returns_zero_on_error() -> None:
    counter = RedisQuotaCounter(FakeRedis(fail=True))
    assert await counter.used_today(uuid.uuid4()) == 0
