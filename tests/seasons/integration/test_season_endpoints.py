"""Интеграционные тесты HTTP-эндпоинтов seasons.

Поднимают реальное FastAPI-приложение, но репозиторий, часы и аутентификацию
подменяют фейками через ``dependency_overrides``. БД-интеграция с Postgres
(citext ``UNIQUE(slug)``, enum ``season_status``, jsonb, append-only грант) —
отдельным e2e (TODO).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from app.modules.seasons.api.dependencies import get_clock, get_season_repository
from tests.seasons.fakes import FakeClock, InMemorySeasonRepository
from tests.seasons.unit.test_use_cases import ENDS, NOW, STARTS


def _user(role: UserRole) -> User:
    return User(
        esia_oid="oid",
        snils_hash="hash",
        username="boss",
        display_name="Босс",
        real_name_enc=None,
        role=role,
    )


@pytest.fixture
def make_client():
    """Фабрика клиента: общий фейковый репозиторий + управляемая роль."""
    created: list[TestClient] = []

    def _build(
        *,
        repo: InMemorySeasonRepository | None = None,
        role: UserRole | None = UserRole.ADMIN,
    ) -> tuple[TestClient, InMemorySeasonRepository]:
        repo = repo or InMemorySeasonRepository()
        app = create_app()
        app.dependency_overrides[get_season_repository] = lambda: repo
        app.dependency_overrides[get_clock] = lambda: FakeClock(NOW)
        if role is not None:
            app.dependency_overrides[get_current_user] = lambda: _user(role)
        client = TestClient(app)
        created.append(client)
        return client, repo

    yield _build
    for client in created:
        client.close()


def _create_payload(slug: str = "2026q3") -> dict[str, str]:
    return {
        "slug": slug,
        "title": "Сезон III",
        "starts_at": STARTS.isoformat(),
        "ends_at": ENDS.isoformat(),
    }


def test_admin_creates_season_then_public_can_read_it(make_client) -> None:
    client, _ = make_client(role=UserRole.ADMIN)
    resp = client.post("/admin/seasons", json=_create_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "upcoming"
    assert body["league_config"] is None

    public = client.get("/seasons/2026q3")
    assert public.status_code == 200
    assert public.json()["slug"] == "2026q3"


def test_create_requires_manage_role(make_client) -> None:
    client, _ = make_client(role=UserRole.USER)
    resp = client.post("/admin/seasons", json=_create_payload())
    assert resp.status_code == 403


def test_duplicate_slug_is_conflict(make_client) -> None:
    client, _ = make_client(role=UserRole.ADMIN)
    assert client.post("/admin/seasons", json=_create_payload()).status_code == 201
    dup = client.post("/admin/seasons", json=_create_payload())
    assert dup.status_code == 409


def test_get_missing_season_is_404(make_client) -> None:
    client, _ = make_client()
    assert client.get("/seasons/nope").status_code == 404


def test_activate_uses_default_config_and_requires_admin(make_client) -> None:
    client, _ = make_client(role=UserRole.ADMIN)
    created = client.post("/admin/seasons", json=_create_payload()).json()
    resp = client.post(f"/admin/seasons/{created['id']}/activate", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["league_config"]["gradation_map"] == [0.1, 0.3, 0.5, 0.7, 0.9]


def test_activate_forbidden_for_editor(make_client) -> None:
    # editor может заводить сезон, но не переводить статус (только admin).
    client, _ = make_client(role=UserRole.EDITOR)
    created = client.post("/admin/seasons", json=_create_payload()).json()
    resp = client.post(f"/admin/seasons/{created['id']}/activate", json={})
    assert resp.status_code == 403


def test_activate_accepts_custom_league_config(make_client) -> None:
    client, _ = make_client(role=UserRole.ADMIN)
    created = client.post("/admin/seasons", json=_create_payload()).json()
    custom = {
        "league_config": {
            "gradation_map": [0.2, 0.4, 0.5, 0.6, 0.8],
            "n_min": 10,
            "c_min": 2,
            "w_min": 4.0,
            "m_per_category": 1,
            "k_shrink": 3.0,
            "min_predictors": 3,
        }
    }
    resp = client.post(f"/admin/seasons/{created['id']}/activate", json=custom)
    assert resp.status_code == 200, resp.text
    assert resp.json()["league_config"]["n_min"] == 10


def test_list_filters_by_status(make_client) -> None:
    client, _ = make_client(role=UserRole.ADMIN)
    client.post("/admin/seasons", json=_create_payload("a"))
    created_b = client.post("/admin/seasons", json=_create_payload("b")).json()
    client.post(f"/admin/seasons/{created_b['id']}/activate", json={})

    active = client.get("/seasons", params={"status": "active"})
    assert active.status_code == 200
    slugs = [s["slug"] for s in active.json()["items"]]
    assert slugs == ["b"]
