"""Redis-адаптеры для OIDC-state и реестра refresh-токенов.

State и refresh-jti — короткоживущие записи с TTL, идеально ложатся на Redis.
В тестах вместо них подставляются in-memory фейки (см. tests/identity/fakes.py).
"""

from __future__ import annotations

from redis.asyncio import Redis

_STATE_PREFIX = "identity:oidc-state:"
_REFRESH_PREFIX = "identity:refresh-jti:"


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
    """Реестр действительных refresh-токенов (allow-list по jti)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def register(self, jti: str, ttl_seconds: int) -> None:
        """Регистрирует выпущенный refresh-токен."""
        await self._redis.set(f"{_REFRESH_PREFIX}{jti}", "1", ex=ttl_seconds)

    async def is_active(self, jti: str) -> bool:
        """Проверяет, что токен не отозван и не истёк."""
        return bool(await self._redis.exists(f"{_REFRESH_PREFIX}{jti}"))

    async def revoke(self, jti: str) -> None:
        """Отзывает токен (logout / ротация)."""
        await self._redis.delete(f"{_REFRESH_PREFIX}{jti}")
