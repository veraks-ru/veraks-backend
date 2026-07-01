"""SQLAlchemy-репозитории комментариев и подписок."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.social.adapters.orm import CommentORM, FollowORM
from app.modules.social.domain.entities import Comment, Follow


class SqlAlchemyCommentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, comment: Comment) -> Comment:
        orm = CommentORM.from_domain(comment)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def get_by_id(self, comment_id: uuid.UUID) -> Comment | None:
        orm = await self._session.get(CommentORM, comment_id)
        return orm.to_domain() if orm is not None else None

    async def list_for_event(
        self, event_id: uuid.UUID, *, limit: int = 200
    ) -> list[Comment]:
        stmt = (
            select(CommentORM)
            .where(
                CommentORM.event_id == event_id,
                CommentORM.deleted_at.is_(None),
            )
            .order_by(CommentORM.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [r.to_domain() for r in rows]

    async def soft_delete(self, comment: Comment) -> None:
        await self._session.execute(
            update(CommentORM)
            .where(CommentORM.id == comment.id)
            .values(deleted_at=comment.deleted_at)
        )


class SqlAlchemyFollowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, follow: Follow) -> Follow:
        orm = FollowORM.from_domain(follow)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def remove(
        self, follower_id: uuid.UUID, followee_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            delete(FollowORM).where(
                FollowORM.follower_id == follower_id,
                FollowORM.followee_id == followee_id,
            )
        )
        return (result.rowcount or 0) > 0

    async def is_following(
        self, follower_id: uuid.UUID, followee_id: uuid.UUID
    ) -> bool:
        stmt = (
            select(FollowORM.id)
            .where(
                FollowORM.follower_id == follower_id,
                FollowORM.followee_id == followee_id,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def following_ids(self, follower_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(FollowORM.followee_id)
            .where(FollowORM.follower_id == follower_id)
            .order_by(FollowORM.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def follower_ids(self, followee_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(FollowORM.follower_id)
            .where(FollowORM.followee_id == followee_id)
            .order_by(FollowORM.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_following(self, follower_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(FollowORM.follower_id == follower_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_followers(self, followee_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(FollowORM.followee_id == followee_id)
        return int((await self._session.execute(stmt)).scalar_one())
