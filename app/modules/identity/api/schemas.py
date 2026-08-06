"""Pydantic-схемы запросов/ответов для эндпоинтов identity.

Это контракт HTTP-слоя; он отделён от доменных сущностей и DTO, чтобы
изменения формата API не протекали внутрь домена.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.identity.domain.consent import Consent, ConsentDocument
from app.modules.identity.domain.entities import User, UserRole, UserStatus

# Публичный хэндл: латиница/цифры/дефис, 3-32 символа, без дефиса по краям.
_USERNAME_PATTERN = r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$"


class CallbackRequest(BaseModel):
    """Параметры callback'а ЕСИА (query-string).

    Все поля опциональны, потому что OIDC-провайдер возвращает ЛИБО
    ``code`` + ``state``, ЛИБО ``error`` + ``state`` (отказ пользователя,
    сбой на стороне Госуслуг). Раньше ``code`` был обязателен, и отказ
    превращался в 422 с невнятным «Не передан код» — теперь роутер сам
    разбирает оба случая и отдаёт человеческую ошибку.
    """

    code: str | None = Field(default=None, description="Authorization code от ЕСИА")
    state: str | None = Field(
        default=None, description="Анти-CSRF state из шага login"
    )
    error: str | None = Field(
        default=None,
        max_length=64,
        description="Код ошибки OIDC (например, access_denied)",
    )
    error_description: str | None = Field(
        default=None, max_length=500, description="Пояснение провайдера"
    )


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


class MissingConsentSchema(BaseModel):
    """Обязательный документ, на актуальную версию которого нет согласия."""

    document: str
    version: str


class AuthMeResponse(MeResponse):
    """Текущий пользователь + статус онбординга (152-ФЗ) для ``GET /auth/me``.

    ``needs_onboarding`` — онбординг не пройден ИЛИ есть недостающие
    обязательные согласия (в т.ч. из-за смены версии документа в конфиге).
    ``missing_consents`` — что именно нужно принять (пусто, если ничего).
    """

    needs_onboarding: bool
    missing_consents: list[MissingConsentSchema]

    @classmethod
    def build(
        cls,
        user: User,
        *,
        needs_onboarding: bool,
        missing: Sequence[ConsentDocument],
    ) -> AuthMeResponse:
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
            needs_onboarding=needs_onboarding,
            missing_consents=[
                MissingConsentSchema(document=doc.document, version=doc.version)
                for doc in missing
            ],
        )


class ConsentInputSchema(BaseModel):
    """Одно согласие, переданное клиентом при онбординге."""

    document: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)


class OnboardingRequest(BaseModel):
    """Тело ``POST /users/me/onboarding``."""

    username: str | None = Field(
        default=None,
        min_length=3,
        max_length=32,
        pattern=_USERNAME_PATTERN,
        description="Новый публичный хэндл (опционально)",
    )
    display_name: str | None = Field(
        default=None, min_length=1, max_length=100, description="Отображаемое имя"
    )
    consents: list[ConsentInputSchema] = Field(default_factory=list)


class ConsentResponse(BaseModel):
    """Один факт принятия документа (``GET /users/me/consents``)."""

    document: str
    version: str
    accepted_at: datetime
    method: str

    @classmethod
    def from_domain(cls, consent: Consent) -> ConsentResponse:
        return cls(
            document=consent.document,
            version=consent.version,
            accepted_at=consent.accepted_at,
            method=consent.method,
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
    username: str | None = Field(
        default=None,
        min_length=3,
        max_length=32,
        pattern=_USERNAME_PATTERN,
        description="Публичный хэндл (латиница/цифры/дефис)",
    )
