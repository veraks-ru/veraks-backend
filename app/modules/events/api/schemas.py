"""Pydantic-схемы запросов/ответов эндпоинтов events.

Контракт HTTP-слоя, отделённый от доменных сущностей и DTO: изменения
формата API не протекают внутрь домена. Схемы транслируются в прикладные
DTO (``application/dto.py``) и обратно из доменных сущностей.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.events.application.dto import (
    EventPatchInput,
    NewCategoryInput,
    NewEventInput,
)
from app.modules.events.domain.entities import Category, Event, EventStatus


class CreateEventRequest(BaseModel):
    """Тело запроса создания события (редакция)."""

    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1)
    category_id: uuid.UUID
    season_id: uuid.UUID | None = None
    opens_at: datetime
    closes_at: datetime
    resolves_at: datetime
    resolution_source: str = Field(min_length=1, description="Заранее заданный источник истины")
    resolution_criteria: str = Field(min_length=1, description="Точные критерии засчитывания")

    def to_input(self) -> NewEventInput:
        """Трансляция в прикладной DTO."""
        return NewEventInput(
            title=self.title,
            description=self.description,
            category_id=self.category_id,
            season_id=self.season_id,
            opens_at=self.opens_at,
            closes_at=self.closes_at,
            resolves_at=self.resolves_at,
            resolution_source=self.resolution_source,
            resolution_criteria=self.resolution_criteria,
        )


class UpdateEventRequest(BaseModel):
    """Тело запроса частичного редактирования (все поля опциональны)."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, min_length=1)
    category_id: uuid.UUID | None = None
    season_id: uuid.UUID | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    resolves_at: datetime | None = None
    resolution_source: str | None = Field(default=None, min_length=1)
    resolution_criteria: str | None = Field(default=None, min_length=1)

    def to_input(self) -> EventPatchInput:
        """Трансляция в прикладной DTO патча."""
        return EventPatchInput(
            title=self.title,
            description=self.description,
            category_id=self.category_id,
            season_id=self.season_id,
            opens_at=self.opens_at,
            closes_at=self.closes_at,
            resolves_at=self.resolves_at,
            resolution_source=self.resolution_source,
            resolution_criteria=self.resolution_criteria,
        )


class EventResponse(BaseModel):
    """Полная проекция события."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    category_id: uuid.UUID
    created_by: uuid.UUID
    season_id: uuid.UUID | None
    status: EventStatus
    opens_at: datetime
    closes_at: datetime
    resolves_at: datetime
    resolution_source: str
    resolution_criteria: str
    outcome: bool | None
    resolved_at: datetime | None
    dispute_window_ends_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, event: Event) -> EventResponse:
        """Маппинг доменной сущности в ответ (окно-VO разворачивается)."""
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


class CreateCategoryRequest(BaseModel):
    """Тело запроса создания категории."""

    slug: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    parent_id: uuid.UUID | None = None

    def to_input(self) -> NewCategoryInput:
        """Трансляция в прикладной DTO."""
        return NewCategoryInput(
            slug=self.slug,
            title=self.title,
            description=self.description,
            parent_id=self.parent_id,
        )


class CategoryResponse(BaseModel):
    """Проекция категории."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    description: str
    parent_id: uuid.UUID | None

    @classmethod
    def from_domain(cls, category: Category) -> CategoryResponse:
        """Маппинг доменной сущности категории в ответ."""
        return cls(
            id=category.id,
            slug=category.slug,
            title=category.title,
            description=category.description,
            parent_id=category.parent_id,
        )
