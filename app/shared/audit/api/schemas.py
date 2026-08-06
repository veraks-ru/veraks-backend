"""Pydantic-схемы запросов/ответов эндпоинтов аудита."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.shared.audit.application.verify_chain import ChainVerificationResult
from app.shared.audit.domain.entities import AuditActorType, AuditEntry


class AuditLogEntryResponse(BaseModel):
    """Запись аудита как есть — без трансформации payload'а."""

    id: int
    occurred_at: datetime
    actor_id: uuid.UUID | None
    actor_type: AuditActorType
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    metadata: dict[str, Any]
    prev_hash: str | None
    hash: str

    @classmethod
    def from_domain(cls, entry: AuditEntry) -> AuditLogEntryResponse:
        assert entry.id is not None  # прочитанная из БД запись всегда с id
        return cls(
            id=entry.id,
            occurred_at=entry.occurred_at,
            actor_id=entry.actor_id,
            actor_type=entry.actor_type,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            before=entry.before,
            after=entry.after,
            metadata=entry.metadata,
            prev_hash=entry.prev_hash,
            hash=entry.hash,
        )


class AuditLogPageResponse(BaseModel):
    """Страница журнала аудита («показать ещё» — по ``has_more``/``before_id``)."""

    items: list[AuditLogEntryResponse]
    has_more: bool


class ChainVerificationResponse(BaseModel):
    """Итог верификации хеш-цепочки."""

    ok: bool
    checked: int
    first_broken_id: int | None = None

    @classmethod
    def from_result(cls, result: ChainVerificationResult) -> ChainVerificationResponse:
        return cls(
            ok=result.ok, checked=result.checked, first_broken_id=result.first_broken_id
        )
