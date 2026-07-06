"""Use-cases B2B: управление ключами, аутентификация ключа, чтение сигналов."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.modules.b2b.application.dto import (
    ConsensusSignal,
    EventSignal,
    LeaderboardSignalRow,
)
from app.modules.b2b.domain.entities import ApiKey
from app.modules.b2b.domain.errors import (
    ApiKeyNotFoundError,
    InvalidApiKeyError,
    QuotaExceededError,
    SignalTargetNotFoundError,
)
from app.modules.b2b.ports.repositories import (
    ApiKeyRepository,
    KeyGenerator,
    QuotaCounter,
    SignalGateway,
)
from app.shared.audit.domain.entities import AuditActorType
from app.shared.audit.ports.audit_trail import AuditTrail


# ── Управление ключами ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """Результат выдачи: сущность ключа + полный секрет (показывается один раз)."""

    key: ApiKey
    plaintext: str


class IssueApiKey:
    """Выдать API-ключ B2B-потребителю (операция администратора).

    Выдача ключа — не денежное событие: проводки выручки здесь НЕ делается
    (выручка B2B проводится только по факту оплаты — вебхуком провайдера, вне
    этого use-case). Факт выдачи фиксируется в неизменяемом аудите.
    """

    def __init__(
        self,
        *,
        keys: ApiKeyRepository,
        generator: KeyGenerator,
        audit: AuditTrail,
        default_quota: int,
    ) -> None:
        self._keys = keys
        self._generator = generator
        self._audit = audit
        self._default_quota = default_quota

    async def execute(
        self,
        *,
        owner_user_id: uuid.UUID,
        name: str,
        daily_quota: int | None = None,
    ) -> IssuedApiKey:
        plaintext = self._generator.generate()
        key = ApiKey.issue(
            owner_user_id=owner_user_id,
            name=name,
            key_prefix=self._generator.prefix(plaintext),
            key_hash=self._generator.hash(plaintext),
            daily_quota=daily_quota or self._default_quota,
        )
        saved = await self._keys.add(key)
        # Выдачу ключа выполняет администратор (гард роутера), поэтому actor —
        # владелец/админ, actor_type — ADMIN.
        await self._audit.record(
            actor_id=owner_user_id,
            actor_type=AuditActorType.ADMIN,
            action="b2b.key.issued",
            entity_type="api_key",
            entity_id=saved.id,
            after={
                "name": saved.name,
                "key_prefix": saved.key_prefix,
                "daily_quota": saved.daily_quota,
                "is_active": saved.is_active,
            },
            metadata={"owner_user_id": str(owner_user_id)},
        )
        return IssuedApiKey(key=saved, plaintext=plaintext)


class ListMyApiKeys:
    """Ключи владельца (без секрета — только префикс)."""

    def __init__(self, *, keys: ApiKeyRepository) -> None:
        self._keys = keys

    async def execute(self, *, owner_user_id: uuid.UUID) -> list[ApiKey]:
        return await self._keys.list_for_owner(owner_user_id)


class RevokeApiKey:
    """Отозвать свой ключ (идемпотентно) с записью в аудит."""

    def __init__(self, *, keys: ApiKeyRepository, audit: AuditTrail) -> None:
        self._keys = keys
        self._audit = audit

    async def execute(
        self, *, owner_user_id: uuid.UUID, key_id: uuid.UUID
    ) -> None:
        key = await self._keys.get_by_id(key_id)
        if key is None or key.owner_user_id != owner_user_id:
            raise ApiKeyNotFoundError("Ключ не найден")
        if key.is_active:
            key.revoke()
            await self._keys.update(key)
            # Аудит пишется только при реальном изменении состояния (первый
            # отзыв); повторный вызов идемпотентен и ничего не логирует.
            await self._audit.record(
                actor_id=owner_user_id,
                actor_type=AuditActorType.ADMIN,
                action="b2b.key.revoked",
                entity_type="api_key",
                entity_id=key.id,
                before={"is_active": True},
                after={"is_active": False},
                metadata={"owner_user_id": str(owner_user_id)},
            )


@dataclass(frozen=True, slots=True)
class ApiKeyUsage:
    key: ApiKey
    used_today: int


class GetKeyUsage:
    """Расход суточной квоты по ключу владельца."""

    def __init__(
        self, *, keys: ApiKeyRepository, quota: QuotaCounter
    ) -> None:
        self._keys = keys
        self._quota = quota

    async def execute(
        self, *, owner_user_id: uuid.UUID, key_id: uuid.UUID
    ) -> ApiKeyUsage:
        key = await self._keys.get_by_id(key_id)
        if key is None or key.owner_user_id != owner_user_id:
            raise ApiKeyNotFoundError("Ключ не найден")
        return ApiKeyUsage(key=key, used_today=await self._quota.used_today(key_id))


# ── Аутентификация ключа (для API-шлюза сигналов) ────────────────────────────


class AuthenticateApiKey:
    """Проверить предъявленный ключ и списать одну единицу квоты."""

    def __init__(
        self,
        *,
        keys: ApiKeyRepository,
        generator: KeyGenerator,
        quota: QuotaCounter,
    ) -> None:
        self._keys = keys
        self._generator = generator
        self._quota = quota

    async def execute(self, *, plaintext: str) -> ApiKey:
        key = await self._keys.get_active_by_hash(
            self._generator.hash(plaintext)
        )
        if key is None:
            raise InvalidApiKeyError("Неизвестный или отозванный ключ")
        allowed, _used = await self._quota.check_and_incr(
            key.id, daily_quota=key.daily_quota
        )
        if not allowed:
            raise QuotaExceededError("Исчерпана суточная квота ключа")
        return key


# ── Сигналы ──────────────────────────────────────────────────────────────────


class GetConsensusSignal:
    def __init__(self, *, gateway: SignalGateway) -> None:
        self._gateway = gateway

    async def execute(self, *, event_id: uuid.UUID) -> ConsensusSignal:
        signal = await self._gateway.consensus(event_id)
        if signal is None:
            raise SignalTargetNotFoundError("Событие не найдено")
        return signal


class GetLeaderboardSignal:
    def __init__(self, *, gateway: SignalGateway) -> None:
        self._gateway = gateway

    async def execute(
        self, *, scope: str, scope_id: uuid.UUID | None, limit: int
    ) -> list[LeaderboardSignalRow]:
        return await self._gateway.leaderboard(
            scope=scope, scope_id=scope_id, limit=limit
        )


class ListEventSignals:
    def __init__(self, *, gateway: SignalGateway) -> None:
        self._gateway = gateway

    async def execute(
        self, *, status: str | None, limit: int
    ) -> list[EventSignal]:
        return await self._gateway.events(status=status, limit=limit)
