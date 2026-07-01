"""Порты домена notifications."""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.modules.notifications.domain.entities import Notification


class NotificationRepository(Protocol):
    """Хранилище уведомлений."""

    async def add(self, notification: Notification) -> Notification: ...
    async def list_for_user(self, user_id: uuid.UUID, *, limit: int = 50) -> list[Notification]: ...
    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> None: ...
    async def mark_all_read(self, user_id: uuid.UUID) -> int: ...
    async def count_unread(self, user_id: uuid.UUID) -> int: ...


@runtime_checkable
class NotificationEmitter(Protocol):
    """Шов для других доменов: создать уведомление адресату.

    Реализация пишет запись в БД (и, при подключении, пушит в реальном времени).
    """

    async def emit(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        title: str,
        body: str = "",
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> None: ...
