"""Use-cases чтения/пометки уведомлений."""

from __future__ import annotations

import uuid

from app.modules.notifications.domain.entities import Notification
from app.modules.notifications.ports.repositories import NotificationRepository


class ListMyNotifications:
    def __init__(self, *, repository: NotificationRepository) -> None:
        self._repo = repository

    async def execute(self, *, user_id: uuid.UUID, limit: int = 50) -> list[Notification]:
        return await self._repo.list_for_user(user_id, limit=limit)


class CountUnread:
    def __init__(self, *, repository: NotificationRepository) -> None:
        self._repo = repository

    async def execute(self, *, user_id: uuid.UUID) -> int:
        return await self._repo.count_unread(user_id)


class MarkNotificationRead:
    def __init__(self, *, repository: NotificationRepository) -> None:
        self._repo = repository

    async def execute(self, *, user_id: uuid.UUID, notification_id: uuid.UUID) -> None:
        await self._repo.mark_read(user_id, notification_id)


class MarkAllRead:
    def __init__(self, *, repository: NotificationRepository) -> None:
        self._repo = repository

    async def execute(self, *, user_id: uuid.UUID) -> int:
        return await self._repo.mark_all_read(user_id)
