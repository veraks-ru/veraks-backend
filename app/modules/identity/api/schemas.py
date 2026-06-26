"""Pydantic-схемы запросов/ответов для эндпоинтов identity.

Это контракт HTTP-слоя; он отделён от доменных сущностей и DTO, чтобы
изменения формата API не протекали внутрь домена.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.identity.domain.entities import User, UserRole, UserStatus


class CallbackRequest(BaseModel):
    """Параметры callback'а ЕСИА (query-string)."""

    code: str = Field(min_length=1, description="Authorization code от ЕСИА")
    state: str = Field(min_length=1, description="Анти-CSRF state из шага login")


class AccessTokenResponse(BaseModel):
    """Тело ответа с access-токеном (refresh уходит в httpOnly cookie)."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MeResponse(BaseModel):
    """Публичная проекция текущего пользователя (без ПДн)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    display_name: str
    role: UserRole
    status: UserStatus

    @classmethod
    def from_domain(cls, user: User) -> MeResponse:
        """Маппинг доменной сущности в ответ (ФИО намеренно не отдаём)."""
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
        )


class PublicProfileResponse(BaseModel):
    """Публичный профиль по хэндлу (псевдоним; ПДн/ФИО не отдаются)."""

    username: str
    display_name: str
    member_since: datetime

    @classmethod
    def from_domain(cls, user: User) -> PublicProfileResponse:
        return cls(
            username=user.username,
            display_name=user.display_name,
            member_since=user.created_at,
        )


class PublicUserRef(BaseModel):
    """Минимальная публичная ссылка на пользователя (для лидербордов)."""

    user_id: uuid.UUID
    username: str
    display_name: str


class UpdateProfileRequest(BaseModel):
    """Изменение собственного профиля. Поля опциональны (partial update)."""

    display_name: str | None = Field(
        default=None, min_length=1, max_length=100, description="Отображаемое имя"
    )
