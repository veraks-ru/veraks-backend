"""Адаптер-эмиттер уведомлений (шов для других доменов).

Пишет уведомление в БД. Реал-тайм-пуш (goctopus) подключается поверх этого
адаптера отдельным декоратором, не меняя доменные use-cases.
"""

from __future__ import annotations

import uuid

from app.modules.notifications.adapters.goctopus import GoctopusPusher
from app.modules.notifications.domain.entities import Notification
from app.modules.notifications.ports.repositories import NotificationRepository


class DbNotificationEmitter:
    def __init__(self, repository: NotificationRepository) -> None:
        self._repo = repository

    async def emit(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        title: str,
        body: str = "",
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> None:
        await self._repo.add(
            Notification(
                user_id=user_id,
                kind=kind,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
            )
        )


class PushingNotificationEmitter:
    """Пишет уведомление в БД и пушит его в реальном времени через goctopus."""

    def __init__(
        self, repository: NotificationRepository, pusher: GoctopusPusher
    ) -> None:
        self._repo = repository
        self._pusher = pusher

    async def emit(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        title: str,
        body: str = "",
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> None:
        saved = await self._repo.add(
            Notification(
                user_id=user_id,
                kind=kind,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
            )
        )
        await self._pusher.push(
            str(user_id),
            {
                "id": str(saved.id),
                "kind": kind,
                "title": title,
                "body": body,
                "entity_type": entity_type,
                "entity_id": str(entity_id) if entity_id else None,
                "created_at": saved.created_at.isoformat(),
            },
        )
