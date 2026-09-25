"""Интеграционные тесты HTTP-эндпоинта `GET /events/feed`.

Поднимает реальное FastAPI-приложение; I/O-порты (read-model ленты, часы) и
опциональный актор подменяются через ``dependency_overrides`` — без Postgres,
по образцу ``tests/events/integration/test_event_endpoints.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.events.api.dependencies import (
    get_clock,
    get_event_feed_reader,
    get_optional_actor,
)
from app.modules.events.application.dto import Actor
from app.modules.events.domain.entities import Event
from app.modules.events.domain.value_objects import EventWindow
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


def _open_event(
    category_id: uuid.UUID, *, opens_at: datetime, closes_at: datetime
) -> Event:
    """Опубликованное событие; момент публикации — ``opens_at`` (окно всегда валидно)."""
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
    event.publish(now=opens_at)
    return event


@pytest.fixture
def make_client(category):
    """Фабрика клиента с фейковой лентой; ``viewer=None`` — гость."""
    created: list[TestClient] = []

    def _build(*, viewer: Actor | None = None):
        events = InMemoryEventRepository()
        categories = InMemoryCategoryRepository()
        categories.seed(category)
        predictions = InMemoryPredictionRepository()
        feed_reader = InMemoryEventFeedReader(events, categories, predictions)

        app = create_app()
        app.dependency_overrides[get_event_feed_reader] = lambda: feed_reader
        app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)
        app.dependency_overrides[get_optional_actor] = lambda: viewer

        client = TestClient(app)
        created.append(client)
        return client, events, predictions

    yield _build
    for client in created:
        client.close()


def test_guest_gets_feed_page_shape(make_client, category) -> None:
    client, events, _ = make_client()
    events.seed(
        _open_event(
            category.id,
            opens_at=FIXED_NOW - timedelta(days=1),
            closes_at=FIXED_NOW + timedelta(days=1),
        )
    )
    resp = client.get("/events/feed")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"items", "next_cursor"}
    assert len(body["items"]) == 1
    assert body["next_cursor"] is None


def test_feed_route_not_shadowed_by_event_ref(make_client) -> None:
    """Доказательство порядка роутов: `/events/feed` не улетает в GetEvent → 404."""
    client, _, _ = make_client()
    resp = client.get("/events/feed")
    assert resp.status_code == 200


@pytest.mark.parametrize("limit", [0, 51])
def test_limit_out_of_range_is_422(make_client, limit: int) -> None:
    client, _, _ = make_client()
    resp = client.get("/events/feed", params={"limit": limit})
    assert resp.status_code == 422


def test_garbage_cursor_is_400(make_client) -> None:
    client, _, _ = make_client()
    resp = client.get("/events/feed", params={"cursor": "garbage"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "InvalidFeedCursorError"


def test_empty_cursor_is_treated_as_absent(make_client, category) -> None:
    """Пустая строка в ``cursor`` — не мусор, а его отсутствие: та же первая страница."""
    client, events, _ = make_client()
    events.seed(
        _open_event(
            category.id,
            opens_at=FIXED_NOW - timedelta(days=1),
            closes_at=FIXED_NOW + timedelta(days=1),
        )
    )
    resp_without_cursor = client.get("/events/feed")
    resp_empty_cursor = client.get("/events/feed", params={"cursor": ""})
    assert resp_empty_cursor.status_code == 200
    assert resp_empty_cursor.json() == resp_without_cursor.json()


def test_user_does_not_see_already_predicted_event(make_client, category) -> None:
    viewer = Actor(user_id=uuid.uuid4(), role=UserRole.USER)
    client, events, predictions = make_client(viewer=viewer)
    event = _open_event(
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
    resp = client.get("/events/feed")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_crowd_distribution_and_category_shape(make_client, category) -> None:
    client, events, predictions = make_client()
    event = _open_event(
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
    resp = client.get("/events/feed")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert set(item["crowd"]["distribution"].keys()) == {
        "definitely_no",
        "probably_no",
        "fifty_fifty",
        "probably_yes",
        "definitely_yes",
    }
    assert item["crowd"]["mean_probability"] == "0.70"
    assert item["crowd"]["total_count"] == 1
    assert item["category"]["slug"] == category.slug
    assert item["category"]["title"] == category.title


def test_empty_page_when_nothing_open(make_client) -> None:
    client, _, _ = make_client()
    resp = client.get("/events/feed")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}
