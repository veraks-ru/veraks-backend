"""Интеграционные тесты HTTP-эндпоинтов `/events/{id}/prediction`.

Поднимают реальное FastAPI-приложение, но I/O-порты (репозиторий, шлюз
events, часы, аудит) и аутентификацию подменяют фейками/оверрайдами через
``dependency_overrides``. БД-интеграция с Postgres (UNIQUE(user,event), enum
confidence_grade, CHECK probability) — отдельным e2e.

TODO(predictions-infra): добавить e2e против реального Postgres
(testcontainers) для проверки ``UNIQUE(user_id, event_id)``, enum и FK.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from app.modules.predictions.api.dependencies import (
    get_audit_recorder,
    get_clock,
    get_event_gateway,
    get_prediction_repository,
    get_user_directory,
)
from tests.predictions.conftest import FIXED_NOW
from tests.predictions.fakes import (
    FakeAuditRecorder,
    FakeClock,
    FakeEventGateway,
    FakeUserDirectory,
    InMemoryPredictionRepository,
)


def _fake_user() -> User:
    """Минимальный аутентифицированный пользователь (роль user достаточно)."""
    return User(
        esia_oid="oid",
        snils_hash="hash",
        username="predictor1",
        display_name="Предсказатель",
        real_name_enc=None,
        role=UserRole.USER,
    )


@pytest.fixture
def make_client(open_snapshot):
    """Фабрика клиента: общие фейки + управляемая аутентификация.

    ``authenticated=False`` оставляет реальную аутентификацию (для 401).
    Возвращает ``(client, prediction_repo, event_gateway, user)``.
    """
    created: list[TestClient] = []

    def _build(*, authenticated: bool = True, gateway: FakeEventGateway | None = None):
        repo = InMemoryPredictionRepository()
        event_gateway = gateway if gateway is not None else FakeEventGateway([open_snapshot])
        user = _fake_user()

        app = create_app()
        app.dependency_overrides[get_prediction_repository] = lambda: repo
        app.dependency_overrides[get_event_gateway] = lambda: event_gateway
        app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)
        app.dependency_overrides[get_audit_recorder] = lambda: FakeAuditRecorder()
        app.dependency_overrides[get_user_directory] = lambda: FakeUserDirectory(
            {user.username: user.id}
        )
        if authenticated:
            app.dependency_overrides[get_current_user] = lambda: user

        client = TestClient(app)
        created.append(client)
        return client, repo, event_gateway, user

    yield _build
    for client in created:
        client.close()


def test_put_prediction_requires_auth(make_client, open_snapshot) -> None:
    client, _, _, _ = make_client(authenticated=False)
    resp = client.put(
        f"/events/{open_snapshot.event_id}/prediction",
        json={"confidence_grade": "fifty_fifty"},
    )
    assert resp.status_code == 401


def test_put_and_get_my_prediction(make_client, open_snapshot) -> None:
    client, _, _, _ = make_client()
    event_id = open_snapshot.event_id

    put = client.put(
        f"/events/{event_id}/prediction",
        json={"confidence_grade": "definitely_yes"},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["confidence_grade"] == "definitely_yes"
    assert float(body["probability"]) == 0.9
    assert body["is_locked"] is False

    mine = client.get(f"/events/{event_id}/prediction/me")
    assert mine.status_code == 200
    assert mine.json()["id"] == body["id"]


def test_put_prediction_is_upsert(make_client, open_snapshot) -> None:
    client, repo, _, _ = make_client()
    event_id = open_snapshot.event_id

    client.put(
        f"/events/{event_id}/prediction", json={"confidence_grade": "fifty_fifty"}
    )
    second = client.put(
        f"/events/{event_id}/prediction", json={"confidence_grade": "probably_no"}
    )
    assert second.status_code == 200
    assert float(second.json()["probability"]) == 0.3


def test_put_prediction_closed_event_conflict(make_client, closed_snapshot) -> None:
    client, _, _, _ = make_client(gateway=FakeEventGateway([closed_snapshot]))
    resp = client.put(
        f"/events/{closed_snapshot.event_id}/prediction",
        json={"confidence_grade": "fifty_fifty"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "PredictionsClosedError"


def test_put_prediction_missing_event_404(make_client) -> None:
    client, _, _, _ = make_client(gateway=FakeEventGateway([]))
    resp = client.put(
        f"/events/{uuid.uuid4()}/prediction",
        json={"confidence_grade": "fifty_fifty"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "PredictionTargetEventNotFoundError"


def test_get_my_prediction_missing_404(make_client, open_snapshot) -> None:
    client, _, _, _ = make_client()
    resp = client.get(f"/events/{open_snapshot.event_id}/prediction/me")
    assert resp.status_code == 404
    assert resp.json()["error"] == "PredictionNotFoundError"


def test_put_prediction_invalid_grade_422(make_client, open_snapshot) -> None:
    client, _, _, _ = make_client()
    resp = client.put(
        f"/events/{open_snapshot.event_id}/prediction",
        json={"confidence_grade": "maybe"},
    )
    assert resp.status_code == 422


def test_predictions_summary_hidden_while_open(make_client, open_snapshot) -> None:
    """До закрытия приёма сигнал толпы скрыт → 409 (анти-якорение)."""
    client, _, _, _ = make_client()
    resp = client.get(f"/events/{open_snapshot.event_id}/predictions/summary")
    assert resp.status_code == 409
    assert resp.json()["error"] == "PredictionSummaryHiddenError"


def test_predictions_summary_after_close(make_client, closed_snapshot) -> None:
    """После закрытия — публичный агрегат распределения и консенсуса."""
    from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction

    client, repo, _, _ = make_client(gateway=FakeEventGateway([closed_snapshot]))
    event_id = closed_snapshot.event_id
    for grade in (
        ConfidenceGrade.DEFINITELY_YES,
        ConfidenceGrade.DEFINITELY_YES,
        ConfidenceGrade.PROBABLY_NO,
    ):
        repo.seed(
            Prediction.place(user_id=uuid.uuid4(), event_id=event_id, grade=grade)
        )

    resp = client.get(f"/events/{event_id}/predictions/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_count"] == 3
    assert body["distribution"]["definitely_yes"] == 2
    assert body["mean_probability"] == "0.70"  # (0.9+0.9+0.3)/3


def test_my_predictions_lists_own(make_client, open_snapshot) -> None:
    """GET /users/me/predictions — все свои прогнозы, включая ожидающие."""
    client, repo, _, user = make_client()
    from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction

    repo.seed(
        Prediction.place(
            user_id=user.id,
            event_id=open_snapshot.event_id,
            grade=ConfidenceGrade.PROBABLY_YES,
        )
    )
    resp = client.get("/users/me/predictions")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


def test_my_predictions_requires_auth(make_client) -> None:
    client, _, _, _ = make_client(authenticated=False)
    assert client.get("/users/me/predictions").status_code == 401


def test_user_predictions_public_only_resolved(make_client) -> None:
    """GET /users/{username}/predictions — публично, только засчитанные."""
    from decimal import Decimal

    from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction

    client, repo, _, user = make_client(authenticated=False)
    pending = Prediction.place(
        user_id=user.id, event_id=uuid.uuid4(), grade=ConfidenceGrade.FIFTY_FIFTY
    )
    resolved = Prediction.place(
        user_id=user.id, event_id=uuid.uuid4(), grade=ConfidenceGrade.DEFINITELY_YES
    )
    resolved.brier_score = Decimal("0.01")
    repo.seed(pending)
    repo.seed(resolved)

    resp = client.get(f"/users/{user.username}/predictions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1  # ожидающий скрыт
    assert body[0]["brier_score"] is not None


def test_user_predictions_unknown_profile_404(make_client) -> None:
    client, _, _, _ = make_client(authenticated=False)
    assert client.get("/users/ghost/predictions").status_code == 404


def test_default_clock_is_system_clock() -> None:
    """Дефолтный провайдер часов — системные (UTC)."""
    from app.modules.predictions.adapters.clock import SystemClock

    assert isinstance(get_clock(), SystemClock)
