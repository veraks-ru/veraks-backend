"""Порты домена social (репозитории и шлюзы к соседним доменам)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.social.domain.entities import Comment, FeedItem, Follow


@dataclass(frozen=True, slots=True)
class UserRef:
    """Публичная ссылка на пользователя (для авторства/списков подписок)."""

    id: uuid.UUID
    username: str
    display_name: str


class CommentRepository(Protocol):
    """Хранилище комментариев."""

    async def add(self, comment: Comment) -> Comment: ...
    async def get_by_id(self, comment_id: uuid.UUID) -> Comment | None: ...
    async def list_for_event(
        self, event_id: uuid.UUID, *, limit: int = 200
    ) -> list[Comment]: ...
    async def soft_delete(self, comment: Comment) -> None: ...


class FollowRepository(Protocol):
    """Хранилище подписок (follower→followee)."""

    async def add(self, follow: Follow) -> Follow: ...
    async def remove(
        self, follower_id: uuid.UUID, followee_id: uuid.UUID
    ) -> bool: ...
    async def is_following(
        self, follower_id: uuid.UUID, followee_id: uuid.UUID
    ) -> bool: ...
    async def following_ids(self, follower_id: uuid.UUID) -> list[uuid.UUID]: ...
    async def follower_ids(self, followee_id: uuid.UUID) -> list[uuid.UUID]: ...
    async def count_following(self, follower_id: uuid.UUID) -> int: ...
    async def count_followers(self, followee_id: uuid.UUID) -> int: ...


@runtime_checkable
class UserLookup(Protocol):
    """Резолв пользователей (хэндл↔id, публичные ссылки) для social."""

    async def resolve_username(self, username: str) -> UserRef | None: ...
    async def refs_by_ids(
        self, ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, UserRef]: ...


@runtime_checkable
class EventExistsGateway(Protocol):
    """Проверка существования события и его автора (шов к домену events)."""

    async def exists(self, event_id: uuid.UUID) -> bool: ...

    async def creator_id(self, event_id: uuid.UUID) -> uuid.UUID | None:
        """Автор события (для уведомления о комментарии) или ``None``, если нет."""
        ...


@runtime_checkable
class FeedGateway(Protocol):
    """Сбор ленты активности по множеству отслеживаемых предсказателей."""

    async def recent_for_authors(
        self, author_ids: list[uuid.UUID], *, limit: int = 50, offset: int = 0
    ) -> list[FeedItem]: ...
