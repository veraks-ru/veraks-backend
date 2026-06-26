"""ORM-модели events (SQLAlchemy 2.0).

Маппятся на доменные сущности в обе стороны через явные ``to_domain`` /
``from_domain``, чтобы домен оставался свободным от инфраструктуры.
Денормализованное окно события хранится тремя колонками, а в домене —
единым value-object :class:`EventWindow`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum as SAEnum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import CITEXT, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.events.domain.entities import Category, Event, EventStatus
from app.modules.events.domain.value_objects import EventWindow

_status_enum = SAEnum(
    EventStatus,
    name="event_status",
    values_callable=lambda enum: [member.value for member in enum],
)


class CategoryORM(Base):
    """Таблица ``categories`` — дерево рубрик событий."""

    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id"),
        nullable=True,
        index=True,
    )

    def to_domain(self) -> Category:
        """ORM → доменная сущность."""
        return Category(
            id=self.id,
            slug=self.slug,
            title=self.title,
            description=self.description,
            parent_id=self.parent_id,
        )

    @classmethod
    def from_domain(cls, category: Category) -> CategoryORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=category.id,
            slug=category.slug,
            title=category.title,
            description=category.description,
            parent_id=category.parent_id,
        )


class EventORM(Base):
    """Таблица ``events`` — прогнозируемые события (бинарный исход в MVP)."""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    season_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("seasons.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[EventStatus] = mapped_column(
        _status_enum, nullable=False, index=True
    )
    opens_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    closes_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, index=True
    )
    resolves_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, index=True
    )
    resolution_source: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    dispute_window_ends_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> Event:
        """ORM → доменная сущность (с восстановлением окна-VO)."""
        return Event(
            id=self.id,
            title=self.title,
            description=self.description,
            category_id=self.category_id,
            created_by=self.created_by,
            window=EventWindow(
                opens_at=self.opens_at,
                closes_at=self.closes_at,
                resolves_at=self.resolves_at,
            ),
            resolution_source=self.resolution_source,
            resolution_criteria=self.resolution_criteria,
            season_id=self.season_id,
            status=self.status,
            outcome=self.outcome,
            resolved_at=self.resolved_at,
            dispute_window_ends_at=self.dispute_window_ends_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, event: Event) -> EventORM:
        """Доменная сущность → новая ORM-строка (окно разворачивается в колонки)."""
        return cls(
            id=event.id,
            title=event.title,
            description=event.description,
            category_id=event.category_id,
            created_by=event.created_by,
            season_id=event.season_id,
            status=event.status,
            opens_at=event.window.opens_at,
            closes_at=event.window.closes_at,
            resolves_at=event.window.resolves_at,
            resolution_source=event.resolution_source,
            resolution_criteria=event.resolution_criteria,
            outcome=event.outcome,
            resolved_at=event.resolved_at,
            dispute_window_ends_at=event.dispute_window_ends_at,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )
