"""FastAPI-роутер модерации пользователей (`/admin/users`, только ADMIN, B7).

Отдельный роутер (не под `/auth`, не под `/users`): эндпоинты не должны
попадать под жёсткий rate limit `/auth/*` (см. `app/middleware/rate_limit.py`)
и логически относятся к админке, а не к профилю пользователя. RBAC — общий
для identity гард `require_admin` (см. `api/dependencies.py`), по тому же
паттерну, что и `app/shared/audit/api/router.py`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.modules.identity.api.dependencies import (
    AdminUser,
    get_list_users_uc,
    get_reinstate_user_uc,
    get_suspend_user_uc,
)
from app.modules.identity.api.schemas import (
    AdminUserResponse,
    SuspendUserRequest,
    UserPageResponse,
)
from app.modules.identity.application.use_cases import (
    ListUsers,
    ReinstateUser,
    SuspendUser,
)
from app.modules.identity.domain.entities import UserStatus

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


@router.get("", response_model=UserPageResponse, summary="Список пользователей (admin)")
async def list_users(
    _admin: AdminUser,
    uc: Annotated[ListUsers, Depends(get_list_users_uc)],
    status_filter: Annotated[
        UserStatus | None, Query(alias="status", description="Фильтр по статусу")
    ] = None,
    search: Annotated[
        str | None,
        Query(max_length=100, description="Поиск по username/display_name (ILIKE)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserPageResponse:
    """Постраничный список для модерации: фильтр по статусу + поиск по хэндлу/имени."""
    page = await uc.execute(status=status_filter, search=search, limit=limit, offset=offset)
    return UserPageResponse(
        items=[AdminUserResponse.from_domain(u) for u in page.items],
        total=page.total,
    )


@router.post(
    "/{user_id}/suspend",
    response_model=AdminUserResponse,
    summary="Заблокировать пользователя (admin, модерация)",
)
async def suspend_user(
    user_id: uuid.UUID,
    payload: SuspendUserRequest,
    admin: AdminUser,
    uc: Annotated[SuspendUser, Depends(get_suspend_user_uc)],
) -> AdminUserResponse:
    """Блокирует активный аккаунт и отзывает его refresh-сессии.

    Нельзя заблокировать самого себя или другого администратора (доменные
    ошибки → 403, см. ``_ERROR_STATUS`` в ``app/main.py``). Причина обязательна
    и уходит в аудит (``identity.user.suspended``), но не в публичный профиль.
    """
    user = await uc.execute(actor=admin, target_id=user_id, reason=payload.reason)
    return AdminUserResponse.from_domain(user)


@router.post(
    "/{user_id}/reinstate",
    response_model=AdminUserResponse,
    summary="Разблокировать пользователя (admin)",
)
async def reinstate_user(
    user_id: uuid.UUID,
    admin: AdminUser,
    uc: Annotated[ReinstateUser, Depends(get_reinstate_user_uc)],
) -> AdminUserResponse:
    """Возвращает заблокированный аккаунт в ``active`` (``identity.user.reinstated``)."""
    user = await uc.execute(actor=admin, target_id=user_id)
    return AdminUserResponse.from_domain(user)
