"""Порт записи в неизменяемый аудит-журнал.

Прикладной слой доменов зависит от этого протокола, а не от реализации.
Реализация сама вычисляет ``occurred_at`` (серверное время), ``prev_hash`` и
``hash`` — вызывающему достаточно описать факт (кто, что, над чем, диф).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.shared.audit.domain.entities import AuditActorType, AuditEntry


@runtime_checkable
class AuditTrail(Protocol):
    """Добавление записи в append-only журнал с хеш-цепочкой."""

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
        """Записывает факт изменения состояния и возвращает сохранённое звено."""
        ...
