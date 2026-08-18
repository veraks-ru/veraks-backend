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

from app.config import get_settings
from app.main import create_app
from app.modules.identity.api.dependencies import (
    get_consent_repository,
    get_current_user,
)
from app.modules.identity.domain.entities import User, UserRole
from app.modules.predictions.api.dependencies import (
    get_audit_recorder,
    get_clock,
    get_event_gateway,
    get_prediction_repository,
    get_user_directory,
)
from tests.identity.fakes import (
    InMemoryConsentRepository,
    onboarded_consent_repository,
)
from tests.predictions.conftest import FIXED_NOW
from tests.predictions.fakes import (
    FakeAuditRecorder,
    FakeClock,
    FakeEventGateway,
    FakeUserDirectory,
    InMemoryPredictionRepository,
)


def _fake_user(*, onboarded: bool = True) -> User:
    """Минимальный аутентифицированный пользователь (роль user достаточно).

    По умолчанию — с пройденным онбордингом: ставить прогноз без акцепта
    оферты/ПДн запрещено гардом ``require_onboarded_user`` (PRD §7).
    """
    return User(
        esia_oid_hash="oid",
        snils_hash="hash",
        username="predictor1",
        display_name="Предсказатель",
        real_name_enc=None,
        role=UserRole.USER,
        onboarded_at=FIXED_NOW if onboarded else None,
    )


@pytest.fixture
def make_client(open_snapshot):
    """Фабрика клиента: общие фейки + управляемая аутентификация.

    ``authenticated=False`` оставляет реальную аутентификацию (для 401).
    Возвращает ``(client, prediction_repo, event_gateway, user)``.
    """
    created: list[TestClient] = []

    def _build(
        *,
        authenticated: bool = True,
        gateway: FakeEventGateway | None = None,
        onboarded: bool = True,
    ):
        repo = InMemoryPredictionRepository()
        event_gateway = gateway if gateway is not None else FakeEventGateway([open_snapshot])
        user = _fake_user(onboarded=onboarded)
        consents = (
            onboarded_consent_repository(user.id)
            if onboarded
            else InMemoryConsentRepository()
        )

        app = create_app()
        app.dependency_overrides[get_prediction_repository] = lambda: repo
        app.dependency_overrides[get_event_gateway] = lambda: event_gateway
        app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)
        app.dependency_overrides[get_audit_recorder] = lambda: FakeAuditRecorder()
        app.dependency_overrides[get_user_directory] = lambda: FakeUserDirectory(
            {user.username: user.id}
        )
        # Гард онбординга (identity) считает недостающие согласия по реальному
        # реестру документов — подменяем только хранилище согласий.
        app.dependency_overrides[get_consent_repository] = lambda: consents
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


def test_put_prediction_without_onboarding_forbidden(
    make_client, open_snapshot
) -> None:
    """Участие в конкурсе без акцепта оферты/ПДн — 403 (PRD §7).

    Клиентский гард отправляет на ``/onboarding``, но прямой вызов API должен
    получать отказ с распознаваемым кодом ошибки.
    """
    client, _, _, _ = make_client(onboarded=False)

    resp = client.put(
        f"/events/{open_snapshot.event_id}/prediction",
        json={"confidence_grade": "fifty_fifty"},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == "ConsentRequiredError"
    # Прогноз не сохранён (чтение своего прогноза — без гарда онбординга).
    mine = client.get(f"/events/{open_snapshot.event_id}/prediction/me")
    assert mine.status_code == 404


def test_put_prediction_forbidden_after_document_version_bump(
    make_client, open_snapshot
) -> None:
    """Юрист поднял версию документа → участие блокируется до переподтверждения."""
    client, _, _, _ = make_client()
    settings = get_settings()
    original = settings.consents.offer_version

    first = client.put(
        f"/events/{open_snapshot.event_id}/prediction",
        json={"confidence_grade": "fifty_fifty"},
    )
    assert first.status_code == 200, first.text

    settings.consents.offer_version = "2026-09-01"
    try:
        resp = client.put(
            f"/events/{open_snapshot.event_id}/prediction",
            json={"confidence_grade": "definitely_yes"},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "ConsentRequiredError"
        assert "offer" in resp.json()["detail"]
    finally:
        settings.consents.offer_version = original


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
    client, _repo, _, _ = make_client()
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


def test_predictions_summary_public_while_open(make_client, open_snapshot) -> None:
    """Сигнал толпы доступен и при открытом приёме, и без авторизации.

    Публичный индикатор «во что верят люди» — то, ради чего на площадку
    заходят и те, кто сам не прогнозирует.
    """
    client, _, _, _ = make_client()
    resp = client.get(f"/events/{open_snapshot.event_id}/predictions/summary")
    assert resp.status_code == 200
    assert resp.json()["event_id"] == str(open_snapshot.event_id)


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


# ── Доска лучших прогнозов (GET /events/{id}/top-predictions) ─────────────


def test_top_predictions_open_event_conflict(make_client, open_snapshot) -> None:
    """Событие ещё не разрешено — доска недоступна (409)."""
    client, _, _, _ = make_client(authenticated=False)
    resp = client.get(f"/events/{open_snapshot.event_id}/top-predictions")
    assert resp.status_code == 409
    assert resp.json()["error"] == "EventTopPredictionsUnavailableError"


def test_top_predictions_missing_event_404(make_client) -> None:
    client, _, _, _ = make_client(authenticated=False, gateway=FakeEventGateway([]))
    resp = client.get(f"/events/{uuid.uuid4()}/top-predictions")
    assert resp.status_code == 404
    assert resp.json()["error"] == "PredictionTargetEventNotFoundError"


def test_top_predictions_annulled_event_conflict(make_client) -> None:
    """Аннулированное событие — доска не отдаётся (409), не 200 с пустым списком."""
    event_id = uuid.uuid4()
    gateway = FakeEventGateway([])
    gateway.set_resolved(event_id, False)
    client, _, _, _ = make_client(authenticated=False, gateway=gateway)
    resp = client.get(f"/events/{event_id}/top-predictions")
    assert resp.status_code == 409
    assert resp.json()["error"] == "EventTopPredictionsUnavailableError"


def test_top_predictions_resolved_sorted_by_brier(make_client) -> None:
    """Разрешённое событие: топ по возрастанию Brier, скрытый пользователь исключён."""
    from decimal import Decimal

    from app.modules.predictions.api.dependencies import get_user_directory
    from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction

    event_id = uuid.uuid4()
    gateway = FakeEventGateway([])
    gateway.set_resolved(event_id, True)
    client, repo, _, _ = make_client(authenticated=False, gateway=gateway)

    accurate_id, mediocre_id, hidden_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def _seed(user_id: uuid.UUID, grade: ConfidenceGrade, brier: str) -> None:
        p = Prediction.place(user_id=user_id, event_id=event_id, grade=grade)
        p.brier_score = Decimal(brier)
        repo.seed(p)

    _seed(accurate_id, ConfidenceGrade.DEFINITELY_YES, "0.02")
    _seed(mediocre_id, ConfidenceGrade.FIFTY_FIFTY, "0.40")
    # hidden_id — прогноз засчитан (влияет на среднее толпы), но профиль
    # деактивирован (deleted/suspended) — публично не показывается.
    _seed(hidden_id, ConfidenceGrade.DEFINITELY_YES, "0.01")

    users = FakeUserDirectory()
    users.set_active(accurate_id, username="accurate", display_name="Точный")
    users.set_active(mediocre_id, username="mediocre", display_name="Средний")
    client.app.dependency_overrides[get_user_directory] = lambda: users

    resp = client.get(f"/events/{event_id}/top-predictions")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert [row["username"] for row in body] == ["accurate", "mediocre"]
    assert body[0]["confidence_grade"] == "definitely_yes"
    assert body[0]["brier_score"] == "0.02"
    assert body[0]["beat_crowd"] is True
    assert body[1]["beat_crowd"] is False


def test_top_predictions_limit_capped_at_50(make_client) -> None:
    event_id = uuid.uuid4()
    gateway = FakeEventGateway([])
    gateway.set_resolved(event_id, True)
    client, _, _, _ = make_client(authenticated=False, gateway=gateway)
    resp = client.get(f"/events/{event_id}/top-predictions?limit=51")
    assert resp.status_code == 422


def test_default_clock_is_system_clock() -> None:
    """Дефолтный провайдер часов — системные (UTC)."""
    from app.modules.predictions.adapters.clock import SystemClock

    assert isinstance(get_clock(), SystemClock)
