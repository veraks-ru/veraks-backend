"""DTO прикладного слоя resolutions (frozen dataclass'ы, не pydantic).

``Actor`` — нейтральный носитель «кто и с какой ролью» (из аутентификации).
``EventLifecycle`` — срез статуса события, который шлюз events отдаёт
прикладному слою, чтобы тот не зависел от сущности ``Event`` целиком.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.modules.events.domain.entities import EventStatus
from app.modules.identity.domain.entities import UserRole


@dataclass(frozen=True, slots=True)
class Actor:
    """Актор операции: идентификатор пользователя и его роль (RBAC/SoD)."""

    user_id: uuid.UUID
    role: UserRole


@dataclass(frozen=True, slots=True)
class EventLifecycle:
    """Срез жизненного цикла события для прикладного слоя resolutions."""

    event_id: uuid.UUID
    status: EventStatus
    outcome: bool | None
    dispute_window_ends_at: datetime | None
    season_id: uuid.UUID | None
