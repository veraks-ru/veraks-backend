"""Тесты rate-limiter'а (H-RATELIMIT, T11): ядро на фейковом Redis + middleware."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.rate_limit import (
    _STRICT_AUTH_ROUTES,
    RateLimitMiddleware,
    check_rate_limit,
)


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


# ── Middleware: попытки входа против всего остального ─────────────────────
#
# Строгий лимит вешается на явный список маршрутов «попыток входа», а НЕ на
# префикс /auth: под префиксом живут ещё и GET /auth/me, GET /auth/providers,
# POST /auth/logout, которые фронт дёргает на каждом переходе по сайту. Пока
# группа задавалась префиксом, обычная навигация нескольких человек за одним
# офисным NAT выедала бакет 20/мин и получала 429 на ровном месте.


def _make_app(redis_factory: Callable[[], object], *, limit: int, auth_limit: int) -> Starlette:
    """Мини-приложение с реальными путями обеих групп (см. app.main)."""

    async def ok(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/events", ok),
            # «Порождающие» маршруты — строгая группа.
            Route("/auth/refresh", ok, methods=["POST"]),
            Route("/auth/email/request", ok, methods=["POST"]),
            Route("/auth/email/callback", ok, methods=["POST"]),
            Route("/auth/esia/login", ok),
            Route("/auth/esia/callback", ok),
            # Чтения/сброс состояния под тем же префиксом — общая группа.
            Route("/auth/me", ok),
            Route("/auth/providers", ok),
            Route("/auth/logout", ok, methods=["POST"]),
        ]
    )
    app.add_middleware(
        RateLimitMiddleware,
        redis_factory=redis_factory,
        limit=limit,
        auth_limit=auth_limit,
    )
    return app


def test_login_attempt_uses_stricter_limit_and_returns_429_with_retry_after() -> None:
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=100, auth_limit=1)
    with TestClient(app) as client:
        first = client.post("/auth/refresh")
        second = client.post("/auth/refresh")

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_email_request_consumes_strict_bucket() -> None:
    """Запрос письма со ссылкой входа — «порождающий» маршрут, строгая группа."""
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=100, auth_limit=1)
    with TestClient(app) as client:
        first = client.post("/auth/email/request", json={"email": "a@example.com"})
        second = client.post("/auth/email/request", json={"email": "a@example.com"})

    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/auth/email/request"),
        ("POST", "/auth/email/callback"),
        ("POST", "/auth/refresh"),
        ("GET", "/auth/esia/login"),
        ("GET", "/auth/esia/callback"),
    ],
)
def test_strict_group_membership(method: str, path: str) -> None:
    """Фиксируем состав строгой группы: каждый её маршрут душится auth-лимитом."""
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=100, auth_limit=1)
    with TestClient(app) as client:
        assert client.request(method, path).status_code == 200
        assert client.request(method, path).status_code == 429


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/auth/me"), ("GET", "/auth/providers"), ("POST", "/auth/logout")],
)
def test_read_only_auth_paths_do_not_consume_strict_bucket(
    method: str, path: str
) -> None:
    """Главная регрессия: навигация по сайту не выедает бакет попыток входа.

    Строгий лимит выставлен в 1, и путь дёргается многократно — но он не
    «порождающий», поэтому идёт под общий лимит и не влияет на возможность
    реально войти.
    """
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=100, auth_limit=1)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.request(method, path).status_code == 200
        # Бакет попыток входа не тронут: вход по-прежнему возможен.
        assert client.post("/auth/refresh").status_code == 200


def test_strict_limit_does_not_consume_global_counter() -> None:
    """Строгий лимит бьёт по попыткам входа, но не расходует общий лимит."""
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=100, auth_limit=1)
    with TestClient(app) as client:
        client.post("/auth/refresh")
        client.post("/auth/refresh")  # уже 429 по строгому лимиту
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


def test_read_only_auth_path_uses_global_limit() -> None:
    """GET /auth/me лимитируется — но общим лимитом, а не строгим."""
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=1, auth_limit=100)
    with TestClient(app) as client:
        assert client.get("/auth/me").status_code == 200
        assert client.get("/auth/me").status_code == 429


def test_trailing_slash_does_not_escape_strict_group() -> None:
    """Приписанный слэш не переводит попытку входа под мягкий лимит."""
    redis = _FakeRedis()
    app = _make_app(lambda: redis, limit=100, auth_limit=1)
    with TestClient(app) as client:
        client.post("/auth/refresh", follow_redirects=False)
        second = client.post("/auth/refresh/", follow_redirects=False)

    assert second.status_code == 429


def test_redis_failure_is_fail_closed_on_login_attempt() -> None:
    app = _make_app(lambda: _BrokenRedis(), limit=100, auth_limit=20)
    with TestClient(app) as client:
        response = client.post("/auth/refresh")

    assert response.status_code == 503
    assert "Retry-After" in response.headers


def test_redis_failure_is_fail_open_on_ordinary_path() -> None:
    app = _make_app(lambda: _BrokenRedis(), limit=100, auth_limit=20)
    with TestClient(app) as client:
        response = client.get("/events")

    assert response.status_code == 200


def test_redis_failure_is_fail_open_on_read_only_auth_path() -> None:
    """Сбой Redis не должен разлогинивать сайт: /auth/me — общая группа."""
    app = _make_app(lambda: _BrokenRedis(), limit=100, auth_limit=20)
    with TestClient(app) as client:
        response = client.get("/auth/me")

    assert response.status_code == 200


def test_strict_group_matches_real_app_routes() -> None:
    """Список маршрутов сверяется с реальным приложением.

    Две ошибки, которые ловит этот тест: опечатка/переименование (маршрут
    молча уходит под мягкий лимит) и новый эндпоинт под ``/auth``, для
    которого никто не решил, «порождающий» он или нет.
    """
    from app.main import create_app

    schema = create_app().openapi()
    actual = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    auth_routes = {(method, path) for method, path in actual if path.startswith("/auth")}

    assert _STRICT_AUTH_ROUTES <= actual  # все строгие маршруты существуют
    assert auth_routes - _STRICT_AUTH_ROUTES == {
        ("GET", "/auth/me"),
        ("GET", "/auth/providers"),
        ("POST", "/auth/logout"),
    }


@pytest.mark.parametrize("auth_limit", [0])
def test_zero_auth_limit_disables_strict_group_check(auth_limit: int) -> None:
    """auth_limit=0 — попытки входа не лимитируются отдельно (Redis не дёргается)."""
    app = _make_app(lambda: _BrokenRedis(), limit=100, auth_limit=auth_limit)
    with TestClient(app) as client:
        response = client.post("/auth/refresh")

    assert response.status_code == 200
