"""Интеграционные тесты HTTP-эндпоинтов scoring.

Поднимают реальное FastAPI-приложение, но I/O-порты (шлюз, писатель,
репозиторий рейтингов, часы) и аутентификацию подменяют фейками через
``dependency_overrides``. БД-интеграция с Postgres (UNIQUE области, enum
rating_scope, FK) — отдельным e2e (TODO).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from app.modules.scoring.api.dependencies import (
    get_clock,
    get_event_scoring_gateway,
    get_prediction_score_writer,
    get_rating_repository,
)
from app.modules.scoring.application.dto import EventScoringStatus
from app.modules.scoring.application.use_cases import RecomputeRatings
from app.modules.scoring.domain.entities import ScopeType
from tests.scoring.conftest import FIXED_NOW, make_event
from tests.scoring.fakes import (
    FakeClock,
    FakeEventScoringGateway,
    FakePredictionScoreWriter,
    InMemoryRatingRepository,
)


def _user(role: UserRole = UserRole.USER) -> User:
    return User(
        esia_oid="oid",
        snils_hash="hash",
        username="predictor1",
        display_name="Предсказатель",
        real_name_enc=None,
        role=role,
    )


@pytest.fixture
def make_client():
    """Фабрика клиента: общие фейки + управляемая роль/аутентификация."""
    created: list[TestClient] = []

    def _build(
        *,
        gateway: FakeEventScoringGateway | None = None,
        repo: InMemoryRatingRepository | None = None,
        writer: FakePredictionScoreWriter | None = None,
        role: UserRole | None = UserRole.USER,
    ):
        gateway = gateway or FakeEventScoringGateway()
        repo = repo or InMemoryRatingRepository()
        writer = writer or FakePredictionScoreWriter()

        app = create_app()
        app.dependency_overrides[get_event_scoring_gateway] = lambda: gateway
        app.dependency_overrides[get_prediction_score_writer] = lambda: writer
        app.dependency_overrides[get_rating_repository] = lambda: repo
        app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)
        if role is not None:
            app.dependency_overrides[get_current_user] = lambda: _user(role)

        client = TestClient(app)
        created.append(client)
        return client, gateway, repo, writer

    yield _build
    for client in created:
        client.close()


async def _seed_ratings(repo: InMemoryRatingRepository) -> tuple[uuid.UUID, list]:
    category_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    event, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        category_id=category_id,
        user_ids=ids,
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    await RecomputeRatings(
        gateway=gateway, ratings=repo, clock=FakeClock(FIXED_NOW)
    ).execute()
    return category_id, ids


# ── Лидерборды ──────────────────────────────────────────────────────────────


async def test_global_leaderboard_returns_ranked_entries(make_client) -> None:
    repo = InMemoryRatingRepository()
    _, ids = await _seed_ratings(repo)
    client, _, _, _ = make_client(repo=repo)

    resp = client.get("/leaderboards/global")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_type"] == "global"
    assert body["entries"][0]["rank"] == 1
    assert body["entries"][0]["user_id"] == str(ids[4])  # игрок с 0.3 — №1


async def test_category_leaderboard_scoped(make_client) -> None:
    repo = InMemoryRatingRepository()
    category_id, _ = await _seed_ratings(repo)
    client, _, _, _ = make_client(repo=repo)

    resp = client.get(f"/leaderboards/categories/{category_id}?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_type"] == "category"
    assert len(body["entries"]) == 3


# ── Калибровка ──────────────────────────────────────────────────────────────


def test_user_calibration_endpoint(make_client) -> None:
    user_id = uuid.uuid4()
    entries = [(0.70, 1)] * 31 + [(0.70, 0)] * 9
    gateway = FakeEventScoringGateway(user_entries={user_id: entries})
    client, _, _, _ = make_client(gateway=gateway)

    resp = client.get(f"/users/{user_id}/calibration")
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_total"] == 40
    assert body["bins"][0]["frequency"] == pytest.approx(0.775, abs=1e-4)
    assert body["ece"] == pytest.approx(0.075, abs=1e-4)


# ── Скоринг события (RBAC) ───────────────────────────────────────────────────


def test_score_event_requires_elevated_role(make_client) -> None:
    event, _ = make_event(outcome=1, probabilities=[0.9, 0.7])
    gateway = FakeEventScoringGateway(
        statuses={
            event.event_id: EventScoringStatus(
                found=True, is_resolved=True, is_final=True, outcome=1
            )
        },
        events={event.event_id: event},
    )
    client, _, _, _ = make_client(gateway=gateway, role=UserRole.USER)

    resp = client.post(f"/admin/events/{event.event_id}/score")
    assert resp.status_code == 403
    assert resp.json()["error"] == "ScoringPermissionError"


def test_score_event_as_editor(make_client) -> None:
    event, ids = make_event(outcome=1, probabilities=[0.9, 0.7])
    gateway = FakeEventScoringGateway(
        statuses={
            event.event_id: EventScoringStatus(
                found=True, is_resolved=True, is_final=True, outcome=1
            )
        },
        events={event.event_id: event},
    )
    writer = FakePredictionScoreWriter()
    client, _, _, _ = make_client(gateway=gateway, writer=writer, role=UserRole.EDITOR)

    resp = client.post(f"/admin/events/{event.event_id}/score")
    assert resp.status_code == 200
    assert resp.json()["scored"] == 2
    assert event.event_id in writer.saved


def test_score_unresolved_event_conflict(make_client) -> None:
    event_id = uuid.uuid4()
    gateway = FakeEventScoringGateway(
        statuses={
            event_id: EventScoringStatus(
                found=True, is_resolved=False, is_final=False, outcome=None
            )
        }
    )
    client, _, _, _ = make_client(gateway=gateway, role=UserRole.EDITOR)

    resp = client.post(f"/admin/events/{event_id}/score")
    assert resp.status_code == 409
    assert resp.json()["error"] == "EventNotResolvedError"


def test_score_missing_event_404(make_client) -> None:
    client, _, _, _ = make_client(role=UserRole.EDITOR)
    resp = client.post(f"/admin/events/{uuid.uuid4()}/score")
    assert resp.status_code == 404
    assert resp.json()["error"] == "ScoringTargetEventNotFoundError"


# ── Пересчёт рейтингов (RBAC) ────────────────────────────────────────────────


def test_recompute_requires_admin(make_client) -> None:
    client, _, _, _ = make_client(role=UserRole.EDITOR)
    resp = client.post("/admin/ratings/recompute")
    assert resp.status_code == 403


async def test_recompute_as_admin(make_client) -> None:
    ids = [uuid.uuid4() for _ in range(5)]
    event, _ = make_event(
        outcome=0, probabilities=[0.9, 0.9, 0.9, 0.9, 0.3], user_ids=ids
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    repo = InMemoryRatingRepository()
    client, _, _, _ = make_client(gateway=gateway, repo=repo, role=UserRole.ADMIN)

    resp = client.post("/admin/ratings/recompute")
    assert resp.status_code == 200
    # 5 пользователей × 2 области (global + category) = 10 рейтингов.
    assert resp.json()["upserted"] == 10
    board = await repo.leaderboard(ScopeType.GLOBAL, None)
    assert board[0].user_id == ids[4]


def test_leaderboard_requires_no_auth(make_client) -> None:
    """Лидерборды публичны — доступны без аутентификации."""
    repo = InMemoryRatingRepository()
    client, _, _, _ = make_client(repo=repo, role=None)
    resp = client.get("/leaderboards/global")
    assert resp.status_code == 200
