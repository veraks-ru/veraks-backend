"""Общие строительные блоки обоих потоков входа (ЕСИА и ссылка на email).

Раньше выпуск сессии и подбор псевдонима были приватными методами
``CompleteEsiaLogin``. С появлением второго способа входа их пришлось бы
копировать — а сессия, выпущенная «почти так же», это не деталь: разъехавшиеся
TTL или незарегистрированный в реестре refresh-jti ломают отзыв токенов ровно
для одного из потоков и незаметно. Поэтому здесь — единственная реализация,
которую используют оба use-case'а.
"""

from __future__ import annotations

import secrets

from app.modules.identity.application.dto import SessionClaims, SessionTokens
from app.modules.identity.domain.entities import User, generate_username_seed
from app.modules.identity.ports.repositories import UserRepository
from app.modules.identity.ports.security import RefreshTokenStore, TokenIssuer

_MAX_USERNAME_ATTEMPTS = 1000


class SessionIssuer:
    """Выпускает пару access/refresh и регистрирует refresh для отзыва."""

    def __init__(
        self,
        *,
        tokens: TokenIssuer,
        refresh_store: RefreshTokenStore,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> None:
        self._tokens = tokens
        self._refresh_store = refresh_store
        self._access_ttl = access_ttl_seconds
        self._refresh_ttl = refresh_ttl_seconds

    async def issue(self, user: User) -> SessionTokens:
        """Сессия пользователя: access + refresh, refresh — в реестре семейства."""
        claims = SessionClaims(user_id=user.id, role=user.role)
        access = self._tokens.issue_access(claims)
        refresh, jti = self._tokens.issue_refresh(claims)
        await self._refresh_store.register(jti, self._refresh_ttl, str(user.id))
        return SessionTokens(
            access_token=access,
            refresh_token=refresh,
            access_ttl_seconds=self._access_ttl,
            refresh_ttl_seconds=self._refresh_ttl,
        )


async def allocate_username(users: UserRepository) -> str:
    """Подбирает свободный псевдонимный хэндл (seed + суффикс при коллизии).

    Проверка занятости здесь — оптимизация, а не гарантия: между ней и INSERT
    хэндл может занять параллельная регистрация. Настоящая гарантия —
    ``UNIQUE(username)``, а вызывающий переаллоцирует хэндл, поймав нарушение.
    """
    seed = generate_username_seed()
    if not await users.username_exists(seed):
        return seed
    for suffix in range(1, _MAX_USERNAME_ATTEMPTS):
        candidate = f"{seed}{suffix}"
        if not await users.username_exists(candidate):
            return candidate
    # Крайне маловероятно: добавляем случайный хвост.
    return f"{seed}{secrets.token_hex(4)}"
