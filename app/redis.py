"""Общий асинхронный Redis-клиент (кэш, брокер, короткоживущие записи)."""

from __future__ import annotations

from redis.asyncio import Redis

from app.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """Ленивая инициализация единого пула подключений к Redis."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis
