"""Интеграционные тесты HTTP-эндпоинтов `/admin/users` (модерация, B7, только admin).

RBAC подменяется напрямую через ``get_current_user`` (тот же приём, что и в
``tests/shared/audit/integration/test_audit_endpoints.py``) — роль текущего
пользователя не зависит от прохождения полного OIDC-потока.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.identity.api.dependencies import (
    get_audit_trail,
    get_current_user,
    get_refresh_store,
    get_user_repository,
)
from app.modules.identity.domain.entities import User, UserRole, UserStatus
from tests.identity.fakes import (
    FakeAuditTrail,
    FakeRefreshTokenStore,
    InMemoryUserRepository,
)


def _user(
    username: str, *, role: UserRole = UserRole.USER, status: UserStatus = UserStatus.ACTIVE
) -> User:
    return User(
        esia_oid_hash=f"oid-{username}",
        snils_hash=f"snils-{username}",
        username=username,
        display_name=username,
        real_name_enc=None,
        role=role,
        status=status,
    )


@pytest.fixture
def ctx():
    repo = InMemoryUserRepository()
    refresh_store = FakeRefreshTokenStore()
    audit = FakeAuditTrail()

    admin = _user("boss", role=UserRole.ADMIN)

    app = create_app()
    app.dependency_overrides[get_user_repository] = lambda: repo
    app.dependency_overrides[get_refresh_store] = lambda: refresh_store
    app.dependency_overrides[get_audit_trail] = lambda: audit
    app.dependency_overrides[get_current_user] = lambda: admin

    with TestClient(app) as client:
        yield client, repo, refresh_store, audit, admin


def _login_as(app, user: User) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def test_suspend_requires_admin(ctx) -> None:
    client, repo, _, _, _ = ctx
    editor = _user("ed", role=UserRole.EDITOR)
    client.app.dependency_overrides[get_current_user] = lambda: editor

    resp = client.post(f"/admin/users/{uuid.uuid4()}/suspend", json={"reason": "x"})
    assert resp.status_code == 403


def test_list_users_requires_admin(ctx) -> None:
    client, *_ = ctx
    client.app.dependency_overrides[get_current_user] = lambda: _user(
        "arb", role=UserRole.ARBITER
    )
    resp = client.get("/admin/users")
    assert resp.status_code == 403


async def test_suspend_happy_path_revokes_and_audits(ctx) -> None:
    client, repo, refresh_store, audit, admin = ctx
    await repo.add(admin)
    target = _user("troll")
    await repo.add(target)
    await refresh_store.register("jti-1", 3600, str(target.id))

    resp = client.post(
        f"/admin/users/{target.id}/suspend", json={"reason": "спам-прогнозы"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "suspended"

    stored = await repo.get_by_id(target.id)
    assert stored is not None and stored.status is UserStatus.SUSPENDED
    assert await refresh_store.is_active("jti-1") is False
    assert audit.actions() == ["identity.user.suspended"]


async def test_suspend_requires_non_empty_reason(ctx) -> None:
    client, repo, _, _, admin = ctx
    await repo.add(admin)
    target = _user("troll2")
    await repo.add(target)

    resp = client.post(f"/admin/users/{target.id}/suspend", json={"reason": ""})
    assert resp.status_code == 422


async def test_suspend_self_is_forbidden(ctx) -> None:
    client, repo, _, _, admin = ctx
    await repo.add(admin)

    resp = client.post(f"/admin/users/{admin.id}/suspend", json={"reason": "оговорка"})
    assert resp.status_code == 403


async def test_suspend_another_admin_is_forbidden(ctx) -> None:
    client, repo, _, _, admin = ctx
    await repo.add(admin)
    other_admin = _user("boss2", role=UserRole.ADMIN)
    await repo.add(other_admin)

    resp = client.post(
        f"/admin/users/{other_admin.id}/suspend", json={"reason": "конфликт"}
    )
    assert resp.status_code == 403


async def test_suspend_unknown_user_404(ctx) -> None:
    client, repo, _, _, admin = ctx
    await repo.add(admin)

    resp = client.post(f"/admin/users/{uuid.uuid4()}/suspend", json={"reason": "x"})
    assert resp.status_code == 404


async def test_reinstate_happy_path(ctx) -> None:
    client, repo, _, audit, admin = ctx
    await repo.add(admin)
    target = _user("reformed", status=UserStatus.SUSPENDED)
    await repo.add(target)

    resp = client.post(f"/admin/users/{target.id}/reinstate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    stored = await repo.get_by_id(target.id)
    assert stored is not None and stored.status is UserStatus.ACTIVE
    assert audit.actions() == ["identity.user.reinstated"]


async def test_reinstate_non_suspended_conflicts(ctx) -> None:
    client, repo, _, _, admin = ctx
    await repo.add(admin)
    target = _user("already-active")
    await repo.add(target)

    resp = client.post(f"/admin/users/{target.id}/reinstate")
    assert resp.status_code == 409


async def test_list_users_filters_by_status_and_search(ctx) -> None:
    client, repo, _, _, admin = ctx
    await repo.add(admin)
    await repo.add(_user("alice"))
    await repo.add(_user("bob", status=UserStatus.SUSPENDED))

    resp = client.get("/admin/users", params={"status": "suspended"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["username"] == "bob"

    resp = client.get("/admin/users", params={"search": "ali"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["username"] == "alice"


async def test_list_users_pagination(ctx) -> None:
    client, repo, _, _, admin = ctx
    await repo.add(admin)
    for i in range(3):
        await repo.add(_user(f"u{i}"))

    resp = client.get("/admin/users", params={"limit": 2, "offset": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4  # + admin
    assert len(body["items"]) == 2
