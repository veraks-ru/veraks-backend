"""Доменная сущность уведомления пользователю."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Notification:
    """In-app уведомление адресату (напр., автору предложения события)."""

    user_id: uuid.UUID
    kind: str  # напр. "event.approved" / "event.rejected"
    title: str
    body: str = ""
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    is_read: bool = False
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)
