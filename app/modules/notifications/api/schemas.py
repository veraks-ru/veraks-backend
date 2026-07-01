"""Pydantic-схемы уведомлений."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.modules.notifications.domain.entities import Notification


class NotificationResponse(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    body: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    is_read: bool
    created_at: datetime

    @classmethod
    def from_domain(cls, n: Notification) -> "NotificationResponse":
        return cls(
            id=n.id,
            kind=n.kind,
            title=n.title,
            body=n.body,
            entity_type=n.entity_type,
            entity_id=n.entity_id,
            is_read=n.is_read,
            created_at=n.created_at,
        )
