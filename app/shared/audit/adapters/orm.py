"""ORM-модель ``audit_log`` (SQLAlchemy 2.0).

``id`` — ``bigserial`` (логи, в отличие от доменных сущностей на uuid).
Колонка БД ``metadata`` маппится на атрибут ``meta``: имя ``metadata``
зарезервировано в декларативной базе SQLAlchemy. Неизменяемость гарантируется
схемным триггером (см. миграцию ``0008``), а не ORM.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Enum as SAEnum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.shared.audit.domain.entities import AuditActorType, AuditEntry

_actor_type_enum = SAEnum(
    AuditActorType,
    name="audit_actor_type",
    values_callable=lambda enum: [member.value for member in enum],
)


class AuditLogORM(Base):
    """Таблица ``audit_log`` — единый неизменяемый журнал значимых действий."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    actor_type: Mapped[AuditActorType] = mapped_column(_actor_type_enum, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Колонка ``metadata`` → атрибут ``meta`` (``metadata`` занято в Base).
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    prev_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    hash: Mapped[str] = mapped_column(Text, nullable=False)

    def to_domain(self) -> AuditEntry:
        """ORM → доменная запись аудита."""
        return AuditEntry(
            id=self.id,
            occurred_at=self.occurred_at,
            actor_id=self.actor_id,
            actor_type=self.actor_type,
            action=self.action,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            before=self.before,
            after=self.after,
            metadata=self.meta,
            prev_hash=self.prev_hash,
            hash=self.hash,
        )
