"""Резолв пользователей для social поверх таблицы ``users`` (интеграционный шов)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.domain.entities import UserStatus
from app.modules.social.ports.repositories import UserRef


class SqlAlchemyUserLookup:
    """``UserLookup`` — только активные пользователи (псевдонимные ссылки)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_username(self, username: str) -> UserRef | None:
        stmt = select(UserORM).where(
            UserORM.username == username,
            UserORM.status == UserStatus.ACTIVE,
        )
        user = (await self._session.execute(stmt)).scalar_one_or_none()
        if user is None:
            return None
        return UserRef(
            id=user.id, username=user.username, display_name=user.display_name
        )

    async def refs_by_ids(
        self, ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, UserRef]:
        if not ids:
            return {}
        stmt = select(UserORM).where(UserORM.id.in_(set(ids)))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {
            u.id: UserRef(id=u.id, username=u.username, display_name=u.display_name)
            for u in rows
        }
