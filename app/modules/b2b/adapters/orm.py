"""ORM-модель ``api_keys``."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.b2b.domain.entities import ApiKey


class ApiKeyORM(Base):
    """API-ключ B2B-потребителя (хранится хэш секрета)."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    daily_quota: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    def to_domain(self) -> ApiKey:
        return ApiKey(
            id=self.id,
            owner_user_id=self.owner_user_id,
            name=self.name,
            key_prefix=self.key_prefix,
            key_hash=self.key_hash,
            daily_quota=self.daily_quota,
            is_active=self.is_active,
            created_at=self.created_at,
            revoked_at=self.revoked_at,
        )

    @classmethod
    def from_domain(cls, k: ApiKey) -> "ApiKeyORM":
        return cls(
            id=k.id,
            owner_user_id=k.owner_user_id,
            name=k.name,
            key_prefix=k.key_prefix,
            key_hash=k.key_hash,
            daily_quota=k.daily_quota,
            is_active=k.is_active,
            created_at=k.created_at,
            revoked_at=k.revoked_at,
        )
