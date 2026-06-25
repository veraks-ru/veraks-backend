"""Интеграционные тесты HTTP-эндпоинтов `/events` и `/categories`.

Поднимают реальное FastAPI-приложение, но I/O-порты (репозитории, часы) и
аутентификацию подменяют фейками/оверрайдами через ``dependency_overrides``.
БД-интеграция с Postgres (UNIQUE, enum, CHECK окна) покрывается отдельно.

TODO(events-infra): добавить e2e против реального Postgres (testcontainers)
для проверки FK на users/categories, enum event_status и CHECK-констрейнтов
временного окна.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.events.adapters.clock import SystemClock
from app.modules.events.api.dependencies import (
    get_audit_trail,
    get_category_repository,
    get_clock,
    get_event_repository,
)
from app.modules.events.domain.entities import Category
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from tests.events.conftest import FIXED_NOW
from tests.events.fakes import (
    FakeAuditTrail,
    FakeClock,
    InMemoryCategoryRepository,
    InMemoryEventRepository,
)


def _fake_user(role: UserRole) -> User:
    """Минимальный аутентифицированный пользователь с заданной ролью."""
    return User(
        esia_oid="oid",
        snils_hash="hash",
        username="editor1",
        display_name="Редактор",
        real_name_enc=None,
        role=role,
    )


@pytest.fixture
def make_client(category: Category):
    """Фабрика клиента: настраивает роль актора и общие фейки.

    ``role=None`` оставляет реальную аутентификацию (для проверки 401).
    Возвращает ``(client, event_repo, category_repo)``.
    """
    created: list = []

    def _build(role: UserRole | None = UserRole.EDITOR):
        event_repo = InMemoryEventRepository()
        category_repo = InMemoryCategoryRepository()
        category_repo.seed(category)

        app = create_app()
        app.dependency_overrides[get_event_repository] = lambda: event_repo
        app.dependency_overrides[get_category_repository] = lambda: category_repo
        app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)
        app.dependency_overrides[get_audit_trail] = lambda: FakeAuditTrail()
        if role is not None:
            user = _fake_user(role)
            app.dependency_overrides[get_current_user] = lambda: user

        client = TestClient(app)
        created.append(client)
        return client, event_repo, category_repo

    yield _build
    for client in created:
        client.close()


def _event_payload(category_id: uuid.UUID, **over) -> dict:
    base = {
        "title": "Будет ли X к концу года?",
        "description": "Подробности события",
        "category_id": str(category_id),
        "opens_at": (FIXED_NOW + timedelta(days=1)).isoformat(),
        "closes_at": (FIXED_NOW + timedelta(days=30)).isoformat(),
        "resolves_at": (FIXED_NOW + timedelta(days=31)).isoformat(),
        "resolution_source": "https://source.example",
        "resolution_criteria": "Официальное подтверждение",
    }
    base.update(over)
    return base


def test_create_event_requires_auth(make_client) -> None:
    client, _, _ = make_client(role=None)
    resp = client.post("/events", json=_event_payload(uuid.uuid4()))
    assert resp.status_code == 401


def test_create_event_forbidden_for_user(make_client, category) -> None:
    client, _, _ = make_client(role=UserRole.USER)
    resp = client.post("/events", json=_event_payload(category.id))
    assert resp.status_code == 403


def test_create_and_get_event(make_client, category) -> None:
    client, _, _ = make_client()
    created = client.post("/events", json=_event_payload(category.id))
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "draft"
    assert body["created_by"]

    fetched = client.get(f"/events/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == body["title"]


def test_create_event_unknown_category_404(make_client) -> None:
    client, _, _ = make_client()
    resp = client.post("/events", json=_event_payload(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["error"] == "CategoryNotFoundError"


def test_create_event_invalid_window_400(make_client, category) -> None:
    client, _, _ = make_client()
    payload = _event_payload(
        category.id,
        opens_at=(FIXED_NOW + timedelta(days=30)).isoformat(),
        closes_at=(FIXED_NOW + timedelta(days=1)).isoformat(),
    )
    resp = client.post("/events", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "InvalidEventWindowError"


def test_lifecycle_publish_close(make_client, category) -> None:
    client, _, _ = make_client()
    event_id = client.post("/events", json=_event_payload(category.id)).json()["id"]

    published = client.post(f"/events/{event_id}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "open"

    closed = client.post(f"/events/{event_id}/close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"


def test_invalid_transition_conflict(make_client, category) -> None:
    client, _, _ = make_client()
    event_id = client.post("/events", json=_event_payload(category.id)).json()["id"]
    # Нельзя закрыть черновик (draft → closed запрещён).
    resp = client.post(f"/events/{event_id}/close")
    assert resp.status_code == 409
    assert resp.json()["error"] == "InvalidEventTransitionError"


def test_patch_locks_window_after_publish(make_client, category) -> None:
    client, _, _ = make_client()
    event_id = client.post("/events", json=_event_payload(category.id)).json()["id"]
    client.post(f"/events/{event_id}/publish")

    # Заголовок правится в open.
    ok = client.patch(f"/events/{event_id}", json={"title": "Уточнённый заголовок"})
    assert ok.status_code == 200
    assert ok.json()["title"] == "Уточнённый заголовок"

    # Окно после публикации заблокировано → 409.
    locked = client.patch(
        f"/events/{event_id}",
        json={
            "opens_at": (FIXED_NOW + timedelta(days=2)).isoformat(),
            "closes_at": (FIXED_NOW + timedelta(days=40)).isoformat(),
            "resolves_at": (FIXED_NOW + timedelta(days=41)).isoformat(),
        },
    )
    assert locked.status_code == 409


def test_list_events_filters_by_status(make_client, category) -> None:
    client, _, _ = make_client()
    a = client.post("/events", json=_event_payload(category.id)).json()["id"]
    client.post("/events", json=_event_payload(category.id))
    client.post(f"/events/{a}/publish")

    draft = client.get("/events", params={"status": "draft"})
    assert draft.status_code == 200
    assert all(e["status"] == "draft" for e in draft.json())
    assert len(draft.json()) == 1

    opened = client.get("/events", params={"status": "open"})
    assert len(opened.json()) == 1


def test_categories_list_and_create(make_client) -> None:
    client, _, _ = make_client()
    listed = client.get("/categories")
    assert listed.status_code == 200
    assert any(c["slug"] == "politics" for c in listed.json())

    created = client.post(
        "/categories", json={"slug": "sport", "title": "Спорт"}
    )
    assert created.status_code == 201
    assert created.json()["slug"] == "sport"


def test_create_category_slug_conflict_409(make_client) -> None:
    client, _, _ = make_client()
    resp = client.post("/categories", json={"slug": "politics", "title": "Дубль"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "CategorySlugTakenError"


def test_get_missing_event_404(make_client) -> None:
    client, _, _ = make_client()
    resp = client.get(f"/events/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_default_clock_is_system_clock() -> None:
    """Дефолтный провайдер часов — системные (UTC)."""
    assert isinstance(get_clock(), SystemClock)
