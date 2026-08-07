"""Redis-адаптеры для OIDC-state, ссылок входа и реестра refresh-токенов.

State, magic-link и refresh-jti — короткоживущие записи с TTL, идеально
ложатся на Redis. В тестах вместо них подставляются in-memory фейки (см.
tests/identity/fakes.py).
"""

from __future__ import annotations

import json

from redis.asyncio import Redis

from app.modules.identity.application.dto import OidcFlowState

_STATE_PREFIX = "identity:oidc-state:"
_REFRESH_PREFIX = "identity:refresh-jti:"
_ROTATED_PREFIX = "identity:refresh-rotated:"
_USER_FAMILY_PREFIX = "identity:refresh-family:"
_MAGIC_LINK_PREFIX = "identity:magic-link:"
_MAGIC_LINK_QUOTA_PREFIX = "identity:magic-link-quota:"

# Счётчик писем на адрес: INCR и установка TTL — одной атомарной операцией.
#
# Наивная пара «INCR, затем EXPIRE только при count == 1» имеет узкое, но
# необратимое окно отказа: если процесс умрёт (или Redis отвергнет вторую
# команду) между командами, ключ останется БЕЗ TTL — навсегда. Счётчик такого
# адреса больше никогда не обнулится, ссылка входа на него не уйдёт НИКОГДА,
# а инструмента сброса у поддержки нет. Скрипт закрывает окно и заодно лечит
# уже испорченные ключи: TTL проставляется всякий раз, когда его нет
# (``TTL`` возвращает -1 у ключа без срока жизни).
#
# Почему Lua, а не ``EXPIRE key ttl NX``: опция NX появилась только в Redis 7.
# В проде и compose стоит 7 (infra/helm values.yaml, web/docker-compose.yml),
# но у разработчика локально может оказаться 6.x, и вход падал бы с ошибкой
# команды. ``EVAL`` есть с 2.6 и даёт ту же атомарность.
_QUOTA_INCR_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class RedisStateStore:
    """Одноразовый ``state`` + секреты OIDC-потока (PKCE ``code_verifier``, ``nonce``).

    Значение — JSON с секретами; они не покидают сервер и гаснут вместе со
    state (одноразовость обеспечивает атомарный ``GETDEL``).
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def save(self, state: str, flow: OidcFlowState, ttl_seconds: int) -> None:
        """Сохраняет state и секреты потока с TTL."""
        payload = json.dumps(
            {"code_verifier": flow.code_verifier, "nonce": flow.nonce}
        )
        await self._redis.set(f"{_STATE_PREFIX}{state}", payload, ex=ttl_seconds)

    async def consume(self, state: str) -> OidcFlowState | None:
        """Атомарно (``GETDEL``) гасит state и отдаёт секреты потока.

        ``GETDEL`` (Redis 6.2+) читает и удаляет одной командой — параллельный
        повтор callback'а с тем же state получит ``None``, как и раньше при
        ``DEL``.
        """
        raw = await self._redis.getdel(f"{_STATE_PREFIX}{state}")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return OidcFlowState(
                code_verifier=str(data["code_verifier"]), nonce=str(data["nonce"])
            )
        except (ValueError, TypeError, KeyError):
            # Битое/устаревшее значение (например, запись прежнего формата,
            # пережившая деплой) — трактуем как невалидный state.
            return None


class RedisMagicLinkStore:
    """Одноразовые ссылки входа (``sha256(токен) → адрес``) и лимит писем.

    По ключу лежит хэш токена, а не сам токен (см. ``domain.magic_link``);
    гашение — атомарным ``GETDEL``, как у ``RedisStateStore``, поэтому
    параллельный повтор перехода по ссылке получит ``None``.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def save(self, token_hash: str, email: str, ttl_seconds: int) -> None:
        """Кладёт адрес под хэшем токена с TTL ссылки."""
        await self._redis.set(f"{_MAGIC_LINK_PREFIX}{token_hash}", email, ex=ttl_seconds)

    async def consume(self, token_hash: str) -> str | None:
        """Атомарно (``GETDEL``) гасит ссылку и отдаёт адрес."""
        raw = await self._redis.getdel(f"{_MAGIC_LINK_PREFIX}{token_hash}")
        return None if raw is None else str(raw)

    async def count_request(self, quota_key: str, window_seconds: int) -> int:
        """Счётчик писем в фиксированном окне; TTL проставляется атомарно.

        См. :data:`_QUOTA_INCR_SCRIPT` — почему это скрипт, а не пара команд:
        ключ, оставшийся без TTL, заблокировал бы адрес навсегда.
        """
        key = f"{_MAGIC_LINK_QUOTA_PREFIX}{quota_key}"
        count = await self._redis.eval(  # type: ignore[misc]  # redis stub sync/async union
            _QUOTA_INCR_SCRIPT, 1, key, str(window_seconds)
        )
        return int(count)


class RedisRefreshTokenStore:
    """Реестр refresh-токенов (allow-list по jti) с детектом повторного использования."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def register(self, jti: str, ttl_seconds: int, user_id: str) -> None:
        """Регистрирует токен и добавляет его в семейство пользователя."""
        await self._redis.set(f"{_REFRESH_PREFIX}{jti}", user_id, ex=ttl_seconds)
        family = f"{_USER_FAMILY_PREFIX}{user_id}"
        await self._redis.sadd(family, jti)  # type: ignore[misc]  # redis stub sync/async union
        await self._redis.expire(family, ttl_seconds)

    async def is_active(self, jti: str) -> bool:
        """Проверяет, что токен не отозван и не истёк."""
        return bool(await self._redis.exists(f"{_REFRESH_PREFIX}{jti}"))

    async def revoke(self, jti: str) -> None:
        """Отзывает токен (logout / ротация)."""
        await self._redis.delete(f"{_REFRESH_PREFIX}{jti}")

    async def mark_rotated(self, jti: str, ttl_seconds: int) -> None:
        """Помечает jti как использованный для ротации (на остаток его TTL)."""
        await self._redis.set(f"{_ROTATED_PREFIX}{jti}", "1", ex=ttl_seconds)

    async def was_rotated(self, jti: str) -> bool:
        """Был ли jti уже ротирован (признак повторного использования)."""
        return bool(await self._redis.exists(f"{_ROTATED_PREFIX}{jti}"))

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Отзывает все refresh-токены пользователя (при детекте кражи)."""
        family = f"{_USER_FAMILY_PREFIX}{user_id}"
        jtis = await self._redis.smembers(family)  # type: ignore[misc]  # redis stub sync/async union
        keys = [f"{_REFRESH_PREFIX}{jti}" for jti in jtis]
        if keys:
            await self._redis.delete(*keys)
        await self._redis.delete(family)
