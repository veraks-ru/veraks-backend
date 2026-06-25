"""DTO прикладного слоя — нейтральные контракты между портами и use-cases.

Намеренно не используют pydantic: это внутренние структуры, не зависящие
от HTTP. API-схемы (api/schemas.py) — отдельные pydantic-модели.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.modules.identity.domain.entities import UserRole


@dataclass(frozen=True, slots=True)
class SessionClaims:
    """Полезная нагрузка сессионного токена."""

    user_id: uuid.UUID
    role: UserRole


@dataclass(frozen=True, slots=True)
class AuthorizationRedirect:
    """Результат инициации логина: куда отправить пользователя."""

    authorization_url: str
    state: str


@dataclass(frozen=True, slots=True)
class SessionTokens:
    """Пара токенов сессии, выдаваемая клиенту (refresh — в httpOnly cookie)."""

    access_token: str
    refresh_token: str
    access_ttl_seconds: int
    refresh_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Итог завершённого логина: токены + признак, создан ли аккаунт."""

    user_id: uuid.UUID
    tokens: SessionTokens
    is_new_user: bool
