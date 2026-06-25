"""FastAPI-роутер профилей пользователей (`/users`).

Публичный профиль по хэндлу (псевдоним, без ПДн) и редактирование своего
профиля. Аутентификация требуется только для ``/users/me``. Доменные ошибки
маппятся в HTTP централизованно в ``app/main.py``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.identity.api.dependencies import (
    CurrentUser,
    get_public_profile_uc,
    get_update_profile_uc,
)
from app.modules.identity.api.schemas import (
    MeResponse,
    PublicProfileResponse,
    UpdateProfileRequest,
)
from app.modules.identity.application.use_cases import (
    GetPublicProfile,
    UpdateMyProfile,
)

router = APIRouter(prefix="/users", tags=["users"])


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
