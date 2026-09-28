"""Адаптер ленты открытых событий для свайпа — ``SqlAlchemyEventFeedReader``.

Кросс-табличное чтение: читает ``predictions`` напрямую из адаптера events —
единственное место в домене events, которому разрешено импортировать ORM
соседнего домена (прецеденты: ``app/modules/social/adapters/feed_gateway.py``,
``app/modules/b2b/adapters/signal_gateway.py``, оба читают ``PredictionORM``
из своих адаптеров тем же способом).

Два запроса на страницу — без N+1:
    1. страница событий с ``JOIN`` категории (статус/окно/категория/анти-джойн/
       курсор — всё в ``WHERE`` одного запроса);
    2. один агрегат ``GROUP BY event_id, confidence_grade`` по id уже
       отобранной страницы — сводка толпы считается в БД, а не циклом по
       прогнозам в Python.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.orm import CategoryORM, EventORM
from app.modules.events.domain.entities import EventStatus
from app.modules.events.ports.feed import (
    EventFeedItem,
    FeedCategoryRef,
    FeedCrowd,
    FeedQuery,
    FeedViewerAnswer,
)
from app.modules.predictions.adapters.orm import PredictionORM

# Пять грейдов уверенности — контракт ответа ленты (§3, §7 дизайн-спеки).
# Литералы, а не enum ``ConfidenceGrade`` модуля predictions: адаптеру
# достаточно строковых значений колонки, домен predictions сюда не тянем.
_ALL_GRADES: tuple[str, ...] = (
    "definitely_no",
    "probably_no",
    "fifty_fifty",
    "probably_yes",
    "definitely_yes",
)


class SqlAlchemyEventFeedReader:
    """Читает страницу ленты и сводку толпы по ней поверх асинхронной сессии."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def page(self, query: FeedQuery) -> list[EventFeedItem]:
        """Строит страницу карточек ленты (правила выборки — §3 спеки)."""
        # Режим «мои ответы»: вместо анти-джойна — обычный JOIN с прогнозом
        # зрителя, из него же берём его грейд и время последней правки.
        mine = query.only_predicted_by is not None
        answer_cols = (
            [PredictionORM.confidence_grade, PredictionORM.updated_at]
            if mine
            else [literal(None), literal(None)]
        )
        stmt = (
            select(
                EventORM, CategoryORM.id, CategoryORM.slug, CategoryORM.title, *answer_cols
            )
            .join(CategoryORM, CategoryORM.id == EventORM.category_id)
            .where(
                EventORM.status == EventStatus.OPEN,
                EventORM.opens_at <= query.now,
                EventORM.closes_at > query.now,
            )
        )
        if query.category_id is not None:
            stmt = stmt.where(EventORM.category_id == query.category_id)
        if mine:
            stmt = stmt.join(
                PredictionORM,
                and_(
                    PredictionORM.event_id == EventORM.id,
                    PredictionORM.user_id == query.only_predicted_by,
                ),
            )
        if query.exclude_predicted_by is not None:
            stmt = stmt.where(
                ~select(PredictionORM.id)
                .where(
                    PredictionORM.event_id == EventORM.id,
                    PredictionORM.user_id == query.exclude_predicted_by,
                )
                .exists()
            )
        if query.after is not None:
            cursor = query.after
            stmt = stmt.where(
                or_(
                    EventORM.closes_at > cursor.closes_at,
                    and_(
                        EventORM.closes_at == cursor.closes_at,
                        EventORM.id > cursor.event_id,
                    ),
                )
            )
        stmt = stmt.order_by(EventORM.closes_at.asc(), EventORM.id.asc()).limit(
            query.limit
        )

        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return []

        event_ids = [event_orm.id for event_orm, *_rest in rows]
        crowd_by_event = await self._crowd_for(event_ids)

        return [
            EventFeedItem(
                event=event_orm.to_domain(),
                category=FeedCategoryRef(id=cat_id, slug=cat_slug, title=cat_title),
                crowd=crowd_by_event[event_orm.id],
                viewer_answer=(
                    # SAEnum с values_callable отдаёт член enum — берём значение.
                    FeedViewerAnswer(confidence_grade=grade.value, updated_at=updated_at)
                    if grade is not None
                    else None
                ),
            )
            for event_orm, cat_id, cat_slug, cat_title, grade, updated_at in rows
        ]

    async def _crowd_for(
        self, event_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, FeedCrowd]:
        """Один ``GROUP BY`` на всю страницу — без N+1 по каждому событию."""
        rows = (
            await self._session.execute(
                select(
                    PredictionORM.event_id,
                    PredictionORM.confidence_grade,
                    func.count(),
                    func.sum(PredictionORM.probability),
                )
                .where(PredictionORM.event_id.in_(event_ids))
                .group_by(PredictionORM.event_id, PredictionORM.confidence_grade)
            )
        ).all()

        totals: dict[uuid.UUID, int] = dict.fromkeys(event_ids, 0)
        sums: dict[uuid.UUID, Decimal] = dict.fromkeys(event_ids, Decimal(0))
        distributions: dict[uuid.UUID, dict[str, int]] = {
            event_id: dict.fromkeys(_ALL_GRADES, 0) for event_id in event_ids
        }
        for event_id, grade, count, total_probability in rows:
            # ``confidence_grade`` — SAEnum с ``values_callable``: даже при
            # колоночном select приходит член enum, а не сырая строка.
            grade_value = grade.value
            n = int(count)
            distributions[event_id][grade_value] = n
            totals[event_id] += n
            sums[event_id] += Decimal(total_probability)

        return {
            event_id: FeedCrowd(
                total_count=totals[event_id],
                distribution=distributions[event_id],
                mean_probability=(
                    sums[event_id] / totals[event_id] if totals[event_id] else None
                ),
            )
            for event_id in event_ids
        }
