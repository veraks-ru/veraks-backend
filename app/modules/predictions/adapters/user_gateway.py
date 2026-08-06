"""Адаптер резолва пользователя по хэндлу поверх таблицы users (identity).

Реализует порт ``UserDirectory`` для публичного трек-рекорда и доски лучших
прогнозов. Возвращает только активные аккаунты (удалённые/заблокированные
публично не доступны).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.domain.entities import UserStatus
from app.modules.predictions.ports.users import PublicUserRef


class SqlAlchemyUserDirectory:
    """Резолв ``user_id`` ↔ username (только активные)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_username(self, username: str) -> uuid.UUID | None:
        """``id`` активного пользователя по username (citext) или ``None``."""
        stmt = select(UserORM.id).where(
            UserORM.username == username,
            UserORM.status == UserStatus.ACTIVE,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active_by_ids(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, PublicUserRef]:
        """Хэндлы активных пользователей из ``user_ids`` одним запросом."""
        if not user_ids:
            return {}
        stmt = select(UserORM).where(
            UserORM.id.in_(user_ids),
            UserORM.status == UserStatus.ACTIVE,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {
            row.id: PublicUserRef(
                user_id=row.id, username=row.username, display_name=row.display_name
            )
            for row in rows
        }
