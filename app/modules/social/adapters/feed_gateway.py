"""Шлюз сборки ленты: комментарии и засчитанные прогнозы отслеживаемых авторов.

Интеграционный шов: читает ``comments`` (свой домен), ``predictions`` и
``events`` (соседние). При выносе в сервисы заменяется сетевым контрактом.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.orm import EventORM
from app.modules.identity.adapters.orm import UserORM
from app.modules.predictions.adapters.orm import PredictionORM
from app.modules.social.adapters.orm import CommentORM
from app.modules.social.domain.entities import FeedItem


class SqlAlchemyFeedGateway:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recent_for_authors(
        self, author_ids: list[uuid.UUID], *, limit: int = 50, offset: int = 0
    ) -> list[FeedItem]:
        if not author_ids:
            return []
        ids = set(author_ids)
        # Тянем из каждого источника окно до offset+limit, затем сливаем и режем
        # общий срез — так пагинация корректна поверх двух отсортированных потоков.
        window = offset + limit
        items: list[FeedItem] = []

        # Комментарии отслеживаемых авторов.
        comments = (
            await self._session.execute(
                select(CommentORM, EventORM.title, UserORM.username, UserORM.display_name)
                .join(EventORM, EventORM.id == CommentORM.event_id)
                .join(UserORM, UserORM.id == CommentORM.author_id)
                .where(
                    CommentORM.author_id.in_(ids),
                    CommentORM.deleted_at.is_(None),
                )
                .order_by(CommentORM.created_at.desc())
                .limit(window)
            )
        ).all()
        for comment, title, username, display_name in comments:
            items.append(
                FeedItem(
                    kind="comment",
                    actor_id=comment.author_id,
                    actor_username=username,
                    actor_display_name=display_name,
                    event_id=comment.event_id,
                    event_title=title,
                    occurred_at=comment.created_at,
                    body=comment.body,
                )
            )

        # Засчитанные прогнозы отслеживаемых авторов (виден Brier и исход).
        scored = (
            await self._session.execute(
                select(
                    PredictionORM,
                    EventORM.title,
                    EventORM.outcome,
                    UserORM.username,
                    UserORM.display_name,
                )
                .join(EventORM, EventORM.id == PredictionORM.event_id)
                .join(UserORM, UserORM.id == PredictionORM.user_id)
                .where(
                    PredictionORM.user_id.in_(ids),
                    PredictionORM.scored_at.is_not(None),
                    PredictionORM.brier_score.is_not(None),
                )
                .order_by(PredictionORM.scored_at.desc())
                .limit(window)
            )
        ).all()
        for prediction, title, outcome, username, display_name in scored:
            items.append(
                FeedItem(
                    kind="score",
                    actor_id=prediction.user_id,
                    actor_username=username,
                    actor_display_name=display_name,
                    event_id=prediction.event_id,
                    event_title=title,
                    occurred_at=prediction.scored_at,
                    brier=float(prediction.brier_score),
                    outcome=bool(outcome) if outcome is not None else None,
                )
            )

        items.sort(key=lambda it: it.occurred_at, reverse=True)
        return items[offset : offset + limit]
