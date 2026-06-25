"""Доменная сущность аудита.

``AuditEntry`` — обычный dataclass без I/O. Запись в ``audit_log`` неизменяема:
сущность создаётся адаптером один раз (с уже посчитанным ``hash``) и больше не
меняется. Тип актора отделён от ролей identity, чтобы аудит не зависел от
конкретного домена (маппинг роли → тип актора делает вызывающая сторона).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class AuditActorType(str, enum.Enum):
    """Кто совершил действие. ``SYSTEM`` — фоновые задачи (actor_id = NULL)."""

    USER = "user"
    EDITOR = "editor"
    ARBITER = "arbiter"
    ADMIN = "admin"
    SYSTEM = "system"


@dataclass(slots=True)
class AuditEntry:
    """Одна запись неизменяемого журнала.

    ``before``/``after`` — снимок/диф состояния сущности; ``metadata`` —
    технический контекст (request_id, ip, event_id и т.п.). ``prev_hash`` и
    ``hash`` образуют tamper-evident цепочку. ``id`` (bigserial) присваивается
    БД при вставке.
    """

    occurred_at: datetime
    actor_id: uuid.UUID | None
    actor_type: AuditActorType
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    hash: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    prev_hash: str | None = None
    id: int | None = None
