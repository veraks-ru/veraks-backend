"""Порты домена b2b: хранилище ключей, квота, генератор, выручка, сигналы."""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.modules.b2b.application.dto import (
    ConsensusSignal,
    EventSignal,
    LeaderboardSignalRow,
)
from app.modules.b2b.domain.entities import ApiKey


class ApiKeyRepository(Protocol):
    async def add(self, key: ApiKey) -> ApiKey: ...
    async def get_by_id(self, key_id: uuid.UUID) -> ApiKey | None: ...
    async def get_active_by_hash(self, key_hash: str) -> ApiKey | None: ...
    async def list_for_owner(self, owner_user_id: uuid.UUID) -> list[ApiKey]: ...
    async def update(self, key: ApiKey) -> ApiKey: ...


@runtime_checkable
class KeyGenerator(Protocol):
    def generate(self) -> str:
        """Секрет ключа (полный, показывается один раз)."""
        ...

    def hash(self, plaintext: str) -> str:
        """Необратимый хэш секрета для хранения/поиска."""
        ...

    def prefix(self, plaintext: str) -> str:
        """Короткий префикс секрета для узнавания в списке."""
        ...


@runtime_checkable
class QuotaCounter(Protocol):
    async def check_and_incr(
        self, key_id: uuid.UUID, *, daily_quota: int
    ) -> tuple[bool, int]:
        """Инкремент суточного счётчика; ``(в пределах квоты, текущее число)``."""
        ...

    async def used_today(self, key_id: uuid.UUID) -> int:
        """Сколько запросов израсходовано за сегодня (без инкремента)."""
        ...


@runtime_checkable
class SignalGateway(Protocol):
    async def consensus(self, event_id: uuid.UUID) -> ConsensusSignal | None: ...
    async def leaderboard(
        self, *, scope: str, scope_id: uuid.UUID | None, limit: int
    ) -> list[LeaderboardSignalRow]: ...
    async def events(self, *, status: str | None, limit: int) -> list[EventSignal]: ...
