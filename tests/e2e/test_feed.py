"""E2E ленты событий для свайпа (`GET /events/feed`) против реального Postgres.

Проверяет то, что фейками не покрыть: настоящий SQL-агрегат сводки толпы
(``GROUP BY``) и keyset-пагинацию поверх реальных индексов/enum'ов.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.feed_reader import SqlAlchemyEventFeedReader
from app.modules.events.domain.value_objects import FeedCursor
from app.modules.events.ports.feed import FeedQuery
from app.modules.predictions.adapters.repository import SqlAlchemyPredictionRepository
from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction
from tests.e2e.helpers import OPENS_AT, add_category, add_open_event, add_user

pytestmark = pytest.mark.asyncio


async def test_feed_page_guest_and_user_exclude_predicted(session: AsyncSession) -> None:
    """Гость видит оба открытых события; пользователь A — без уже предсказанного."""
    category = await add_category(session)
    user_a = await add_user(session, username="alice")
    user_b = await add_user(session, username="bob")

    event_1 = await add_open_event(
        session, category_id=category.id, created_by=user_b.id, season_id=None
    )
    event_2 = await add_open_event(
        session, category_id=category.id, created_by=user_b.id, season_id=None
    )

    predictions = SqlAlchemyPredictionRepository(session)
    await predictions.add(
        Prediction.place(
            user_id=user_a.id,
            event_id=event_1.id,
            grade=ConfidenceGrade.PROBABLY_YES,
            now=OPENS_AT + timedelta(days=1),
        )
    )
    await predictions.add(
        Prediction.place(
            user_id=user_b.id,
            event_id=event_1.id,
            grade=ConfidenceGrade.DEFINITELY_YES,
            now=OPENS_AT + timedelta(days=1),
        )
    )
    await session.commit()

    reader = SqlAlchemyEventFeedReader(session)
    now = OPENS_AT + timedelta(days=2)

    guest_page = await reader.page(FeedQuery(now=now, limit=10))
    assert {item.event.id for item in guest_page} == {event_1.id, event_2.id}

    user_page = await reader.page(
        FeedQuery(now=now, limit=10, exclude_predicted_by=user_a.id)
    )
    assert {item.event.id for item in user_page} == {event_2.id}

    by_id = {item.event.id: item for item in guest_page}
    crowd_1 = by_id[event_1.id].crowd
    assert crowd_1.total_count == 2
    assert crowd_1.mean_probability == Decimal("0.80")  # (0.70 + 0.90) / 2
    assert crowd_1.distribution["probably_yes"] == 1
    assert crowd_1.distribution["definitely_yes"] == 1
    assert crowd_1.distribution["definitely_no"] == 0
    assert set(crowd_1.distribution.keys()) == {
        "definitely_no",
        "probably_no",
        "fifty_fifty",
        "probably_yes",
        "definitely_yes",
    }

    crowd_2 = by_id[event_2.id].crowd
    assert crowd_2.total_count == 0
    assert crowd_2.mean_probability is None


async def test_feed_pagination_by_cursor(session: AsyncSession) -> None:
    """``limit=1`` + курсор со второй страницы отдаёт второе (оставшееся) событие."""
    category = await add_category(session)
    user = await add_user(session, username="carol")
    event_1 = await add_open_event(
        session, category_id=category.id, created_by=user.id, season_id=None
    )
    event_2 = await add_open_event(
        session, category_id=category.id, created_by=user.id, season_id=None
    )
    await session.commit()

    reader = SqlAlchemyEventFeedReader(session)
    now = OPENS_AT + timedelta(days=2)

    first_page = await reader.page(FeedQuery(now=now, limit=1))
    assert len(first_page) == 1
    first_id = first_page[0].event.id

    cursor = FeedCursor(
        closes_at=first_page[0].event.window.closes_at, event_id=first_id
    )
    second_page = await reader.page(FeedQuery(now=now, limit=1, after=cursor))
    assert len(second_page) == 1
    second_id = second_page[0].event.id

    assert second_id != first_id
    assert {first_id, second_id} == {event_1.id, event_2.id}


async def test_feed_excludes_event_not_yet_open(session: AsyncSession) -> None:
    """``opens_at`` в будущем относительно ``now`` — событие в ленту не попадает."""
    category = await add_category(session)
    user = await add_user(session, username="dave")
    event = await add_open_event(
        session, category_id=category.id, created_by=user.id, season_id=None
    )
    await session.commit()

    reader = SqlAlchemyEventFeedReader(session)
    page = await reader.page(FeedQuery(now=OPENS_AT - timedelta(hours=1), limit=10))
    assert event.id not in {item.event.id for item in page}
