"""Простой rate-limiter на Redis (фиксированное окно по IP).

ARCHITECTURE.md §6 требует ограничения частоты — без него дешёвыми запросами
можно брутфорсить/скрейпить/устраивать DoS. Реализация без внешних зависимостей:
счётчик ``INCR`` с TTL на окно. Ключ — клиентский IP + окно.

Два независимых лимита с разной политикой отказа:

- **Общий** (все пути) — fail-open: при сбое Redis запрос пропускается
  (лучше обслужить, чем положить сайт из-за недоступности лимитера — в
  отличие от биллинговой квоты B2B, которая намеренно fail-closed).
- **Строгий** (явный список «попыток входа», см. :data:`_STRICT_AUTH_ROUTES`) —
  заметно более жёсткий лимит и, наоборот, fail-closed (503 +
  ``Retry-After``): это точка входа для брутфорса/подбора состояний и
  токенов, при недоступном лимитере лучше временно отказать в
  аутентификации, чем снять с неё защиту.

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

_STRICT_AUTH_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/auth/email/request"),
        ("POST", "/auth/email/callback"),
        ("POST", "/auth/refresh"),
        ("GET", "/auth/esia/login"),
        ("GET", "/auth/esia/callback"),
    }
)
"""Маршруты (метод + путь) под ужесточённым лимитом — «попытки входа».

**Критерий:** строгий лимит там, где запрос ПОРОЖДАЕТ письмо, сессию или
криптооперацию, а не там, где просто читается текущее состояние. Каждый
маршрут из списка либо отправляет письмо со ссылкой входа, либо обменивает
секрет (токен из письма, authorization code, refresh-токен) на новую сессию,
либо выпускает серверный секрет (``state``/PKCE) — то есть стоит денег,
писем или даёт материал для перебора. Именно их и имеет смысл душить 20
запросами в минуту с IP.

Остальное под ``/auth`` — ``GET /auth/me``, ``GET /auth/providers``,
``POST /auth/logout`` — идёт под общий лимит. Это чтения/сброс состояния,
которые фронт дёргает на каждом переходе по сайту: под строгим лимитом
несколько человек за одним офисным NAT выбирали бы бакет за минуту обычной
навигации и получали 429 на ровном месте. Раньше группа задавалась префиксом
``/auth``, и все они попадали под строгий лимит — это и была та ошибка.

Список ведётся вручную: новый эндпоинт под ``/auth`` по умолчанию окажется
под общим лимитом, поэтому, добавляя «порождающий» маршрут, его нужно внести
сюда явно (тесты в ``tests/test_rate_limit.py`` фиксируют текущий состав).
"""


def _normalize_path(path: str) -> str:
    """Схлопывает хвостовой слэш: ``/auth/refresh/`` — тот же маршрут.

    Middleware работает ДО роутинга, поэтому без нормализации приписанный
    слэш переводил бы «попытку входа» под мягкий общий лимит (Starlette
    всё равно отредиректит запрос на канонический путь).
    """
    return path.rstrip("/") or "/"


def _is_strict_auth_route(method: str, path: str) -> bool:
    """Относится ли запрос к «попыткам входа» (строгий лимит, fail-closed).

    ``HEAD`` считается за ``GET``: Starlette обслуживает GET-маршруты и по
    HEAD, то есть эндпоинт реально отработает (например, выпустит ``state``),
    просто тело не уйдёт.
    """
    normalized_method = "GET" if method.upper() == "HEAD" else method.upper()
    return (normalized_method, _normalize_path(path)) in _STRICT_AUTH_ROUTES


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

    Для «попыток входа» (:data:`_STRICT_AUTH_ROUTES`) применяется отдельный
    (обычно более жёсткий) лимит ``auth_limit`` с fail-closed при сбое Redis;
    для всех остальных запросов, включая прочие пути под ``/auth``, —
    ``limit`` с fail-open. Каждая группа ведёт свой счётчик (ключи разделены
    префиксом identity), чтобы попытки входа не расходовали общий лимит и
    наоборот. ``limit``/``auth_limit`` равные 0 отключают проверку для
    соответствующей группы (запрос пропускается без обращения к Redis).
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
        is_strict = _is_strict_auth_route(request.method, request.url.path)
        limit = self._auth_limit if is_strict else self._limit
        if limit <= 0:
            return await call_next(request)

        ip = client_ip(request) or "unknown"
        # Разные scope в identity — чтобы строгий и общий лимит не делили один
        # счётчик даже при совпадении окна.
        identity = f"{'auth' if is_strict else 'global'}:{ip}"
        try:
            result = await check_rate_limit(
                self._redis_factory(),
                identity,
                limit=limit,
                window_seconds=self._window,
            )
        except Exception:
            if is_strict:
                # Fail-closed: без лимитера попытки входа беззащитны перед
                # брутфорсом — временно отказываем, а не снимаем защиту.
                logger.warning(
                    "rate limiter unavailable — fail-closed on login attempt",
                    exc_info=True,
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
