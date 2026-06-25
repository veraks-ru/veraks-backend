"""Шлюз billing к домену seasons: резолв сезона по публичному ``slug``.

Прозрачность фонда по сезону (``GET /seasons/{slug}/prize-fund``) требует
перевести slug → id. billing читает таблицу ``seasons`` напрямую (модульный
монолит), не завязываясь на внутренние типы домена seasons.

TODO(billing-integration): прямое чтение таблицы соседнего домена в монолите;
заменить сетевым контрактом при выделении seasons в отдельный сервис.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.seasons.adapters.orm import SeasonORM


class SqlAlchemySeasonDirectory:
    """Резолв ``id`` сезона по slug поверх таблицы ``seasons``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_slug(self, slug: str) -> uuid.UUID | None:
        """``id`` сезона по slug (citext, регистронезависимо) или ``None``."""
        stmt = select(SeasonORM.id).where(SeasonORM.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()
