"""Простой rate-limiter на Redis (фиксированное окно по IP).

ARCHITECTURE.md §6 требует ограничения частоты — без него дешёвыми запросами
можно брутфорсить/скрейпить/устраивать DoS. Реализация без внешних зависимостей:
счётчик ``INCR`` с TTL на окно. Ключ — клиентский IP + окно.

Fail-open: при сбое Redis запрос пропускается (лучше обслужить, чем положить
сайт из-за недоступности лимитера — в отличие от биллинговой квоты B2B, которая
намеренно fail-closed). Включается вне ``local`` (в тестах не мешает).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ratelimit:"


async def check_rate_limit(
    redis: Redis, identity: str, *, limit: int, window_seconds: int
) -> bool:
    """Разрешён ли запрос: увеличивает счётчик окна и сверяет с лимитом.

    Возвращает ``True``, если в текущем окне сделано не больше ``limit`` запросов.
    Окно фиксированное: ключ содержит номер окна, TTL = длина окна.
    """
    # Номер окна нужен детерминированный; берём из Redis TIME, чтобы не зависеть
    # от локальных часов процесса (и обойти запрет argless time в некоторых средах).
    now_seconds = int((await redis.time())[0])
    window = now_seconds // window_seconds
    key = f"{_KEY_PREFIX}{identity}:{window}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return int(count) <= limit


def _client_ip(request: Request) -> str:
    """IP клиента с учётом обратного прокси (первый в ``X-Forwarded-For``)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Ограничивает число запросов с одного IP в минуту (fixed window)."""

    def __init__(
        self,
        app: object,
        *,
        redis_factory: Callable[[], Redis],
        limit: int,
        window_seconds: int = 60,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._redis_factory = redis_factory
        self._limit = limit
        self._window = window_seconds

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        identity = _client_ip(request)
        try:
            allowed = await check_rate_limit(
                self._redis_factory(),
                identity,
                limit=self._limit,
                window_seconds=self._window,
            )
        except Exception:  # noqa: BLE001 — fail-open: сбой лимитера не роняет сайт
            logger.warning("rate limiter unavailable — allowing request", exc_info=True)
            allowed = True
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Слишком много запросов — попробуйте позже"},
            )
        return await call_next(request)
