"""DTO прикладного слоя events — нейтральные контракты между API и use-cases.

Намеренно без pydantic: это внутренние структуры, не зависящие от HTTP.
API-схемы (``api/schemas.py``) — отдельные pydantic-модели, которые
транслируются в эти DTO.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.modules.identity.domain.entities import UserRole


@dataclass(frozen=True, slots=True)
class Actor:
    """Кто выполняет операцию (для RBAC и авторства).

    ``role`` — общий с identity RBAC-справочник (shared kernel).
    """

    user_id: uuid.UUID
    role: UserRole


@dataclass(frozen=True, slots=True)
class NewEventInput:
    """Данные для создания черновика события."""

    title: str
    description: str
    category_id: uuid.UUID
    opens_at: datetime
    closes_at: datetime
    resolves_at: datetime
    resolution_source: str
    resolution_criteria: str
    season_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class EventPatchInput:
    """Частичные правки события (``None`` — поле не меняется).

    Окно правится только целиком: либо переданы все три отметки времени,
    либо ни одной (частичная замена окна нарушала бы инвариант порядка дат).
    """

    title: str | None = None
    description: str | None = None
    category_id: uuid.UUID | None = None
    season_id: uuid.UUID | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    resolves_at: datetime | None = None
    resolution_source: str | None = None
    resolution_criteria: str | None = None


@dataclass(frozen=True, slots=True)
class NewCategoryInput:
    """Данные для создания категории."""

    slug: str
    title: str
    description: str = ""
    parent_id: uuid.UUID | None = None
