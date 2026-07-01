"""ORM-модель ``notifications``."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.notifications.domain.entities import Notification


class NotificationORM(Base):
    """In-app уведомления пользователю."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    def to_domain(self) -> Notification:
        return Notification(
            id=self.id,
            user_id=self.user_id,
            kind=self.kind,
            title=self.title,
            body=self.body,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            is_read=self.is_read,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, n: Notification) -> "NotificationORM":
        return cls(
            id=n.id,
            user_id=n.user_id,
            kind=n.kind,
            title=n.title,
            body=n.body,
            entity_type=n.entity_type,
            entity_id=n.entity_id,
            is_read=n.is_read,
            created_at=n.created_at,
        )
