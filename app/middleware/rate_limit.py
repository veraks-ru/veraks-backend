"""Простой rate-limiter на Redis (фиксированное окно по IP).

ARCHITECTURE.md §6 требует ограничения частоты — без него дешёвыми запросами
можно брутфорсить/скрейпить/устраивать DoS. Реализация без внешних зависимостей:
счётчик ``INCR`` с TTL на окно. Ключ — клиентский IP + окно.

Два независимых лимита с разной политикой отказа:

- **Общий** (все пути) — fail-open: при сбое Redis запрос пропускается
  (лучше обслужить, чем положить сайт из-за недоступности лимитера — в
  отличие от биллинговой квоты B2B, которая намеренно fail-closed).
- **``/auth/*``** (инициация ЕСИА, callback, refresh) — заметно более жёсткий
  лимит и, наоборот, fail-closed (503 + ``Retry-After``): это точка входа для
  брутфорса/подбора состояний и токенов, при недоступном лимитере лучше
  временно отказать в аутентификации, чем снять с неё защиту.

Оба лимита включаются вне ``local`` (в тестах не мешает).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.http import client_ip

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ratelimit:"

# Пути, на которые действует ужесточённый auth-лимит (см. docstring модуля).
_AUTH_PATH_PREFIX = "/auth"


def _is_auth_path(path: str) -> bool:
    """Путь входит в группу ``/auth/*`` (с учётом границы сегмента)."""
    return path == _AUTH_PATH_PREFIX or path.startswith(f"{_AUTH_PATH_PREFIX}/")


@dataclass(frozen=True)
class RateLimitCheck:
    """Результат проверки лимита: разрешён ли запрос и когда повторить."""

    allowed: bool
    retry_after_seconds: int


async def check_rate_limit(
    redis: Redis, identity: str, *, limit: int, window_seconds: int
) -> RateLimitCheck:
    """Разрешён ли запрос: увеличивает счётчик окна и сверяет с лимитом.

    ``allowed=True``, если в текущем окне сделано не больше ``limit`` запросов.
    Окно фиксированное: ключ содержит номер окна, TTL = длина окна.
    ``retry_after_seconds`` — сколько секунд осталось до конца текущего окна
    (для заголовка ``Retry-After`` в ответе 429/503), минимум 1.
    """
    # Номер окна нужен детерминированный; берём из Redis TIME, чтобы не зависеть
    # от локальных часов процесса (и обойти запрет argless time в некоторых средах).
    now_seconds = int((await redis.time())[0])
    window = now_seconds // window_seconds
    key = f"{_KEY_PREFIX}{identity}:{window}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    window_end = (window + 1) * window_seconds
    retry_after = max(window_end - now_seconds, 1)
    return RateLimitCheck(allowed=int(count) <= limit, retry_after_seconds=retry_after)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Ограничивает число запросов с одного IP в минуту (fixed window).

    Для путей ``/auth/*`` применяется отдельный (обычно более жёсткий) лимит
    ``auth_limit`` с fail-closed при сбое Redis; для остальных путей —
    ``limit`` с fail-open. Каждая группа лимитов ведёт свой счётчик (ключи
    разделены префиксом identity), чтобы обращения к /auth не расходовали
    общий лимит и наоборот. ``limit``/``auth_limit`` равные 0 отключают
    проверку для соответствующей группы путей (запрос пропускается без
    обращения к Redis).
    """

    def __init__(
        self,
        app: object,
        *,
        redis_factory: Callable[[], Redis],
        limit: int,
        auth_limit: int,
        window_seconds: int = 60,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._redis_factory = redis_factory
        self._limit = limit
        self._auth_limit = auth_limit
        self._window = window_seconds

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        is_auth_path = _is_auth_path(request.url.path)
        limit = self._auth_limit if is_auth_path else self._limit
        if limit <= 0:
            return await call_next(request)

        ip = client_ip(request) or "unknown"
        # Разные scope в identity — чтобы auth и общий лимит не делили один
        # счётчик даже при совпадении окна.
        identity = f"{'auth' if is_auth_path else 'global'}:{ip}"
        try:
            result = await check_rate_limit(
                self._redis_factory(),
                identity,
                limit=limit,
                window_seconds=self._window,
            )
        except Exception:  # noqa: BLE001 — политика отказа зависит от группы путей
            if is_auth_path:
                # Fail-closed: без лимитера auth-эндпоинты беззащитны перед
                # брутфорсом — временно отказываем, а не снимаем защиту.
                logger.warning(
                    "rate limiter unavailable — fail-closed on /auth", exc_info=True
                )
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Сервис временно недоступен — попробуйте позже"},
                    headers={"Retry-After": str(self._window)},
                )
            logger.warning("rate limiter unavailable — allowing request", exc_info=True)
            return await call_next(request)

        if not result.allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Слишком много запросов — попробуйте позже"},
                headers={"Retry-After": str(result.retry_after_seconds)},
            )
        return await call_next(request)
