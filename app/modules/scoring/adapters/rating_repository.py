"""SQLAlchemy-реализация репозитория рейтингов."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.scoring.adapters.orm import RatingORM
from app.modules.scoring.domain.entities import Rating, ScopeType


class SqlAlchemyRatingRepository:
    """Хранилище рейтингов поверх асинхронной сессии SQLAlchemy.

    ``upsert_many`` вызывается единственным фоновым пересчётом (без гонок),
    поэтому реализован как «обновить-или-вставить» по ключу области — это не
    зависит от версии Postgres (``ON CONFLICT`` с ``NULL`` в ключе требует
    ``NULLS NOT DISTINCT``/PG15+).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, ratings: Sequence[Rating]) -> int:
        """Идемпотентно сохраняет рейтинги (latest-wins по ключу области)."""
        for rating in ratings:
            existing = await self._find(
                rating.user_id, rating.scope_type, rating.scope_id
            )
            if existing is None:
                self._session.add(RatingORM.from_domain(rating))
                continue
            existing.mean_brier = rating.mean_brier
            existing.skill_score = rating.skill_score
            existing.calibration_error = rating.calibration_error
            existing.n_resolved = rating.n_resolved
            existing.rank = rating.rank
            existing.updated_at = rating.updated_at
        await self._session.flush()
        return len(ratings)

    async def leaderboard(
        self,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Rating]:
        """Топ области по предрасчитанному рангу (по возрастанию)."""
        stmt = (
            select(RatingORM)
            .where(
                RatingORM.scope_type == scope_type,
                self._scope_match(scope_id),
            )
            .order_by(RatingORM.rank.asc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]

    async def get_for_user(
        self,
        user_id: uuid.UUID,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
    ) -> Rating | None:
        """Рейтинг пользователя в области (для профиля) или ``None``."""
        orm = await self._find(user_id, scope_type, scope_id)
        return orm.to_domain() if orm else None

    async def _find(
        self,
        user_id: uuid.UUID,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
    ) -> RatingORM | None:
        stmt = select(RatingORM).where(
            RatingORM.user_id == user_id,
            RatingORM.scope_type == scope_type,
            self._scope_match(scope_id),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _scope_match(scope_id: uuid.UUID | None):  # type: ignore[no-untyped-def]
        """Условие сравнения ``scope_id`` с корректной обработкой ``NULL`` (global)."""
        if scope_id is None:
            return RatingORM.scope_id.is_(None)
        return RatingORM.scope_id == scope_id
