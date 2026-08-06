"""FastAPI-роутер профилей пользователей (`/users`).

Публичный профиль по хэндлу (псевдоним, без ПДн) и редактирование своего
профиля. Аутентификация требуется только для ``/users/me``. Доменные ошибки
маппятся в HTTP централизованно в ``app/main.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.config import SettingsDep
from app.http import client_ip
from app.modules.billing.application.dto import Actor as BillingActor
from app.modules.billing.application.use_cases import (
    CancelSubscription as BillingCancelSubscription,
)
from app.modules.billing.domain.entities import SubscriptionStatus
from app.modules.billing.ports.repositories import (
    SubscriptionRepository as BillingSubscriptionRepository,
)
from app.modules.identity.api.dependencies import (
    CurrentUser,
    get_billing_subscription_repository,
    get_cancel_subscription_on_delete,
    get_complete_onboarding_uc,
    get_delete_my_account_uc,
    get_my_consents_uc,
    get_public_profile_uc,
    get_update_profile_uc,
    get_user_repository,
)
from app.modules.identity.api.router import clear_session_cookies
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
    DeleteMyAccount,
    GetMyConsents,
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
) -> AuthMeResponse:
    """Фиксирует принятие обязательных документов и завершает онбординг.

    Идемпотентен: если онбординг уже пройден и недостающих согласий нет,
    просто возвращает текущее состояние 200 (плюс применяет переданные
    правки профиля, как обычный PATCH). Иначе при неполном наборе согласий —
    ``IncompleteConsentsError`` (422, маппинг в ``app/main.py``).

    ``uc.execute()`` либо бросает ``IncompleteConsentsError``, либо
    гарантирует постусловие «все обязательные согласия текущих версий
    приняты, onboarded_at выставлен» — поэтому статус после успешного
    вызова известен без повторного похода в ``GetOnboardingStatus``.
    """
    user = await uc.execute(
        user_id=current_user.id,
        username=payload.username,
        display_name=payload.display_name,
        provided_consents=[
            ConsentInput(document=c.document, version=c.version)
            for c in payload.consents
        ],
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return AuthMeResponse.build(user, needs_onboarding=False, missing=[])


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


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить аккаунт (самостоятельно, 152-ФЗ)",
)
async def delete_me(
    response: Response,
    settings: SettingsDep,
    current_user: CurrentUser,
    delete_uc: Annotated[DeleteMyAccount, Depends(get_delete_my_account_uc)],
    subscriptions: Annotated[
        BillingSubscriptionRepository, Depends(get_billing_subscription_repository)
    ],
    cancel_subscription_uc: Annotated[
        BillingCancelSubscription, Depends(get_cancel_subscription_on_delete)
    ],
) -> Response:
    """Необратимо удаляет аккаунт: анонимизация профиля + отзыв сессий.

    Активная подписка не блокирует удаление: автопродление отменяется тем же
    путём, что и ручной ``POST /billing/subscriptions/{id}/cancel`` (уже
    списанные средства не возвращаются — вопрос к юристу, не к коду). После
    анонимизации отзывается всё семейство refresh-токенов; доступ по уже
    выданному access-токену истечёт сам (TTL ≤ 15 мин). Cookie чистим так же,
    как при обычном logout.
    """
    subscription = await subscriptions.get_latest_by_user(current_user.id)
    if subscription is not None and subscription.status is SubscriptionStatus.ACTIVE:
        await cancel_subscription_uc.execute(
            subscription_id=subscription.id,
            actor=BillingActor(user_id=current_user.id, role=current_user.role),
        )
    await delete_uc.execute(user_id=current_user.id)
    clear_session_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
