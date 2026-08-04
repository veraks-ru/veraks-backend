"""Интеграционные тесты HTTP-эндпоинтов `/events` и `/categories`.

Поднимают реальное FastAPI-приложение, но I/O-порты (репозитории, часы) и
аутентификацию подменяют фейками/оверрайдами через ``dependency_overrides``.
БД-интеграция с Postgres (UNIQUE, enum, CHECK окна) покрывается отдельно.

TODO(events-infra): добавить e2e против реального Postgres (testcontainers)
для проверки FK на users/categories, enum event_status и CHECK-констрейнтов
временного окна.
"""

from __future__ import annotations

import asyncio
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
    get_lock_event_predictions,
    get_notifier,
    get_optional_actor,
    get_recompute_ratings,
    get_subscription_gate,
    get_void_event_disputes,
)
from app.modules.events.application.dto import Actor
from app.modules.events.domain.entities import Category, Event, EventStatus
from app.modules.events.domain.value_objects import EventWindow
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from app.modules.predictions.application.use_cases import LockEventPredictions
from app.modules.resolutions.application.use_cases import VoidEventDisputes
from app.modules.resolutions.domain.entities import Dispute, DisputeStatus
from app.modules.scoring.application.use_cases import RecomputeRatings
from tests.events.conftest import FIXED_NOW
from tests.events.fakes import (
    FakeAuditTrail,
    FakeClock,
    FakeSubscriptionGate,
    InMemoryCategoryRepository,
    InMemoryEventRepository,
)
from tests.predictions.fakes import InMemoryPredictionRepository
from tests.resolutions.fakes import (
    FakeAuditTrail as ResolutionsFakeAuditTrail,
    FakeClock as ResolutionsFakeClock,
    InMemoryDisputeRepository,
)
from tests.scoring.fakes import (
    FakeClock as ScoringFakeClock,
    FakeEventScoringGateway,
    FakeSeasonConfigGateway,
    InMemoryRatingRepository,
)


class _NullNotifier:
    """Нотификатор-заглушка для интеграционных тестов events (без БД/сети)."""

    async def emit(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        return None


def _fake_user(role: UserRole) -> User:
    """Минимальный аутентифицированный пользователь с заданной ролью."""
    return User(
        esia_oid_hash="oid",
        snils_hash="hash",
        username="editor1",
        display_name="Редактор",
        real_name_enc=None,
        role=role,
    )


@pytest.fixture
def make_client(category: Category, restricted_category: Category):
    """Фабрика клиента: настраивает роль актора и общие фейки.

    ``role=None`` оставляет реальную аутентификацию (для проверки 401).
    Возвращает ``(client, event_repo, category_repo, dispute_repo)``.
    """
    created: list = []

    def _build(role: UserRole | None = UserRole.EDITOR):
        event_repo = InMemoryEventRepository()
        category_repo = InMemoryCategoryRepository()
        dispute_repo = InMemoryDisputeRepository()
        category_repo.seed(category)
        category_repo.seed(restricted_category)

        app = create_app()
        app.dependency_overrides[get_event_repository] = lambda: event_repo
        app.dependency_overrides[get_category_repository] = lambda: category_repo
        app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)
        app.dependency_overrides[get_audit_trail] = lambda: FakeAuditTrail()
        app.dependency_overrides[get_notifier] = lambda: _NullNotifier()
        app.dependency_overrides[get_subscription_gate] = lambda: FakeSubscriptionGate(
            active=True
        )
        app.dependency_overrides[get_lock_event_predictions] = (
            lambda: LockEventPredictions(
                predictions=InMemoryPredictionRepository(),
                clock=FakeClock(FIXED_NOW),
            )
        )
        # Снятие споров и пересчёт рейтингов после аннулирования — на фейках
        # портов соседних доменов (без Postgres), чтобы проверялась именно
        # связка роутера. Репозиторий споров общий на клиент: тест кладёт в
        # него спор и проверяет, что тот закрылся.
        app.dependency_overrides[get_void_event_disputes] = lambda: VoidEventDisputes(
            disputes=dispute_repo,
            audit=ResolutionsFakeAuditTrail(),
            clock=ResolutionsFakeClock(FIXED_NOW),
        )
        app.dependency_overrides[get_recompute_ratings] = lambda: RecomputeRatings(
            gateway=FakeEventScoringGateway(),
            ratings=InMemoryRatingRepository(),
            clock=ScoringFakeClock(FIXED_NOW),
            season_config=FakeSeasonConfigGateway(),
        )
        if role is not None:
            user = _fake_user(role)
            app.dependency_overrides[get_current_user] = lambda: user
            # Публичные GET используют опциональную авторизацию — тот же актор.
            app.dependency_overrides[get_optional_actor] = lambda: Actor(
                user_id=user.id, role=user.role
            )

        client = TestClient(app)
        created.append(client)
        return client, event_repo, category_repo, dispute_repo

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
    client, _, _, _ = make_client(role=None)
    resp = client.post("/events", json=_event_payload(uuid.uuid4()))
    assert resp.status_code == 401


def test_create_event_forbidden_for_user(make_client, category) -> None:
    client, _, _, _ = make_client(role=UserRole.USER)
    resp = client.post("/events", json=_event_payload(category.id))
    assert resp.status_code == 403


def test_create_and_get_event(make_client, category) -> None:
    client, _, _, _ = make_client()
    created = client.post("/events", json=_event_payload(category.id))
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "draft"
    assert body["created_by"]

    fetched = client.get(f"/events/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == body["title"]


def test_create_event_unknown_category_404(make_client) -> None:
    client, _, _, _ = make_client()
    resp = client.post("/events", json=_event_payload(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["error"] == "CategoryNotFoundError"


def test_create_event_restricted_category_422(make_client, restricted_category) -> None:
    """PRD §7.5: создание события в запрещённой категории отклоняется."""
    client, _, _, _ = make_client()
    resp = client.post("/events", json=_event_payload(restricted_category.id))
    assert resp.status_code == 422
    assert resp.json()["error"] == "RestrictedCategoryError"


def test_propose_event_restricted_category_422(make_client, restricted_category) -> None:
    """PRD §7.5: пользователь не может предложить событие в запрещённой категории."""
    client, _, _, _ = make_client(role=UserRole.USER)
    resp = client.post("/events/propose", json=_event_payload(restricted_category.id))
    assert resp.status_code == 422
    assert resp.json()["error"] == "RestrictedCategoryError"


def test_create_event_invalid_window_400(make_client, category) -> None:
    client, _, _, _ = make_client()
    payload = _event_payload(
        category.id,
        opens_at=(FIXED_NOW + timedelta(days=30)).isoformat(),
        closes_at=(FIXED_NOW + timedelta(days=1)).isoformat(),
    )
    resp = client.post("/events", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "InvalidEventWindowError"


def test_lifecycle_publish_close(make_client, category) -> None:
    client, _, _, _ = make_client()
    event_id = client.post("/events", json=_event_payload(category.id)).json()["id"]

    published = client.post(f"/events/{event_id}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "open"

    closed = client.post(f"/events/{event_id}/close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"


def test_invalid_transition_conflict(make_client, category) -> None:
    client, _, _, _ = make_client()
    event_id = client.post("/events", json=_event_payload(category.id)).json()["id"]
    # Нельзя закрыть черновик (draft → closed запрещён).
    resp = client.post(f"/events/{event_id}/close")
    assert resp.status_code == 409
    assert resp.json()["error"] == "InvalidEventTransitionError"


def test_patch_locks_conditions_after_publish(make_client, category) -> None:
    """Ст. 1058 ГК РФ: условия опубликованного события неизменны."""
    client, _, _, _ = make_client()
    event_id = client.post("/events", json=_event_payload(category.id)).json()["id"]
    client.post(f"/events/{event_id}/publish")

    # Формулировка после публикации заблокирована → 409.
    locked_title = client.patch(
        f"/events/{event_id}", json={"title": "Уточнённый заголовок"}
    )
    assert locked_title.status_code == 409
    assert locked_title.json()["error"] == "EventEditNotAllowedError"

    # Критерий/источник разрешения тоже заблокированы.
    locked_criteria = client.patch(
        f"/events/{event_id}", json={"resolution_criteria": "Другой критерий"}
    )
    assert locked_criteria.status_code == 409

    # Окно после публикации заблокировано → 409.
    locked_window = client.patch(
        f"/events/{event_id}",
        json={
            "opens_at": (FIXED_NOW + timedelta(days=2)).isoformat(),
            "closes_at": (FIXED_NOW + timedelta(days=40)).isoformat(),
            "resolves_at": (FIXED_NOW + timedelta(days=41)).isoformat(),
        },
    )
    assert locked_window.status_code == 409


def test_list_events_filters_by_status(make_client, category) -> None:
    client, _, _, _ = make_client()
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
    client, _, _, _ = make_client()
    listed = client.get("/categories")
    assert listed.status_code == 200
    politics = next(c for c in listed.json() if c["slug"] == "politics")
    assert politics["is_restricted"] is False

    created = client.post(
        "/categories", json={"slug": "sport", "title": "Спорт"}
    )
    assert created.status_code == 201
    assert created.json()["slug"] == "sport"
    assert created.json()["is_restricted"] is False


def test_categories_list_carries_is_restricted_flag(
    make_client, restricted_category
) -> None:
    """Флаг запрещённой категории отдаётся в списке — фронт скрывает/дизейблит её."""
    client, _, _, _ = make_client()
    listed = client.get("/categories")
    restricted = next(
        c for c in listed.json() if c["slug"] == restricted_category.slug
    )
    assert restricted["is_restricted"] is True


def test_create_category_with_is_restricted_flag(make_client) -> None:
    client, _, _, _ = make_client()
    created = client.post(
        "/categories",
        json={"slug": "health", "title": "Здоровье", "is_restricted": True},
    )
    assert created.status_code == 201
    assert created.json()["is_restricted"] is True


def test_create_category_slug_conflict_409(make_client) -> None:
    client, _, _, _ = make_client()
    resp = client.post("/categories", json={"slug": "politics", "title": "Дубль"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "CategorySlugTakenError"


def test_get_missing_event_404(make_client) -> None:
    client, _, _, _ = make_client()
    resp = client.get(f"/events/{uuid.uuid4()}")
    assert resp.status_code == 404


def _seed_resolved_event(repo: InMemoryEventRepository, category: Category) -> Event:
    """Кладёт в репозиторий событие в статусе ``resolved`` (с исходом)."""
    event = Event.create_draft(
        title="Разрешённое событие",
        description="Исход зафиксирован",
        category_id=category.id,
        created_by=uuid.uuid4(),
        window=EventWindow(
            opens_at=FIXED_NOW + timedelta(days=1),
            closes_at=FIXED_NOW + timedelta(days=30),
            resolves_at=FIXED_NOW + timedelta(days=31),
        ),
        resolution_source="https://source.example",
        resolution_criteria="Официальное подтверждение",
        now=FIXED_NOW,
    )
    event.publish(now=FIXED_NOW)
    event.close(now=FIXED_NOW)
    event.begin_resolution(now=FIXED_NOW)
    event.record_outcome(
        outcome=True,
        dispute_window_ends_at=FIXED_NOW + timedelta(days=32),
        now=FIXED_NOW,
    )
    return repo.seed(event)


def test_annul_event_by_arbiter(make_client, category) -> None:
    """Арбитр аннулирует разрешённое событие; статус отдаётся в ответе."""
    client, repo, _, _ = make_client(role=UserRole.ARBITER)
    event = _seed_resolved_event(repo, category)

    resp = client.post(
        f"/events/{event.id}/annul", json={"reason": "Двусмысленная формулировка"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "annulled"

    # Статус виден и в обычном чтении события.
    assert client.get(f"/events/{event.id}").json()["status"] == "annulled"


def test_annul_disputed_event_voids_its_dispute(make_client, category) -> None:
    """Аннулирование ``disputed``-события снимает открытый спор в том же запросе.

    Иначе спор остался бы открытым навсегда: решить его нельзя (обе ветки ведут
    через запрещённый переход ``annulled → resolved``), а он вечно блокировал бы
    финализацию сезона.
    """
    client, repo, _, disputes = make_client(role=UserRole.ARBITER)
    event = _seed_resolved_event(repo, category)
    event.open_dispute(now=FIXED_NOW)
    repo.seed(event)
    dispute = Dispute.open_for(
        event_id=event.id,
        resolution_id=uuid.uuid4(),
        raised_by=uuid.uuid4(),
        reason="Источник противоречит формулировке",
        now=FIXED_NOW,
    )
    asyncio.run(disputes.add(dispute))

    resp = client.post(f"/events/{event.id}/annul", json={"reason": "Спор неразрешим"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "annulled"

    stored = asyncio.run(disputes.get_by_id(dispute.id))
    assert stored is not None
    assert stored.status is DisputeStatus.VOIDED
    assert stored.is_open() is False
    assert stored.decision_notes == "Спор неразрешим"
    assert asyncio.run(disputes.has_open_for_event(event.id)) is False


def test_annul_event_forbidden_for_editor(make_client, category) -> None:
    client, repo, _, _ = make_client(role=UserRole.EDITOR)
    event = _seed_resolved_event(repo, category)
    resp = client.post(f"/events/{event.id}/annul", json={"reason": "Причина"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "EventPermissionError"


def test_annul_event_requires_reason(make_client, category) -> None:
    """Пустая причина отсекается схемой запроса (422)."""
    client, repo, _, _ = make_client(role=UserRole.ARBITER)
    event = _seed_resolved_event(repo, category)
    assert client.post(f"/events/{event.id}/annul", json={"reason": ""}).status_code == 422
    assert client.post(f"/events/{event.id}/annul", json={}).status_code == 422


def test_annul_event_wrong_status_conflict(make_client, category) -> None:
    """Черновик аннулировать нельзя — только отменить (cancelled)."""
    client, repo, _, _ = make_client(role=UserRole.ARBITER)
    event = _seed_resolved_event(repo, category)
    event.annul(reason="Первое аннулирование", now=FIXED_NOW)
    repo.seed(event)
    assert event.status is EventStatus.ANNULLED

    resp = client.post(f"/events/{event.id}/annul", json={"reason": "Повторно"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "InvalidEventTransitionError"


def test_annul_missing_event_404(make_client) -> None:
    client, _, _, _ = make_client(role=UserRole.ARBITER)
    resp = client.post(f"/events/{uuid.uuid4()}/annul", json={"reason": "Причина"})
    assert resp.status_code == 404


def test_default_clock_is_system_clock() -> None:
    """Дефолтный провайдер часов — системные (UTC)."""
    assert isinstance(get_clock(), SystemClock)
