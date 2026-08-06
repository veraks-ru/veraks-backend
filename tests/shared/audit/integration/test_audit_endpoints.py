"""Интеграционные тесты HTTP-эндпоинтов `/admin/audit-log` (только admin)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from app.shared.audit.api.dependencies import get_audit_log_reader
from tests.shared.audit.fakes import FakeAuditLogReader, build_valid_chain


def _user(role: UserRole) -> User:
    return User(
        esia_oid_hash="oid",
        snils_hash="hash",
        username="boss",
        display_name="Босс",
        real_name_enc=None,
        role=role,
    )


@pytest.fixture
def make_client():
    created: list[TestClient] = []

    def _build(
        *, entries: list | None = None, role: UserRole | None = UserRole.ADMIN
    ) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_audit_log_reader] = lambda: FakeAuditLogReader(
            entries if entries is not None else build_valid_chain(5)
        )
        if role is not None:
            app.dependency_overrides[get_current_user] = lambda: _user(role)
        client = TestClient(app)
        created.append(client)
        return client

    yield _build
    for client in created:
        client.close()


def test_list_requires_admin(make_client) -> None:
    client = make_client(role=UserRole.EDITOR)
    resp = client.get("/admin/audit-log")
    assert resp.status_code == 403


def test_list_anonymous_is_unauthorized(make_client) -> None:
    client = make_client(role=None)
    resp = client.get("/admin/audit-log")
    assert resp.status_code == 401


def test_list_returns_page_newest_first(make_client) -> None:
    client = make_client(entries=build_valid_chain(5))
    resp = client.get("/admin/audit-log", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [5, 4]
    assert body["has_more"] is True


def test_list_filters_by_action(make_client) -> None:
    client = make_client(entries=build_valid_chain(5))
    resp = client.get("/admin/audit-log", params={"action": "test.action.3"})
    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [3]


def test_verify_requires_admin(make_client) -> None:
    client = make_client(role=UserRole.USER)
    resp = client.post("/admin/audit-log/verify")
    assert resp.status_code == 403


def test_verify_ok_on_valid_chain(make_client) -> None:
    client = make_client(entries=build_valid_chain(4))
    resp = client.post("/admin/audit-log/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["checked"] == 4
    assert body["first_broken_id"] is None


def test_verify_detects_tampered_record(make_client) -> None:
    entries = build_valid_chain(4)
    entries[1].after = {"n": "tampered"}
    client = make_client(entries=entries)
    resp = client.post("/admin/audit-log/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["first_broken_id"] == 2


def test_actor_id_filter_matches_none_by_default(make_client) -> None:
    """Записи без ``actor_id`` (SYSTEM) не всплывают при фильтре по случайному actor."""
    client = make_client(entries=build_valid_chain(3))
    resp = client.get("/admin/audit-log", params={"actor_id": str(uuid.uuid4())})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_naive_date_range_is_treated_as_utc(make_client) -> None:
    """Наивные ``occurred_from``/``occurred_to`` (без зоны) не роняют сравнение
    с ``timestamptz`` — трактуются как UTC (см. ``router._as_utc``).

    Записи ``build_valid_chain`` имеют ``occurred_at`` = 2026-01-01 UTC
    (aware); без нормализации сравнение aware/naive в фейк-ридере упало бы с
    ``TypeError`` (как упало бы и в реальном драйвере), а не молча дало
    неверный результат — так что 200 с непустым списком доказывает фикс.
    """
    client = make_client(entries=build_valid_chain(3))
    resp = client.get(
        "/admin/audit-log",
        params={"occurred_from": "2025-12-31T00:00:00", "occurred_to": "2026-01-02T00:00:00"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
