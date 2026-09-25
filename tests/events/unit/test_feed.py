"""Юнит-тесты ленты событий для свайпа: курсор и use-case ``ListEventFeed``.

Курсор проверяется в изоляции (round-trip и разбор мусора); use-case — через
``InMemoryEventFeedReader`` и ``FakeClock``, без FastAPI и без Postgres.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta

import pytest

from app.modules.events.application.dto import Actor
from app.modules.events.application.use_cases import ListEventFeed
from app.modules.events.domain.entities import Category, Event
from app.modules.events.domain.errors import InvalidFeedCursorError
from app.modules.events.domain.value_objects import EventWindow, FeedCursor
from app.modules.identity.domain.entities import UserRole
from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction
from tests.events.conftest import FIXED_NOW
from tests.events.fakes import (
    FakeClock,
    InMemoryCategoryRepository,
    InMemoryEventFeedReader,
    InMemoryEventRepository,
)
from tests.predictions.fakes import InMemoryPredictionRepository


def _event(
    category_id: uuid.UUID,
    *,
    opens_at: datetime,
    closes_at: datetime,
    event_id: uuid.UUID | None = None,
) -> Event:
    """Опубликованное (``open``) событие с заданным окном.

    Момент публикации берём равным ``opens_at`` — окно всегда валидно
    (``opens_at < closes_at``), поэтому ``publish()`` проходит независимо от
    того, где лежит окно относительно ``FIXED_NOW`` в конкретном тесте.
    """
    event = Event.create_draft(
        title="Будет ли X к концу года?",
        description="Подробности события",
        category_id=category_id,
        created_by=uuid.uuid4(),
        window=EventWindow(
            opens_at=opens_at,
            closes_at=closes_at,
            resolves_at=closes_at + timedelta(days=1),
        ),
        resolution_source="https://source.example",
        resolution_criteria="Официальное подтверждение",
        now=opens_at,
    )
    if event_id is not None:
        event.id = event_id
    event.publish(now=opens_at)
    return event


# ── FeedCursor ───────────────────────────────────────────────────────────


def test_feed_cursor_round_trip() -> None:
    cursor = FeedCursor(closes_at=FIXED_NOW, event_id=uuid.uuid4())
    encoded = cursor.encode()
    assert "=" not in encoded
    assert FeedCursor.decode(encoded) == cursor


def test_feed_cursor_decode_garbage_raises() -> None:
    with pytest.raises(InvalidFeedCursorError):
        FeedCursor.decode("garbage")


def test_feed_cursor_decode_wrong_part_count_raises() -> None:
    raw = base64.urlsafe_b64encode(b"a|b|c").decode("ascii").rstrip("=")
    with pytest.raises(InvalidFeedCursorError):
        FeedCursor.decode(raw)


def test_feed_cursor_decode_naive_datetime_raises() -> None:
    naive = datetime(2026, 1, 1).isoformat()  # noqa: DTZ001 — намеренно наивная дата
    raw = base64.urlsafe_b64encode(f"{naive}|{uuid.uuid4()}".encode()).decode("ascii").rstrip("=")
    with pytest.raises(InvalidFeedCursorError):
        FeedCursor.decode(raw)


def test_feed_cursor_decode_bad_uuid_raises() -> None:
    raw = (
        base64.urlsafe_b64encode(f"{FIXED_NOW.isoformat()}|not-a-uuid".encode())
        .decode("ascii")
        .rstrip("=")
    )
    with pytest.raises(InvalidFeedCursorError):
        FeedCursor.decode(raw)


# ── ListEventFeed ────────────────────────────────────────────────────────


@pytest.fixture
def events() -> InMemoryEventRepository:
    return InMemoryEventRepository()


@pytest.fixture
def categories(category) -> InMemoryCategoryRepository:
    repo = InMemoryCategoryRepository()
    repo.seed(category)
    return repo


@pytest.fixture
def predictions() -> InMemoryPredictionRepository:
    return InMemoryPredictionRepository()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FIXED_NOW)


@pytest.fixture
def feed(events, categories, predictions) -> InMemoryEventFeedReader:
    return InMemoryEventFeedReader(events, categories, predictions)


@pytest.fixture
def use_case(feed, clock) -> ListEventFeed:
    return ListEventFeed(feed=feed, clock=clock)


async def test_excludes_open_event_with_future_opens_at(use_case, events, category) -> None:
    """Правило §3 п.1: ``opens_at`` в будущем — приём ещё не открыт."""
    events.seed(
        _event(
            category.id,
            opens_at=FIXED_NOW + timedelta(hours=1),
            closes_at=FIXED_NOW + timedelta(days=1),
        )
    )
    page = await use_case.execute(viewer=None, limit=20, category_id=None, cursor=None)
    assert page.items == []


async def test_excludes_open_event_past_closes_at(use_case, events, category) -> None:
    """closes_at уже прошёл, но статус ещё ``open`` — крон не добежал (§3 п.1)."""
    events.seed(
        _event(
            category.id,
            opens_at=FIXED_NOW - timedelta(days=2),
            closes_at=FIXED_NOW - timedelta(hours=1),
        )
    )
    page = await use_case.execute(viewer=None, limit=20, category_id=None, cursor=None)
    assert page.items == []


async def test_excludes_event_with_closes_at_exactly_now(use_case, events, category) -> None:
    """Граница ``closes_at > now`` строгая: ``closes_at == now`` уже не в ленте."""
    events.seed(
        _event(
            category.id,
            opens_at=FIXED_NOW - timedelta(days=1),
            closes_at=FIXED_NOW,
        )
    )
    page = await use_case.execute(viewer=None, limit=20, category_id=None, cursor=None)
    assert page.items == []


async def test_includes_event_with_opens_at_exactly_now(use_case, events, category) -> None:
    """Граница ``opens_at <= now`` нестрогая: ``opens_at == now`` уже в ленте."""
    event = _event(
        category.id,
        opens_at=FIXED_NOW,
        closes_at=FIXED_NOW + timedelta(days=1),
    )
    events.seed(event)
    page = await use_case.execute(viewer=None, limit=20, category_id=None, cursor=None)
    assert [item.event.id for item in page.items] == [event.id]


async def test_excludes_non_open_status(use_case, events, category) -> None:
    event = _event(
        category.id,
        opens_at=FIXED_NOW - timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=1),
    )
    event.close(now=FIXED_NOW)
    events.seed(event)
    page = await use_case.execute(viewer=None, limit=20, category_id=None, cursor=None)
    assert page.items == []


async def test_includes_open_event_within_window(use_case, events, category) -> None:
    event = _event(
        category.id,
        opens_at=FIXED_NOW - timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=1),
    )
    events.seed(event)
    page = await use_case.execute(viewer=None, limit=20, category_id=None, cursor=None)
    assert [item.event.id for item in page.items] == [event.id]
    assert page.items[0].category.id == category.id


async def test_excludes_events_already_predicted_by_viewer(
    use_case, events, predictions, category
) -> None:
    viewer = Actor(user_id=uuid.uuid4(), role=UserRole.USER)
    event = _event(
        category.id,
        opens_at=FIXED_NOW - timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=1),
    )
    events.seed(event)
    predictions.seed(
        Prediction.place(
            user_id=viewer.user_id,
            event_id=event.id,
            grade=ConfidenceGrade.PROBABLY_YES,
            now=FIXED_NOW,
        )
    )
    page = await use_case.execute(viewer=viewer, limit=20, category_id=None, cursor=None)
    assert page.items == []


async def test_guest_sees_events_regardless_of_predictions(
    use_case, events, predictions, category
) -> None:
    event = _event(
        category.id,
        opens_at=FIXED_NOW - timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=1),
    )
    events.seed(event)
    predictions.seed(
        Prediction.place(
            user_id=uuid.uuid4(),
            event_id=event.id,
            grade=ConfidenceGrade.PROBABLY_YES,
            now=FIXED_NOW,
        )
    )
    page = await use_case.execute(viewer=None, limit=20, category_id=None, cursor=None)
    assert [item.event.id for item in page.items] == [event.id]


async def test_category_filter_matches_exactly(
    use_case, events, categories, category
) -> None:
    other_category = Category.create(slug="sports", title="Спорт")
    categories.seed(other_category)
    matching = _event(
        category.id,
        opens_at=FIXED_NOW - timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=1),
    )
    other = _event(
        other_category.id,
        opens_at=FIXED_NOW - timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=2),
    )
    events.seed(matching)
    events.seed(other)
    page = await use_case.execute(
        viewer=None, limit=20, category_id=category.id, cursor=None
    )
    assert [item.event.id for item in page.items] == [matching.id]


async def test_next_cursor_none_when_fewer_than_limit(use_case, events, category) -> None:
    events.seed(
        _event(
            category.id,
            opens_at=FIXED_NOW - timedelta(days=1),
            closes_at=FIXED_NOW + timedelta(days=1),
        )
    )
    page = await use_case.execute(viewer=None, limit=20, category_id=None, cursor=None)
    assert page.next_cursor is None


async def test_next_cursor_none_when_exactly_limit(use_case, events, category) -> None:
    """Классический офф-бай-один: ровно ``limit`` подходящих событий — это ещё не «есть ещё».

    Use-case запрашивает у ридера ``limit + 1``, чтобы отличить эту ситуацию
    от «дальше есть что грузить»; здесь ридер отдаёт ровно ``limit`` строк, и
    страница должна вернуть все их без намёка на следующую.
    """
    limit = 3
    ids = [uuid.UUID(int=i) for i in range(1, limit + 1)]
    for offset, event_id in enumerate(ids):
        events.seed(
            _event(
                category.id,
                opens_at=FIXED_NOW - timedelta(days=1),
                closes_at=FIXED_NOW + timedelta(days=1, hours=offset),
                event_id=event_id,
            )
        )
    page = await use_case.execute(viewer=None, limit=limit, category_id=None, cursor=None)
    assert [item.event.id for item in page.items] == ids
    assert page.next_cursor is None


async def test_second_page_continues_without_duplicates_on_tied_closes_at(
    use_case, events, category
) -> None:
    """Три события с одинаковым ``closes_at`` — tie-break по ``id``, без дублей/пропусков."""
    tied_closes_at = FIXED_NOW + timedelta(days=1)
    ids = [uuid.UUID(int=i) for i in (1, 2, 3)]
    for event_id in ids:
        events.seed(
            _event(
                category.id,
                opens_at=FIXED_NOW - timedelta(days=1),
                closes_at=tied_closes_at,
                event_id=event_id,
            )
        )

    first_page = await use_case.execute(viewer=None, limit=2, category_id=None, cursor=None)
    assert [item.event.id for item in first_page.items] == ids[:2]
    assert first_page.next_cursor is not None

    second_page = await use_case.execute(
        viewer=None, limit=2, category_id=None, cursor=first_page.next_cursor
    )
    assert [item.event.id for item in second_page.items] == ids[2:]
    assert second_page.next_cursor is None
