"""Redis-адаптеры для OIDC-state и реестра refresh-токенов.

State и refresh-jti — короткоживущие записи с TTL, идеально ложатся на Redis.
В тестах вместо них подставляются in-memory фейки (см. tests/identity/fakes.py).
"""

from __future__ import annotations

import json

from redis.asyncio import Redis

from app.modules.identity.application.dto import OidcFlowState

_STATE_PREFIX = "identity:oidc-state:"
_REFRESH_PREFIX = "identity:refresh-jti:"
_ROTATED_PREFIX = "identity:refresh-rotated:"
_USER_FAMILY_PREFIX = "identity:refresh-family:"


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
