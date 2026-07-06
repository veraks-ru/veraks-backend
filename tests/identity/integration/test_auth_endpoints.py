"""Интеграционные тесты HTTP-эндпоинтов `/auth`.

Поднимают реальное FastAPI-приложение, но порты I/O (репозиторий, шлюз ЕСИА,
state/refresh-хранилища) подменяются in-memory фейками через
``dependency_overrides``. Крипто-порты и настройки берутся из тест-окружения.
БД-интеграция с Postgres покрывается отдельно (см. TODO ниже).

TODO(identity-infra): добавить end-to-end тест против реального Postgres
(testcontainers) для проверки UNIQUE-constraint'ов и enum-типов миграции.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.identity.api.dependencies import (
    get_esia_gateway,
    get_refresh_store,
    get_state_store,
    get_user_repository,
)
from tests.identity.fakes import (
    FakeEsiaGateway,
    FakeRefreshTokenStore,
    FakeStateStore,
    InMemoryUserRepository,
)


@pytest.fixture
def context(confirmed_identity):
    """Приложение с подменёнными портами и общими фейками."""
    repo = InMemoryUserRepository()
    state_store = FakeStateStore()
    refresh_store = FakeRefreshTokenStore()
    gateway = FakeEsiaGateway(confirmed_identity)

    app = create_app()
    app.dependency_overrides[get_user_repository] = lambda: repo
    app.dependency_overrides[get_esia_gateway] = lambda: gateway
    app.dependency_overrides[get_state_store] = lambda: state_store
    app.dependency_overrides[get_refresh_store] = lambda: refresh_store

    with TestClient(app) as client:
        yield client, repo, gateway


def _login_and_get_state(client: TestClient) -> str:
    """Дёргает /auth/esia/login и достаёт сгенерированный state из редиректа."""
    resp = client.get("/auth/esia/login", follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers["location"]
    state = parse_qs(urlparse(location).query)["state"][0]
    return state


def test_login_redirects_to_esia(context) -> None:
    client, _, _ = context
    resp = client.get("/auth/esia/login", follow_redirects=False)
    assert resp.status_code == 307
    assert "esia.example/authorize" in resp.headers["location"]


def test_callback_creates_user_and_sets_cookies(context) -> None:
    client, repo, _ = context
    state = _login_and_get_state(client)

    resp = client.get("/auth/esia/callback", params={"code": "abc", "state": state})

    assert resp.status_code == 201  # новый аккаунт
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert "access_token" in resp.cookies
    assert "refresh_token" in resp.cookies


def test_callback_rejects_unknown_state(context) -> None:
    client, _, _ = context
    resp = client.get(
        "/auth/esia/callback", params={"code": "abc", "state": "forged"}
    )
    assert resp.status_code == 400


def test_me_requires_auth(context) -> None:
    client, _, _ = context
    assert client.get("/auth/me").status_code == 401


def test_full_flow_login_me_refresh_logout(context) -> None:
    client, _, _ = context

    state = _login_and_get_state(client)
    login = client.get("/auth/esia/callback", params={"code": "abc", "state": state})
    access = login.json()["access_token"]

    # /auth/me по Bearer-токену.
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["username"].startswith("predictor-")  # псевдоним (H-PII)
    assert me.json()["role"] == "user"

    # refresh по cookie (TestClient переносит cookie автоматически).
    refreshed = client.post("/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]

    # logout отзывает refresh.
    assert client.post("/auth/logout").status_code == 204
    # после logout refresh больше не работает.
    assert client.post("/auth/refresh").status_code == 401


def test_second_login_same_citizen_reuses_account(context) -> None:
    client, repo, _ = context

    state1 = _login_and_get_state(client)
    first = client.get("/auth/esia/callback", params={"code": "a", "state": state1})
    assert first.status_code == 201

    state2 = _login_and_get_state(client)
    second = client.get("/auth/esia/callback", params={"code": "b", "state": state2})
    assert second.status_code == 200  # существующий аккаунт, не создан новый
