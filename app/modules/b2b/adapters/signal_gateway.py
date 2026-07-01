"""Шлюз сигналов B2B: агрегатные чтения предсказаний/рейтингов/событий.

Интеграционный шов к соседним доменам (predictions/scoring/events). Консенсус
считается агрегатом в БД (COUNT/AVG/GROUP BY), а не материализацией голосов.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.b2b.application.dto import (
    ConsensusSignal,
    EventSignal,
    LeaderboardSignalRow,
)
from app.modules.events.adapters.orm import EventORM
from app.modules.events.domain.entities import EventStatus
from app.modules.identity.adapters.orm import UserORM
from app.modules.predictions.adapters.orm import PredictionORM
from app.modules.scoring.adapters.orm import RatingORM
from app.modules.scoring.domain.entities import ScopeType


class SqlAlchemyB2bSignalGateway:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def consensus(self, event_id: uuid.UUID) -> ConsensusSignal | None:
        exists = (
            await self._session.execute(
                select(EventORM.id).where(EventORM.id == event_id)
            )
        ).first()
        if exists is None:
            return None

        total, avg = (
            await self._session.execute(
                select(func.count(), func.avg(PredictionORM.probability)).where(
                    PredictionORM.event_id == event_id
                )
            )
        ).one()
        dist_rows = (
            await self._session.execute(
                select(PredictionORM.confidence_grade, func.count())
                .where(PredictionORM.event_id == event_id)
                .group_by(PredictionORM.confidence_grade)
            )
        ).all()
        distribution = {
            (grade.value if hasattr(grade, "value") else str(grade)): int(count)
            for grade, count in dist_rows
        }
        return ConsensusSignal(
            event_id=event_id,
            total_count=int(total),
            mean_probability=float(avg) if avg is not None else None,
            distribution=distribution,
        )

    async def leaderboard(
        self, *, scope: str, scope_id: uuid.UUID | None, limit: int
    ) -> list[LeaderboardSignalRow]:
        try:
            scope_type = ScopeType(scope)
        except ValueError:
            return []
        stmt = (
            select(RatingORM, UserORM.username)
            .join(UserORM, UserORM.id == RatingORM.user_id)
            .where(RatingORM.scope_type == scope_type)
            .order_by(RatingORM.rank.asc())
            .limit(limit)
        )
        stmt = stmt.where(
            RatingORM.scope_id.is_(None)
            if scope_id is None
            else RatingORM.scope_id == scope_id
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            LeaderboardSignalRow(
                rank=r.rank,
                user_id=r.user_id,
                username=username,
                skill_score=r.skill_score,
                mean_brier=r.mean_brier,
                n_resolved=r.n_resolved,
            )
            for r, username in rows
        ]

    async def events(
        self, *, status: str | None, limit: int
    ) -> list[EventSignal]:
        stmt = select(EventORM).order_by(EventORM.closes_at.asc()).limit(limit)
        if status is not None:
            try:
                stmt = stmt.where(EventORM.status == EventStatus(status))
            except ValueError:
                return []
        else:
            # Без фильтра прячем предложения на модерации (как публичный список).
            stmt = stmt.where(EventORM.status != EventStatus.PROPOSED)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            EventSignal(
                id=e.id,
                title=e.title,
                category_id=e.category_id,
                season_id=e.season_id,
                status=e.status.value,
                opens_at=e.opens_at,
                closes_at=e.closes_at,
                resolves_at=e.resolves_at,
                outcome=e.outcome,
            )
            for e in rows
        ]
