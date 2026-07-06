"""Интеграционные тесты профилей пользователей (`/users`).

Публичный профиль по хэндлу и редактирование своего профиля. Порты identity
подменяются in-memory фейками через ``dependency_overrides``; аутентификация —
через реальный OIDC-поток (login → callback ставит cookie).
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
        yield client, repo


def _login(client: TestClient) -> None:
    """Проходит OIDC-поток: после callback access-cookie выставлен."""
    resp = client.get("/auth/esia/login", follow_redirects=False)
    assert resp.status_code == 307
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    callback = client.get(
        "/auth/esia/callback", params={"code": "abc", "state": state}
    )
    assert callback.status_code == 201


def test_public_profile_returns_pseudonymous_view(context) -> None:
    client, _ = context
    _login(client)  # создаёт пользователя с псевдонимным хэндлом
    username = client.get("/auth/me").json()["username"]

    resp = client.get(f"/users/{username}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == username
    assert username.startswith("predictor-")  # псевдоним, не ФИО (H-PII)
    assert "display_name" in body
    # ФИО/ПДн в публичный профиль не утекают.
    assert "real_name" not in body and "snils" not in body


def test_public_profile_unknown_404(context) -> None:
    client, _ = context
    assert client.get("/users/ghost").status_code == 404


def test_patch_me_updates_display_name(context) -> None:
    client, _ = context
    _login(client)

    username = client.get("/auth/me").json()["username"]
    resp = client.patch("/users/me", json={"display_name": "Оракул"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Оракул"

    # Изменение видно в публичном профиле.
    public = client.get(f"/users/{username}")
    assert public.json()["display_name"] == "Оракул"


def test_patch_me_requires_auth(context) -> None:
    client, _ = context
    resp = client.patch("/users/me", json={"display_name": "X"})
    assert resp.status_code == 401
