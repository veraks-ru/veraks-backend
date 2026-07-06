"""In-memory фейки портов b2b для изолированного тестирования use-cases.

Реализуют те же протоколы, что и продакшн-адаптеры, но без Postgres/Redis.
Репозиторий клонирует сущности на входе/выходе, чтобы внешние мутации не
протекали в хранилище.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.modules.b2b.domain.entities import ApiKey
from app.shared.audit.domain.entities import AuditActorType, AuditEntry


class FakeApiKeyRepository:
    """Хранилище API-ключей в памяти."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, ApiKey] = {}

    async def add(self, key: ApiKey) -> ApiKey:
        self._by_id[key.id] = replace(key)
        return replace(key)

    async def get_by_id(self, key_id: uuid.UUID) -> ApiKey | None:
        found = self._by_id.get(key_id)
        return replace(found) if found else None

    async def get_active_by_hash(self, key_hash: str) -> ApiKey | None:
        for key in self._by_id.values():
            if key.key_hash == key_hash and key.is_active:
                return replace(key)
        return None

    async def list_for_owner(self, owner_user_id: uuid.UUID) -> list[ApiKey]:
        return [
            replace(k)
            for k in self._by_id.values()
            if k.owner_user_id == owner_user_id
        ]

    async def update(self, key: ApiKey) -> ApiKey:
        self._by_id[key.id] = replace(key)
        return replace(key)


class FakeKeyGenerator:
    """Детерминированный генератор секретов (для предсказуемых ассертов)."""

    def __init__(self, secret: str = "vk_testsecret000_abc") -> None:
        self._secret = secret

    def generate(self) -> str:
        return self._secret

    def hash(self, plaintext: str) -> str:
        return f"hash:{plaintext}"

    def prefix(self, plaintext: str) -> str:
        return plaintext[:11]


class FakeQuotaCounter:
    """Квота с настраиваемым вердиктом (для проверки fail-closed на use-case)."""

    def __init__(self, *, allowed: bool = True) -> None:
        self._allowed = allowed
        self.calls = 0

    async def check_and_incr(
        self, key_id: uuid.UUID, *, daily_quota: int
    ) -> tuple[bool, int]:
        self.calls += 1
        return self._allowed, self.calls

    async def used_today(self, key_id: uuid.UUID) -> int:
        return self.calls


class FakeAuditTrail:
    """Запоминает записанные действия (без хеш-цепочки)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_type: AuditActorType,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        self.records.append(
            {
                "actor_id": actor_id,
                "actor_type": actor_type,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "after": dict(after) if after else None,
                "metadata": dict(metadata) if metadata else None,
            }
        )
        return AuditEntry(
            occurred_at=datetime.now(timezone.utc),
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            hash="fake",
        )

    def actions(self) -> list[str]:
        """Список зафиксированных action'ов (для ассертов)."""
        return [r["action"] for r in self.records]


class _FakePipeline:
    """Пайплайн Redis в памяти: копит INCR/EXPIRE и применяет их в execute."""

    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, str, int]] = []

    async def __aenter__(self) -> "_FakePipeline":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, key: str) -> "_FakePipeline":
        self._ops.append(("incr", key, 0))
        return self

    def expire(self, key: str, ttl: int) -> "_FakePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> list[object]:
        if self._redis.fail:
            raise RuntimeError("redis down")
        out: list[object] = []
        for name, key, ttl in self._ops:
            if name == "incr":
                self._redis.store[key] = self._redis.store.get(key, 0) + 1
                out.append(self._redis.store[key])
            else:
                self._redis.ttls[key] = ttl
                self._redis.expire_count += 1
                out.append(True)
        return out


class FakeRedis:
    """Минимальный async-Redis: pipeline(INCR/EXPIRE) + get; опция сбоя."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail
        self.expire_count = 0

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    async def get(self, key: str) -> int | None:
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)
