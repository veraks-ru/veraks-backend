"""Use-cases соцфич: комментарии, подписки, лента.

Зависимости только через порты; каждая операция — одна бизнес-транзакция.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.modules.identity.domain.entities import UserRole
from app.modules.social.domain.entities import Comment, FeedItem
from app.modules.social.domain.errors import (
    CommentEventNotFoundError,
    CommentForbiddenError,
    CommentNotFoundError,
    FollowTargetNotFoundError,
)
from app.modules.social.ports.clock import Clock
from app.modules.social.ports.repositories import (
    CommentRepository,
    EventExistsGateway,
    FeedGateway,
    FollowRepository,
    UserLookup,
    UserRef,
)

_MODERATOR_ROLES = {UserRole.EDITOR, UserRole.ARBITER, UserRole.ADMIN}


@dataclass(frozen=True, slots=True)
class CommentView:
    """Комментарий с публичной ссылкой на автора (для выдачи)."""

    comment: Comment
    author: UserRef | None


class PostComment:
    """Оставить комментарий к событию (доступно любому аутентифицированному)."""

    def __init__(
        self,
        *,
        comments: CommentRepository,
        events: EventExistsGateway,
        clock: Clock,
    ) -> None:
        self._comments = comments
        self._events = events
        self._clock = clock

    async def execute(
        self, *, event_id: uuid.UUID, author_id: uuid.UUID, body: str
    ) -> Comment:
        if not await self._events.exists(event_id):
            raise CommentEventNotFoundError("Событие не найдено")
        comment = Comment.create(
            event_id=event_id,
            author_id=author_id,
            body=body,
            now=self._clock.now(),
        )
        return await self._comments.add(comment)


class DeleteComment:
    """Удалить (мягко) комментарий — автор или модератор."""

    def __init__(self, *, comments: CommentRepository, clock: Clock) -> None:
        self._comments = comments
        self._clock = clock

    async def execute(
        self,
        *,
        comment_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: UserRole,
    ) -> None:
        comment = await self._comments.get_by_id(comment_id)
        if comment is None or comment.is_deleted:
            raise CommentNotFoundError("Комментарий не найден")
        is_author = comment.author_id == actor_id
        if not is_author and actor_role not in _MODERATOR_ROLES:
            raise CommentForbiddenError("Нет прав на удаление комментария")
        comment.soft_delete(now=self._clock.now())
        await self._comments.soft_delete(comment)


class ListEventComments:
    """Список видимых комментариев события с авторами (публичное чтение)."""

    def __init__(
        self, *, comments: CommentRepository, users: UserLookup
    ) -> None:
        self._comments = comments
        self._users = users

    async def execute(self, *, event_id: uuid.UUID) -> list[CommentView]:
        items = await self._comments.list_for_event(event_id)
        refs = await self._users.refs_by_ids([c.author_id for c in items])
        return [CommentView(comment=c, author=refs.get(c.author_id)) for c in items]


class FollowUser:
    """Подписаться на предсказателя по хэндлу."""

    def __init__(
        self, *, follows: FollowRepository, users: UserLookup
    ) -> None:
        self._follows = follows
        self._users = users

    async def execute(self, *, follower_id: uuid.UUID, username: str) -> None:
        target = await self._users.resolve_username(username)
        if target is None:
            raise FollowTargetNotFoundError("Пользователь не найден")
        from app.modules.social.domain.entities import Follow

        # Идемпотентно: повторная подписка — no-op.
        if await self._follows.is_following(follower_id, target.id):
            return
        await self._follows.add(
            Follow(follower_id=follower_id, followee_id=target.id)
        )


class UnfollowUser:
    """Отписаться от предсказателя по хэндлу (идемпотентно)."""

    def __init__(
        self, *, follows: FollowRepository, users: UserLookup
    ) -> None:
        self._follows = follows
        self._users = users

    async def execute(self, *, follower_id: uuid.UUID, username: str) -> bool:
        target = await self._users.resolve_username(username)
        if target is None:
            raise FollowTargetNotFoundError("Пользователь не найден")
        return await self._follows.remove(follower_id, target.id)


@dataclass(frozen=True, slots=True)
class SocialStats:
    user_id: uuid.UUID
    followers: int
    following: int
    is_following: bool


class GetSocialStats:
    """Счётчики подписок пользователя (+ подписан ли зритель)."""

    def __init__(
        self, *, follows: FollowRepository, users: UserLookup
    ) -> None:
        self._follows = follows
        self._users = users

    async def execute(
        self, *, username: str, viewer_id: uuid.UUID | None = None
    ) -> SocialStats:
        target = await self._users.resolve_username(username)
        if target is None:
            raise FollowTargetNotFoundError("Пользователь не найден")
        is_following = (
            await self._follows.is_following(viewer_id, target.id)
            if viewer_id is not None
            else False
        )
        return SocialStats(
            user_id=target.id,
            followers=await self._follows.count_followers(target.id),
            following=await self._follows.count_following(target.id),
            is_following=is_following,
        )


class ListFollowing:
    """Кого читает пользователь (публичные ссылки)."""

    def __init__(
        self, *, follows: FollowRepository, users: UserLookup
    ) -> None:
        self._follows = follows
        self._users = users

    async def execute(self, *, user_id: uuid.UUID) -> list[UserRef]:
        ids = await self._follows.following_ids(user_id)
        refs = await self._users.refs_by_ids(ids)
        return [refs[i] for i in ids if i in refs]


class ListFollowers:
    """Читатели пользователя (публичные ссылки)."""

    def __init__(
        self, *, follows: FollowRepository, users: UserLookup
    ) -> None:
        self._follows = follows
        self._users = users

    async def execute(self, *, user_id: uuid.UUID) -> list[UserRef]:
        ids = await self._follows.follower_ids(user_id)
        refs = await self._users.refs_by_ids(ids)
        return [refs[i] for i in ids if i in refs]


class GetFeed:
    """Персональная лента: активность отслеживаемых предсказателей."""

    def __init__(
        self, *, follows: FollowRepository, feed: FeedGateway
    ) -> None:
        self._follows = follows
        self._feed = feed

    async def execute(
        self, *, user_id: uuid.UUID, limit: int = 50
    ) -> list[FeedItem]:
        author_ids = await self._follows.following_ids(user_id)
        if not author_ids:
            return []
        return await self._feed.recent_for_authors(author_ids, limit=limit)
