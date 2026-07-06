"""Порты криптографии и сессий.

``SnilsHasher`` и ``FieldEncryptor`` инкапсулируют требования 152-ФЗ
(хеш для уникальности, шифрование ФИО). ``TokenIssuer`` выпускает/проверяет
JWT-сессии. ``StateStore``/``RefreshTokenStore`` отвечают за CSRF-state и
ротацию/отзыв refresh-токенов.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.identity.application.dto import SessionClaims
from app.modules.identity.domain.value_objects import Snils


@runtime_checkable
class SnilsHasher(Protocol):
    """HMAC-хеширование СНИЛС (детерминированно, для UNIQUE-ключа)."""

    def hash(self, snils: Snils) -> str:
        """Возвращает hex-строку HMAC от нормализованного СНИЛС."""
        ...


@runtime_checkable
class FieldEncryptor(Protocol):
    """Симметричное шифрование чувствительных полей (ФИО)."""

    def encrypt(self, plaintext: str) -> bytes:
        """Шифрует строку."""
        ...

    def decrypt(self, ciphertext: bytes) -> str:
        """Расшифровывает строку."""
        ...


@runtime_checkable
class TokenIssuer(Protocol):
    """Выпуск и верификация JWT access/refresh-токенов."""

    def issue_access(self, claims: SessionClaims) -> str:
        """Короткоживущий access-токен."""
        ...

    def issue_refresh(self, claims: SessionClaims) -> tuple[str, str]:
        """Refresh-токен; возвращает ``(token, jti)`` для отзыва/ротации."""
        ...

    def verify_access(self, token: str) -> SessionClaims:
        """Проверяет access-токен; поднимает ``InvalidTokenError``."""
        ...

    def verify_refresh(self, token: str) -> tuple[SessionClaims, str]:
        """Проверяет refresh-токен; возвращает ``(claims, jti)``."""
        ...


@runtime_checkable
class StateStore(Protocol):
    """Хранилище одноразового OIDC ``state`` (анти-CSRF)."""

    async def save(self, state: str, ttl_seconds: int) -> None:
        """Сохраняет state с TTL."""
        ...

    async def consume(self, state: str) -> bool:
        """Атомарно проверяет и удаляет state; ``True`` если был валиден."""
        ...


@runtime_checkable
class RefreshTokenStore(Protocol):
    """Реестр действительных refresh-токенов с детектом повторного использования.

    Токены пользователя образуют «семейство» (ключ — ``user_id``). Ротация
    помечает старый jti как использованный; повторное предъявление такого jti —
    признак кражи, и все токены пользователя отзываются.
    """

    async def register(self, jti: str, ttl_seconds: int, user_id: str) -> None:
        """Регистрирует выпущенный refresh-токен в семействе пользователя."""
        ...

    async def is_active(self, jti: str) -> bool:
        """Проверяет, что токен не отозван и не истёк."""
        ...

    async def revoke(self, jti: str) -> None:
        """Отзывает токен (logout / ротация)."""
        ...

    async def mark_rotated(self, jti: str, ttl_seconds: int) -> None:
        """Запоминает, что jti был использован для ротации (для детекта повтора)."""
        ...

    async def was_rotated(self, jti: str) -> bool:
        """Был ли jti уже использован для ротации (признак повторного использования)."""
        ...

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Отзывает все refresh-токены пользователя (при детекте кражи)."""
        ...
