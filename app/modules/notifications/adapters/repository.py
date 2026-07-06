"""SQLAlchemy-репозиторий уведомлений."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.adapters.orm import NotificationORM
from app.modules.notifications.domain.entities import Notification


class SqlAlchemyNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, notification: Notification) -> Notification:
        orm = NotificationORM.from_domain(notification)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int = 50
    ) -> list[Notification]:
        stmt = (
            select(NotificationORM)
            .where(NotificationORM.user_id == user_id)
            .order_by(NotificationORM.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [r.to_domain() for r in rows]

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> None:
        await self._session.execute(
            update(NotificationORM)
            .where(
                NotificationORM.id == notification_id,
                NotificationORM.user_id == user_id,
            )
            .values(is_read=True)
        )

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        result = await self._session.execute(
            update(NotificationORM)
            .where(
                NotificationORM.user_id == user_id,
                NotificationORM.is_read.is_(False),
            )
            .values(is_read=True)
        )
        return cast("CursorResult[Any]", result).rowcount or 0

    async def count_unread(self, user_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            NotificationORM.user_id == user_id,
            NotificationORM.is_read.is_(False),
        )
        return int((await self._session.execute(stmt)).scalar_one())
