"""SQLAlchemy-реализация репозитория рейтингов."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.scoring.adapters.orm import RatingORM
from app.modules.scoring.domain.entities import Rating, ScopeType

# Ключ транзакционного advisory-лока, сериализующего пересчёты рейтингов.
_RECOMPUTE_LOCK_KEY = 704_235_911


class SqlAlchemyRatingRepository:
    """Хранилище рейтингов поверх асинхронной сессии SQLAlchemy.

    ``upsert_many`` реализован как «обновить-или-вставить» по ключу области — это
    не зависит от версии Postgres (``ON CONFLICT`` с ``NULL`` в ключе требует
    ``NULLS NOT DISTINCT``/PG15+). Конкурентные пересчёты сериализуются
    транзакционным advisory-локом (:meth:`acquire_recompute_lock`).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire_recompute_lock(self) -> None:
        """Берёт транзакционный advisory-лок на пересчёт рейтингов.

        Два конкурентных ``score_event`` (или score + ночной full) иначе гоняли
        бы UPDATE/INSERT одних строк ratings → ``IntegrityError`` по
        ``uq_ratings_user_scope`` или рассинхрон рангов. Лок снимается на коммите.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _RECOMPUTE_LOCK_KEY}
        )

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
            existing.qualified = rating.qualified
            existing.updated_at = rating.updated_at
        await self._session.flush()
        return len(ratings)

    async def prune_scopes(
        self,
        scopes: Iterable[tuple[ScopeType, uuid.UUID | None]],
        *,
        keep: Sequence[Rating],
    ) -> int:
        """Удаляет из пересчитанных срезов строки пользователей вне ``keep``.

        После полного пересчёта среза пользователь мог выбыть из рейтинга
        (overturn исхода, падение ниже порога) — его прежняя строка с рангом
        осталась бы «призраком». Здесь такие строки удаляются; строки из
        ``keep`` не трогаются (их обновит ``upsert_many``). Вызывать ДО upsert.
        """
        keep_users: dict[tuple[ScopeType, uuid.UUID | None], set[uuid.UUID]] = {}
        for rating in keep:
            keep_users.setdefault(
                (rating.scope_type, rating.scope_id), set()
            ).add(rating.user_id)

        deleted = 0
        for scope_type, scope_id in scopes:
            users = keep_users.get((scope_type, scope_id), set())
            stmt = delete(RatingORM).where(
                RatingORM.scope_type == scope_type,
                self._scope_match(scope_id),
            )
            if users:
                stmt = stmt.where(RatingORM.user_id.not_in(users))
            result = await self._session.execute(stmt)
            deleted += cast("CursorResult[Any]", result).rowcount or 0
        await self._session.flush()
        return deleted

    async def leaderboard(
        self,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
        *,
        limit: int = 50,
        offset: int = 0,
        qualified_only: bool = False,
    ) -> list[Rating]:
        """Топ области по предрасчитанному рангу (по возрастанию).

        ``qualified_only`` оставляет только квалифицированных участников —
        фильтр на уровне запроса, чтобы пагинация была корректной.
        """
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
        if qualified_only:
            stmt = stmt.where(RatingORM.qualified.is_(True))
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
