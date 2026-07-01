"""Роутер уведомлений (`/users/me/notifications`)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.modules.identity.api.dependencies import CurrentUser
from app.modules.notifications.api.dependencies import (
    get_count_unread,
    get_list_notifications,
    get_mark_all_read,
    get_mark_read,
)
from app.modules.notifications.api.schemas import NotificationResponse
from app.modules.notifications.application.use_cases import (
    CountUnread,
    ListMyNotifications,
    MarkAllRead,
    MarkNotificationRead,
)

router = APIRouter(prefix="/users/me/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationResponse], summary="Мои уведомления")
async def list_notifications(
    current_user: CurrentUser,
    uc: Annotated[ListMyNotifications, Depends(get_list_notifications)],
) -> list[NotificationResponse]:
    items = await uc.execute(user_id=current_user.id)
    return [NotificationResponse.from_domain(n) for n in items]


@router.get("/unread-count", summary="Число непрочитанных")
async def unread_count(
    current_user: CurrentUser,
    uc: Annotated[CountUnread, Depends(get_count_unread)],
) -> dict[str, int]:
    return {"unread": await uc.execute(user_id=current_user.id)}


@router.post("/read", status_code=status.HTTP_204_NO_CONTENT, summary="Прочитать все")
async def read_all(
    current_user: CurrentUser,
    uc: Annotated[MarkAllRead, Depends(get_mark_all_read)],
) -> None:
    await uc.execute(user_id=current_user.id)


@router.post(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Прочитать одно",
)
async def read_one(
    notification_id: uuid.UUID,
    current_user: CurrentUser,
    uc: Annotated[MarkNotificationRead, Depends(get_mark_read)],
) -> None:
    await uc.execute(user_id=current_user.id, notification_id=notification_id)
