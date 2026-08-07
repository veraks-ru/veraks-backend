"""Redis-адаптеры ссылок входа против НАСТОЯЩЕГО Redis.

Фейком тут не обойтись: проверяется ровно то, что фейк подделывает, —
атомарность (``GETDEL``, скрипт счётчика) и реальные TTL. Поэтому тесты идут
против живого сервера и пропускаются, если его нет (та же логика, что у
``tests/e2e`` с Postgres).

База — из ``REDIS_URL`` (в тест-окружении это db 15, см. tests/conftest.py);
свои ключи тест убирает за собой сам.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.modules.identity.adapters.stores import (
    _MAGIC_LINK_PREFIX,
    _MAGIC_LINK_QUOTA_PREFIX,
    RedisMagicLinkStore,
)

_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")


@pytest_asyncio.fixture
async def redis() -> Redis:
    """Живой Redis или пропуск теста; после теста чистит свои ключи."""
    client: Redis = Redis.from_url(_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 — нет сервера, это не падение теста
        await client.aclose()
        pytest.skip(f"нужен запущенный Redis на {_URL}")
    await _cleanup(client)
    try:
        yield client
    finally:
        await _cleanup(client)
        await client.aclose()


async def _cleanup(client: Redis) -> None:
    """Удаляет только ключи ссылок входа — чужие данные в базе не трогаем."""
    for prefix in (_MAGIC_LINK_PREFIX, _MAGIC_LINK_QUOTA_PREFIX):
        keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)


# ── Ссылка входа ──────────────────────────────────────────────────────────


async def test_link_is_consumed_exactly_once(redis: Redis) -> None:
    """``GETDEL`` гасит запись атомарно: второй переход по ссылке пуст."""
    store = RedisMagicLinkStore(redis)
    await store.save("hash-1", "user@example.com", 900)

    assert await store.consume("hash-1") == "user@example.com"
    assert await store.consume("hash-1") is None


async def test_unknown_link_returns_none(redis: Redis) -> None:
    assert await RedisMagicLinkStore(redis).consume("never-issued") is None


async def test_link_gets_the_requested_ttl(redis: Redis) -> None:
    """Ссылка живёт ограниченное время, а не вечно."""
    await RedisMagicLinkStore(redis).save("hash-ttl", "user@example.com", 900)

    ttl = await redis.ttl(f"{_MAGIC_LINK_PREFIX}hash-ttl")

    assert 0 < ttl <= 900


# ── Счётчик писем на адрес ────────────────────────────────────────────────


async def test_quota_counts_and_sets_ttl_on_first_call(redis: Redis) -> None:
    store = RedisMagicLinkStore(redis)

    assert await store.count_request("quota-a", 3600) == 1
    assert await store.count_request("quota-a", 3600) == 2

    ttl = await redis.ttl(f"{_MAGIC_LINK_QUOTA_PREFIX}quota-a")
    assert 0 < ttl <= 3600


async def test_quota_window_is_fixed_not_sliding(redis: Redis) -> None:
    """Повторные запросы НЕ продлевают окно.

    Иначе непрерывный поток запросов держал бы адрес заблокированным сколь
    угодно долго — окно должно закрываться по расписанию, а не по затишью.
    """
    store = RedisMagicLinkStore(redis)
    key = f"{_MAGIC_LINK_QUOTA_PREFIX}quota-b"
    await store.count_request("quota-b", 3600)
    await redis.expire(key, 100)  # «прошло» почти всё окно

    await store.count_request("quota-b", 3600)

    assert await redis.ttl(key) <= 100


async def test_quota_key_without_ttl_is_healed(redis: Redis) -> None:
    """Ключ, оставшийся без TTL, чинится следующим же запросом.

    Ровно та авария, ради которой счётчик переехал в атомарный скрипт: пара
    «INCR, затем EXPIRE только при count == 1» при падении процесса между
    командами оставляла ключ без срока жизни — и адрес блокировался НАВСЕГДА,
    без всякого инструмента сброса у поддержки. Здесь такое состояние
    воспроизводится напрямую (INCR без EXPIRE), и следующий вызов обязан
    вернуть ключу TTL.
    """
    key = f"{_MAGIC_LINK_QUOTA_PREFIX}quota-broken"
    await redis.incr(key)  # «сбой»: счётчик есть, срока жизни нет
    assert await redis.ttl(key) == -1

    count = await RedisMagicLinkStore(redis).count_request("quota-broken", 3600)

    assert count == 2
    assert 0 < await redis.ttl(key) <= 3600
