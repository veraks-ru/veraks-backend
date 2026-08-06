"""Тесты rate-limiter'а (H-RATELIMIT, T11): ядро на фейковом Redis + middleware."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.rate_limit import RateLimitMiddleware, check_rate_limit


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


class _BrokenRedis:
    """Фейк, имитирующий недоступность Redis (любой вызов роняет исключение)."""

    async def time(self) -> tuple[int, int]:
        raise ConnectionError("redis unavailable")


async def test_allows_up_to_limit_then_blocks() -> None:
    redis = _FakeRedis()
    results = [
        (await check_rate_limit(redis, "1.2.3.4", limit=3, window_seconds=60)).allowed
        for _ in range(5)
    ]
    assert results == [True, True, True, False, False]


async def test_separate_identities_have_separate_windows() -> None:
    redis = _FakeRedis()
    assert (await check_rate_limit(redis, "a", limit=1, window_seconds=60)).allowed is True
    assert (await check_rate_limit(redis, "a", limit=1, window_seconds=60)).allowed is False
    # Другой IP — свой счётчик.
    assert (await check_rate_limit(redis, "b", limit=1, window_seconds=60)).allowed is True


async def test_new_window_resets_counter() -> None:
    redis = _FakeRedis()
    assert (await check_rate_limit(redis, "a", limit=1, window_seconds=60)).allowed is True
    assert (await check_rate_limit(redis, "a", limit=1, window_seconds=60)).allowed is False
    redis.advance(60)  # следующее окно
    assert (await check_rate_limit(redis, "a", limit=1, window_seconds=60)).allowed is True


async def test_retry_after_is_time_left_in_window() -> None:
    redis = _FakeRedis(now=60_000_010)  # 10с внутри минутного окна (60_000_000 кратно 60)
    result = await check_rate_limit(redis, "a", limit=0, window_seconds=60)
    assert result.allowed is False
    assert result.retry_after_seconds == 50


# ── Middleware: /auth/* против остальных путей ────────────────────────────


def _make_app(redis_factory: Callable[[], object], *, limit: int, auth_limit: int) -> Starlette:
    """Мини-приложение с двумя путями: обычным и под /auth/*."""

    async def ok(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/events", ok),
            Route("/auth/refresh", ok),
        ]
    )
    app.add_middleware(
        RateLimitMiddleware,
        redis_factory=redis_factory,
        limit=limit,
        auth_limit=auth_limit,
    )
    return app


def test_auth_path_uses_stricter_limit_and_returns_429_with_retry_after() -> None:
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=100, auth_limit=1)
    with TestClient(app) as client:
        first = client.get("/auth/refresh")
        second = client.get("/auth/refresh")

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_auth_limit_does_not_consume_global_counter() -> None:
    """Auth-лимит бьёт по /auth/*, но не расходует общий лимит для остальных путей."""
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=100, auth_limit=1)
    with TestClient(app) as client:
        client.get("/auth/refresh")
        client.get("/auth/refresh")  # уже 429 по auth-лимиту
        other = client.get("/events")

    assert other.status_code == 200


def test_ordinary_path_uses_global_limit() -> None:
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=1, auth_limit=100)
    with TestClient(app) as client:
        first = client.get("/events")
        second = client.get("/events")

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_redis_failure_is_fail_closed_on_auth_path() -> None:
    app = _make_app(lambda: _BrokenRedis(), limit=100, auth_limit=20)
    with TestClient(app) as client:
        response = client.get("/auth/refresh")

    assert response.status_code == 503
    assert "Retry-After" in response.headers


def test_redis_failure_is_fail_open_on_ordinary_path() -> None:
    app = _make_app(lambda: _BrokenRedis(), limit=100, auth_limit=20)
    with TestClient(app) as client:
        response = client.get("/events")

    assert response.status_code == 200


@pytest.mark.parametrize("auth_limit", [0])
def test_zero_auth_limit_disables_auth_group_check(auth_limit: int) -> None:
    """auth_limit=0 — auth-путь не лимитируется отдельно (Redis не дёргается)."""
    app = _make_app(lambda: _BrokenRedis(), limit=100, auth_limit=auth_limit)
    with TestClient(app) as client:
        response = client.get("/auth/refresh")

    assert response.status_code == 200
