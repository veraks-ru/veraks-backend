"""ORM-модель пользователя (SQLAlchemy 2.0).

Маппится на доменную сущность ``User`` в обе стороны через явные функции,
чтобы домен оставался свободным от инфраструктуры.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import CITEXT, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.identity.domain.entities import User, UserRole, UserStatus

_role_enum = SAEnum(
    UserRole,
    name="user_role",
    values_callable=lambda enum: [member.value for member in enum],
)
_status_enum = SAEnum(
    UserStatus,
    name="user_status",
    values_callable=lambda enum: [member.value for member in enum],
)


class UserORM(Base):
    """Таблица ``users`` — аккаунты, привязанные к гражданам (ЕСИА/СНИЛС)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    esia_oid: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    snils_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    username: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    real_name_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    role: Mapped[UserRole] = mapped_column(_role_enum, nullable=False)
    status: Mapped[UserStatus] = mapped_column(_status_enum, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    def to_domain(self) -> User:
        """ORM → доменная сущность."""
        return User(
            id=self.id,
            esia_oid=self.esia_oid,
            snils_hash=self.snils_hash,
            username=self.username,
            display_name=self.display_name,
            real_name_enc=self.real_name_enc,
            role=self.role,
            status=self.status,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, user: User) -> UserORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=user.id,
            esia_oid=user.esia_oid,
            snils_hash=user.snils_hash,
            username=user.username,
            display_name=user.display_name,
            real_name_enc=user.real_name_enc,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
        )
