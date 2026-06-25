"""Порт репозитория пользователей.

Бизнес-логика зависит от этого протокола, а не от SQLAlchemy. Реализация —
в adapters/repository.py; в тестах подставляется in-memory фейк.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.modules.identity.domain.entities import User


@runtime_checkable
class UserRepository(Protocol):
    """Хранилище аккаунтов."""

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """Аккаунт по PK или ``None``."""
        ...

    async def get_by_snils_hash(self, snils_hash: str) -> User | None:
        """Аккаунт по HMAC-хешу СНИЛС (ключ инварианта «1 человек = 1 аккаунт»)."""
        ...

    async def get_by_esia_oid(self, esia_oid: str) -> User | None:
        """Аккаунт по стабильному идентификатору ЕСИА."""
        ...

    async def get_by_username(self, username: str) -> User | None:
        """Аккаунт по публичному хэндлу (citext, регистронезависимо) или ``None``."""
        ...

    async def username_exists(self, username: str) -> bool:
        """Проверка занятости публичного хэндла."""
        ...

    async def add(self, user: User) -> User:
        """Сохраняет новый аккаунт.

        Поднимает :class:`UsernameTakenError` или :class:`SnilsAlreadyExistsError`
        при нарушении уникальности (гонка параллельных регистраций).
        """
        ...

    async def update(self, user: User) -> User:
        """Сохраняет изменения существующего аккаунта."""
        ...


class UsernameTakenError(Exception):
    """Нарушение ``UNIQUE(username)`` при вставке."""


class SnilsAlreadyExistsError(Exception):
    """Нарушение ``UNIQUE(snils_hash)`` — параллельная регистрация того же гражданина."""
