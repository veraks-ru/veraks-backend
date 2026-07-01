"""Composition root модуля notifications."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.modules.notifications.adapters.repository import (
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.application.use_cases import (
    CountUnread,
    ListMyNotifications,
    MarkAllRead,
    MarkNotificationRead,
)
from app.modules.notifications.ports.repositories import NotificationRepository

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_notification_repository(session: SessionDep) -> NotificationRepository:
    return SqlAlchemyNotificationRepository(session)


RepoDep = Annotated[NotificationRepository, Depends(get_notification_repository)]


def get_list_notifications(repository: RepoDep) -> ListMyNotifications:
    return ListMyNotifications(repository=repository)


def get_count_unread(repository: RepoDep) -> CountUnread:
    return CountUnread(repository=repository)


def get_mark_read(repository: RepoDep) -> MarkNotificationRead:
    return MarkNotificationRead(repository=repository)


def get_mark_all_read(repository: RepoDep) -> MarkAllRead:
    return MarkAllRead(repository=repository)
