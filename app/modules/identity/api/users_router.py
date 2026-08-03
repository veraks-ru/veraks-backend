"""FastAPI-роутер профилей пользователей (`/users`).

Публичный профиль по хэндлу (псевдоним, без ПДн) и редактирование своего
профиля. Аутентификация требуется только для ``/users/me``. Доменные ошибки
маппятся в HTTP централизованно в ``app/main.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.modules.identity.api.dependencies import (
    CurrentUser,
    get_complete_onboarding_uc,
    get_my_consents_uc,
    get_onboarding_status_uc,
    get_public_profile_uc,
    get_update_profile_uc,
    get_user_repository,
)
from app.modules.identity.api.schemas import (
    AuthMeResponse,
    ConsentResponse,
    MeResponse,
    OnboardingRequest,
    PublicProfileResponse,
    PublicUserRef,
    UpdateProfileRequest,
)
from app.modules.identity.application.dto import ConsentInput
from app.modules.identity.application.use_cases import (
    CompleteOnboarding,
    GetMyConsents,
    GetOnboardingStatus,
    GetPublicProfile,
    UpdateMyProfile,
)
from app.modules.identity.domain.entities import UserStatus
from app.modules.identity.domain.errors import UserNotFoundError
from app.modules.identity.ports.repositories import UserRepository

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/lookup/{user_id}",
    response_model=PublicUserRef,
    summary="Публичный хэндл по id (для лидербордов)",
)
async def public_profile_by_id(
    user_id: uuid.UUID,
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> PublicUserRef:
    """Резолвит ``user_id`` в публичный хэндл (псевдоним). Только активные."""
    user = await users.get_by_id(user_id)
    if user is None or user.status is not UserStatus.ACTIVE:
        raise UserNotFoundError("Профиль не найден")
    return PublicUserRef(user_id=user.id, username=user.username, display_name=user.display_name)


@router.patch("/me", response_model=MeResponse, summary="Изменить свой профиль")
async def update_me(
    payload: UpdateProfileRequest,
    current_user: CurrentUser,
    uc: Annotated[UpdateMyProfile, Depends(get_update_profile_uc)],
) -> MeResponse:
    """Редактирует профиль текущего пользователя (display_name, username)."""
    user = await uc.execute(
        user_id=current_user.id,
        display_name=payload.display_name,
        username=payload.username,
    )
    return MeResponse.from_domain(user)


@router.post(
    "/me/onboarding",
    response_model=AuthMeResponse,
    summary="Пройти онбординг (согласия 152-ФЗ + псевдоним)",
)
async def complete_onboarding(
    payload: OnboardingRequest,
    request: Request,
    current_user: CurrentUser,
    uc: Annotated[CompleteOnboarding, Depends(get_complete_onboarding_uc)],
    status_uc: Annotated[GetOnboardingStatus, Depends(get_onboarding_status_uc)],
) -> AuthMeResponse:
    """Фиксирует принятие обязательных документов и завершает онбординг.

    Идемпотентен: если онбординг уже пройден и недостающих согласий нет,
    просто возвращает текущее состояние 200 (плюс применяет переданные
    правки профиля, как обычный PATCH). Иначе при неполном наборе согласий —
    ``IncompleteConsentsError`` (422, маппинг в ``app/main.py``).
    """
    user = await uc.execute(
        user_id=current_user.id,
        username=payload.username,
        display_name=payload.display_name,
        provided_consents=[
            ConsentInput(document=c.document, version=c.version)
            for c in payload.consents
        ],
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    needs_onboarding, missing = await status_uc.execute(user=user)
    return AuthMeResponse.build(
        user, needs_onboarding=needs_onboarding, missing=missing
    )


@router.get(
    "/me/consents",
    response_model=list[ConsentResponse],
    summary="Мои согласия (152-ФЗ)",
)
async def my_consents(
    current_user: CurrentUser,
    uc: Annotated[GetMyConsents, Depends(get_my_consents_uc)],
) -> list[ConsentResponse]:
    """Список принятых текущим пользователем документов."""
    consents = await uc.execute(user_id=current_user.id)
    return [ConsentResponse.from_domain(c) for c in consents]


@router.get(
    "/{username}",
    response_model=PublicProfileResponse,
    summary="Публичный профиль по хэндлу",
)
async def public_profile(
    username: str,
    uc: Annotated[GetPublicProfile, Depends(get_public_profile_uc)],
) -> PublicProfileResponse:
    """Возвращает псевдонимный публичный профиль; 404, если нет/неактивен."""
    user = await uc.execute(username=username)
    return PublicProfileResponse.from_domain(user)
