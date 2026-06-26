"""FastAPI-роутер профилей пользователей (`/users`).

Публичный профиль по хэндлу (псевдоним, без ПДн) и редактирование своего
профиля. Аутентификация требуется только для ``/users/me``. Доменные ошибки
маппятся в HTTP централизованно в ``app/main.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.identity.api.dependencies import (
    CurrentUser,
    get_public_profile_uc,
    get_update_profile_uc,
    get_user_repository,
)
from app.modules.identity.api.schemas import (
    MeResponse,
    PublicProfileResponse,
    PublicUserRef,
    UpdateProfileRequest,
)
from app.modules.identity.application.use_cases import (
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
    """Редактирует профиль текущего пользователя (display_name)."""
    user = await uc.execute(
        user_id=current_user.id, display_name=payload.display_name
    )
    return MeResponse.from_domain(user)


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
