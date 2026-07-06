"""Адаптер-эмиттер уведомлений (шов для других доменов).

Пишет уведомление в БД. Реал-тайм-пуш (goctopus) подключается поверх этого
адаптера отдельным декоратором, не меняя доменные use-cases.
"""

from __future__ import annotations

import asyncio
import uuid

from app.modules.notifications.adapters.goctopus import GoctopusPusher
from app.modules.notifications.domain.entities import Notification
from app.modules.notifications.ports.repositories import NotificationRepository

# Держим ссылки на фоновые пуш-задачи, чтобы их не собрал GC до завершения.
_PUSH_TASKS: set[asyncio.Task[None]] = set()


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
        payload = {
            "id": str(saved.id),
            "kind": kind,
            "title": title,
            "body": body,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id else None,
            "created_at": saved.created_at.isoformat(),
        }
        # Реал-тайм-пуш — вне пути транзакции (M-NOTIF): HTTP к goctopus (до 3с)
        # нельзя держать в открытой транзакции, иначе на время сетевого вызова
        # удерживается и глобальный advisory-лок цепочки аудита, сериализуя запись
        # аудита всей системы. Отправляем фоновой задачей; пуш best-effort
        # (goctopus глушит ошибки), источник истины — уже сохранённая запись в БД.
        task = asyncio.create_task(self._pusher.push(str(user_id), payload))
        _PUSH_TASKS.add(task)
        task.add_done_callback(_PUSH_TASKS.discard)
