"""Адаптер справочника пользователей поверх таблицы users (identity).

Реализует порт ``UserDirectory`` для публичной калибровки и лидербордов.
Возвращает только активные аккаунты (удалённые/заблокированные публично не
доступны).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.domain.entities import UserStatus


class SqlAlchemyUserDirectory:
    """Резолв ``user_id`` по username и отбор активных id (только активные)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_username(self, username: str) -> uuid.UUID | None:
        """``id`` активного пользователя по username (citext) или ``None``."""
        stmt = select(UserORM.id).where(
            UserORM.username == username,
            UserORM.status == UserStatus.ACTIVE,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active_ids(
        self, user_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Активные из ``user_ids`` одним запросом (страница лидерборда)."""
        if not user_ids:
            return set()
        stmt = select(UserORM.id).where(
            UserORM.id.in_(user_ids),
            UserORM.status == UserStatus.ACTIVE,
        )
        return set((await self._session.execute(stmt)).scalars().all())
