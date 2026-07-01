"""Шлюз стендингов leagues поверх ``ratings`` домена scoring (интеграционный шов)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.adapters.orm import UserORM
from app.modules.leagues.ports.repositories import StandingRow
from app.modules.scoring.adapters.orm import RatingORM
from app.modules.scoring.domain.entities import ScopeType


class SqlAlchemyStandingsGateway:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def global_rows(
        self, user_ids: list[uuid.UUID]
    ) -> list[StandingRow]:
        if not user_ids:
            return []
        ids = set(user_ids)
        # Публичные ссылки на всех участников (включая тех, у кого нет рейтинга).
        users = {
            u.id: u
            for u in (
                await self._session.execute(
                    select(UserORM).where(UserORM.id.in_(ids))
                )
            ).scalars().all()
        }
        # Глобальные рейтинги тех, у кого они есть.
        ratings = {
            r.user_id: r
            for r in (
                await self._session.execute(
                    select(RatingORM).where(
                        RatingORM.scope_type == ScopeType.GLOBAL,
                        RatingORM.scope_id.is_(None),
                        RatingORM.user_id.in_(ids),
                    )
                )
            ).scalars().all()
        }
        rows: list[StandingRow] = []
        for uid in ids:
            user = users.get(uid)
            if user is None:
                continue
            rating = ratings.get(uid)
            rows.append(
                StandingRow(
                    user_id=uid,
                    username=user.username,
                    display_name=user.display_name,
                    skill_score=rating.skill_score if rating else None,
                    mean_brier=rating.mean_brier if rating else None,
                    n_resolved=rating.n_resolved if rating else 0,
                    rank=0,
                )
            )
        return rows

    async def season_ranked_ids(
        self, season_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        if not user_ids:
            return []
        ids = set(user_ids)
        ranked = list(
            (
                await self._session.execute(
                    select(RatingORM.user_id)
                    .where(
                        RatingORM.scope_type == ScopeType.SEASON,
                        RatingORM.scope_id == season_id,
                        RatingORM.user_id.in_(ids),
                    )
                    .order_by(RatingORM.rank.asc())
                )
            ).scalars().all()
        )
        # Участники без сезонного рейтинга — в конец (худшие), в стабильном порядке.
        seen = set(ranked)
        tail = [uid for uid in user_ids if uid not in seen]
        return ranked + tail

    async def season_rated_ids(self, season_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            (
                await self._session.execute(
                    select(RatingORM.user_id).where(
                        RatingORM.scope_type == ScopeType.SEASON,
                        RatingORM.scope_id == season_id,
                    )
                )
            ).scalars().all()
        )
