"""Адаптер резолва пользователя по хэндлу поверх таблицы users (identity).

Реализует порт ``UserDirectory`` для публичной калибровки. Возвращает id только
активного аккаунта (удалённые/заблокированные публично не доступны).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.domain.entities import UserStatus


class SqlAlchemyUserDirectory:
    """Резолв ``user_id`` по username (только активные)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_username(self, username: str) -> uuid.UUID | None:
        """``id`` активного пользователя по username (citext) или ``None``."""
        stmt = select(UserORM.id).where(
            UserORM.username == username,
            UserORM.status == UserStatus.ACTIVE,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
