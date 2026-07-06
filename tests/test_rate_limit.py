"""Юнит-тест ядра rate-limiter'а (H-RATELIMIT) на фейковом Redis."""

from __future__ import annotations

from app.middleware.rate_limit import check_rate_limit


class _FakeRedis:
    """Минимальный фейк: INCR/EXPIRE/TIME в памяти, фиксированное время."""

    def __init__(self, now: int = 1_000_000) -> None:
        self._counters: dict[str, int] = {}
        self._now = now

    async def time(self) -> tuple[int, int]:
        return (self._now, 0)

    async def incr(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    def advance(self, seconds: int) -> None:
        self._now += seconds


async def test_allows_up_to_limit_then_blocks() -> None:
    redis = _FakeRedis()
    results = [
        await check_rate_limit(redis, "1.2.3.4", limit=3, window_seconds=60)
        for _ in range(5)
    ]
    assert results == [True, True, True, False, False]


async def test_separate_identities_have_separate_windows() -> None:
    redis = _FakeRedis()
    assert await check_rate_limit(redis, "a", limit=1, window_seconds=60) is True
    assert await check_rate_limit(redis, "a", limit=1, window_seconds=60) is False
    # Другой IP — свой счётчик.
    assert await check_rate_limit(redis, "b", limit=1, window_seconds=60) is True


async def test_new_window_resets_counter() -> None:
    redis = _FakeRedis()
    assert await check_rate_limit(redis, "a", limit=1, window_seconds=60) is True
    assert await check_rate_limit(redis, "a", limit=1, window_seconds=60) is False
    redis.advance(60)  # следующее окно
    assert await check_rate_limit(redis, "a", limit=1, window_seconds=60) is True
