"""In-memory фейки портов identity для изолированного тестирования.

Фейки реализуют те же протоколы, что и продакшн-адаптеры, но без I/O —
это позволяет юнит-тестировать use-cases и интеграционно гонять эндпоинты
без Postgres, Redis и сети к ЕСИА.
"""

from __future__ import annotations

import uuid

from app.modules.identity.domain.entities import User
from app.modules.identity.domain.value_objects import EsiaIdentity, EsiaTokens
from app.modules.identity.ports.repositories import (
    SnilsAlreadyExistsError,
    UsernameTakenError,
)


class InMemoryUserRepository:
    """Хранилище пользователей в памяти с эмуляцией UNIQUE-ограничений."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, User] = {}

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._clone(self._by_id.get(user_id))

    async def get_by_snils_hash(self, snils_hash: str) -> User | None:
        for user in self._by_id.values():
            if user.snils_hash == snils_hash:
                return self._clone(user)
        return None

    async def get_by_esia_oid(self, esia_oid: str) -> User | None:
        for user in self._by_id.values():
            if user.esia_oid == esia_oid:
                return self._clone(user)
        return None

    async def get_by_username(self, username: str) -> User | None:
        for user in self._by_id.values():
            if user.username.lower() == username.lower():
                return self._clone(user)
        return None

    async def username_exists(self, username: str) -> bool:
        return any(u.username.lower() == username.lower() for u in self._by_id.values())

    async def add(self, user: User) -> User:
        for existing in self._by_id.values():
            if existing.snils_hash == user.snils_hash:
                raise SnilsAlreadyExistsError(user.snils_hash)
            if existing.username.lower() == user.username.lower():
                raise UsernameTakenError(user.username)
        self._by_id[user.id] = self._clone(user)
        return self._clone(user)

    async def update(self, user: User) -> User:
        self._by_id[user.id] = self._clone(user)
        return self._clone(user)

    @staticmethod
    def _clone(user: User | None) -> User | None:
        """Возвращает копию, чтобы внешние мутации не текли в хранилище."""
        if user is None:
            return None
        return User(
            id=user.id,
            esia_oid=user.esia_oid,
            snils_hash=user.snils_hash,
            username=user.username,
            display_name=user.display_name,
            real_name_enc=user.real_name_enc,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
        )


class FakeEsiaGateway:
    """Шлюз ЕСИА, возвращающий заранее заданную личность."""

    def __init__(self, identity: EsiaIdentity) -> None:
        self.identity = identity
        self.build_calls: list[str] = []

    def build_authorization_url(self, *, state: str) -> str:
        self.build_calls.append(state)
        return f"https://esia.example/authorize?state={state}"

    async def exchange_code(self, *, code: str) -> EsiaTokens:
        return EsiaTokens(access_token=f"access-for-{code}", id_token="id")

    async def fetch_identity(self, tokens: EsiaTokens) -> EsiaIdentity:
        return self.identity


class FakeStateStore:
    """Множество выпущенных одноразовых state."""

    def __init__(self) -> None:
        self._states: set[str] = set()

    async def save(self, state: str, ttl_seconds: int) -> None:
        self._states.add(state)

    async def consume(self, state: str) -> bool:
        if state in self._states:
            self._states.discard(state)
            return True
        return False

    def seed(self, state: str) -> None:
        """Тестовый помощник: заранее положить валидный state."""
        self._states.add(state)


class FakeRefreshTokenStore:
    """Allow-list refresh-jti с детектом повторного использования (семейство = user_id)."""

    def __init__(self) -> None:
        self._active: dict[str, str] = {}  # jti -> user_id
        self._rotated: set[str] = set()
        self._family: dict[str, set[str]] = {}  # user_id -> {jti}

    async def register(self, jti: str, ttl_seconds: int, user_id: str) -> None:
        self._active[jti] = user_id
        self._family.setdefault(user_id, set()).add(jti)

    async def is_active(self, jti: str) -> bool:
        return jti in self._active

    async def revoke(self, jti: str) -> None:
        self._active.pop(jti, None)

    async def mark_rotated(self, jti: str, ttl_seconds: int) -> None:
        self._rotated.add(jti)

    async def was_rotated(self, jti: str) -> bool:
        return jti in self._rotated

    async def revoke_all_for_user(self, user_id: str) -> None:
        for jti in self._family.pop(user_id, set()):
            self._active.pop(jti, None)
