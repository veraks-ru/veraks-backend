"""ORM-модели соцфич: ``comments`` и ``follows``."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.social.domain.entities import Comment, Follow


class CommentORM(Base):
    """Комментарий к событию (мягкое удаление через ``deleted_at``)."""

    __tablename__ = "comments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    def to_domain(self) -> Comment:
        return Comment(
            id=self.id,
            event_id=self.event_id,
            author_id=self.author_id,
            body=self.body,
            created_at=self.created_at,
            deleted_at=self.deleted_at,
        )

    @classmethod
    def from_domain(cls, c: Comment) -> "CommentORM":
        return cls(
            id=c.id,
            event_id=c.event_id,
            author_id=c.author_id,
            body=c.body,
            created_at=c.created_at,
            deleted_at=c.deleted_at,
        )


class FollowORM(Base):
    """Подписка follower→followee (уникальная пара)."""

    __tablename__ = "follows"
    __table_args__ = (
        UniqueConstraint("follower_id", "followee_id", name="uq_follows_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    follower_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    followee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> Follow:
        return Follow(
            id=self.id,
            follower_id=self.follower_id,
            followee_id=self.followee_id,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, f: Follow) -> "FollowORM":
        return cls(
            id=f.id,
            follower_id=f.follower_id,
            followee_id=f.followee_id,
            created_at=f.created_at,
        )
