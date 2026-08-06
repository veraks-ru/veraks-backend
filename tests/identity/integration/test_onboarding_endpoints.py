"""Интеграционные тесты онбординга и согласий (152-ФЗ, T2).

`GET /auth/me` (needs_onboarding/missing_consents), `POST
/users/me/onboarding` и `GET /users/me/consents`. Порты identity подменяются
in-memory фейками через ``dependency_overrides``; аутентификация — через
реальный OIDC-поток (login → callback ставит cookie).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.modules.identity.api.dependencies import (
    get_audit_trail,
    get_consent_repository,
    get_esia_gateway,
    get_refresh_store,
    get_state_store,
    get_user_repository,
)
from tests.identity.fakes import (
    FakeAuditTrail,
    FakeEsiaGateway,
    FakeRefreshTokenStore,
    FakeStateStore,
    InMemoryConsentRepository,
    InMemoryUserRepository,
)

_REQUIRED_CONSENTS = [
    {"document": "offer", "version": "2026-07-05"},
    {"document": "pdn", "version": "2026-07-05"},
]


@pytest.fixture
def context(confirmed_identity):
    repo = InMemoryUserRepository()
    state_store = FakeStateStore()
    refresh_store = FakeRefreshTokenStore()
    gateway = FakeEsiaGateway(confirmed_identity)
    consents = InMemoryConsentRepository()

    app = create_app()
    app.dependency_overrides[get_user_repository] = lambda: repo
    app.dependency_overrides[get_esia_gateway] = lambda: gateway
    app.dependency_overrides[get_state_store] = lambda: state_store
    app.dependency_overrides[get_refresh_store] = lambda: refresh_store
    app.dependency_overrides[get_consent_repository] = lambda: consents
    app.dependency_overrides[get_audit_trail] = lambda: FakeAuditTrail()
    with TestClient(app) as client:
        yield client, repo, consents


def _login(client: TestClient) -> None:
    resp = client.get("/auth/esia/login", follow_redirects=False)
    assert resp.status_code == 307
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    callback = client.get(
        "/auth/esia/callback", params={"code": "abc", "state": state}
    )
    assert callback.status_code == 201


def test_first_login_needs_onboarding(context) -> None:
    """Первый вход: /auth/me сообщает о необходимости онбординга."""
    client, _, _ = context
    _login(client)

    me = client.get("/auth/me")
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["needs_onboarding"] is True
    assert {(c["document"], c["version"]) for c in body["missing_consents"]} == {
        ("offer", "2026-07-05"),
        ("pdn", "2026-07-05"),
    }


def test_onboarding_with_incomplete_consents_fails(context) -> None:
    """Неполный набор согласий → доменная ошибка (422), онбординг не пройден."""
    client, _, _ = context
    _login(client)

    resp = client.post(
        "/users/me/onboarding",
        json={"consents": [{"document": "offer", "version": "2026-07-05"}]},
    )
    assert resp.status_code == 422, resp.text

    me = client.get("/auth/me").json()
    assert me["needs_onboarding"] is True


def test_onboarding_with_full_consents_succeeds(context) -> None:
    """Полный набор согласий + псевдоним → онбординг пройден, согласия видны."""
    client, _, _ = context
    _login(client)

    resp = client.post(
        "/users/me/onboarding",
        json={
            "username": "chosen-handle",
            "display_name": "Выбранное имя",
            "consents": _REQUIRED_CONSENTS,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_onboarding"] is False
    assert body["missing_consents"] == []
    assert body["username"] == "chosen-handle"
    assert body["display_name"] == "Выбранное имя"

    me = client.get("/auth/me").json()
    assert me["needs_onboarding"] is False

    consents = client.get("/users/me/consents").json()
    assert {(c["document"], c["version"]) for c in consents} == {
        ("offer", "2026-07-05"),
        ("pdn", "2026-07-05"),
    }
    assert all(c["method"] == "onboarding_web" for c in consents)


def test_onboarding_idempotent_when_already_done(context) -> None:
    """Повторный вызов при уже пройденном онбординге и полных согласиях — 200."""
    client, _, _ = context
    _login(client)

    first = client.post(
        "/users/me/onboarding", json={"consents": _REQUIRED_CONSENTS}
    )
    assert first.status_code == 200, first.text

    second = client.post("/users/me/onboarding", json={"consents": []})
    assert second.status_code == 200, second.text
    assert second.json()["needs_onboarding"] is False

    # Повтор не задвоил согласия.
    consents = client.get("/users/me/consents").json()
    assert len(consents) == 2


def test_onboarding_requires_auth(context) -> None:
    client, _, _ = context
    resp = client.post("/users/me/onboarding", json={"consents": []})
    assert resp.status_code == 401


def test_onboarding_with_junk_forwarded_for_records_no_ip(context) -> None:
    """Мусор в ``X-Forwarded-For`` не валит онбординг (колонка ``inet``).

    Заголовок клиент присылает сам; раньше он попадал в ``user_consents.ip``
    как есть — ``DataError`` и 500. Теперь невалидное значение отбрасывается,
    и, если валидного адреса нет и у сокета (у ``TestClient`` хост —
    ``testclient``), в согласии остаётся ``NULL``.
    """
    client, _, consents = context
    _login(client)

    resp = client.post(
        "/users/me/onboarding",
        json={"consents": _REQUIRED_CONSENTS},
        headers={"X-Forwarded-For": "not-an-ip"},
    )
    assert resp.status_code == 200, resp.text
    assert [c.ip for c in consents.rows] == [None, None]


def test_onboarding_records_valid_forwarded_for(context) -> None:
    """Валидный адрес из ``X-Forwarded-For`` фиксируется в согласии."""
    client, _, consents = context
    _login(client)

    resp = client.post(
        "/users/me/onboarding",
        json={"consents": _REQUIRED_CONSENTS},
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )
    assert resp.status_code == 200, resp.text
    assert {c.ip for c in consents.rows} == {"203.0.113.7"}


def test_document_version_bump_reintroduces_missing_consent(context) -> None:
    """Смена версии документа в конфиге → needs_onboarding снова true, доносится только недостающее."""
    client, _, _ = context
    _login(client)

    onboarding = client.post(
        "/users/me/onboarding", json={"consents": _REQUIRED_CONSENTS}
    )
    assert onboarding.status_code == 200, onboarding.text
    assert client.get("/auth/me").json()["needs_onboarding"] is False

    settings = get_settings()
    original_offer_version = settings.consents.offer_version
    settings.consents.offer_version = "2026-08-01"
    try:
        me = client.get("/auth/me").json()
        assert me["needs_onboarding"] is True
        assert me["missing_consents"] == [
            {"document": "offer", "version": "2026-08-01"}
        ]

        # Донабор — присылаем только недостающее.
        resp = client.post(
            "/users/me/onboarding",
            json={"consents": [{"document": "offer", "version": "2026-08-01"}]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["needs_onboarding"] is False
    finally:
        settings.consents.offer_version = original_offer_version
