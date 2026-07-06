"""Redis-адаптеры для OIDC-state и реестра refresh-токенов.

State и refresh-jti — короткоживущие записи с TTL, идеально ложатся на Redis.
В тестах вместо них подставляются in-memory фейки (см. tests/identity/fakes.py).
"""

from __future__ import annotations

from redis.asyncio import Redis

_STATE_PREFIX = "identity:oidc-state:"
_REFRESH_PREFIX = "identity:refresh-jti:"
_ROTATED_PREFIX = "identity:refresh-rotated:"
_USER_FAMILY_PREFIX = "identity:refresh-family:"


class RedisStateStore:
    """Одноразовый ``state`` для анти-CSRF в OIDC-потоке."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def save(self, state: str, ttl_seconds: int) -> None:
        """Сохраняет state с TTL."""
        await self._redis.set(f"{_STATE_PREFIX}{state}", "1", ex=ttl_seconds)

    async def consume(self, state: str) -> bool:
        """Атомарно (DEL возвращает число удалённых) проверяет и гасит state."""
        deleted = await self._redis.delete(f"{_STATE_PREFIX}{state}")
        return bool(deleted)


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
