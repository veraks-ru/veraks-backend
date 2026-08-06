"""Адаптер резолва категорий по id поверх таблицы categories (events).

Реализует порт ``CategoryDirectory`` для сводки профиля.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.orm import CategoryORM
from app.modules.scoring.application.dto import CategoryRef


class SqlAlchemyCategoryDirectory:
    """Резолв ``category_id`` → slug/название одним запросом."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_ids(
        self, category_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, CategoryRef]:
        """Категории из ``category_ids``, найденные в справочнике (по id)."""
        if not category_ids:
            return {}
        stmt = select(CategoryORM).where(CategoryORM.id.in_(category_ids))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {
            row.id: CategoryRef(category_id=row.id, slug=row.slug, title=row.title)
            for row in rows
        }
