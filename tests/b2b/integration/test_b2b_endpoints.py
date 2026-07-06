"""Интеграционные тесты HTTP-эндпоинтов управления B2B-ключами.

Поднимают реальное FastAPI-приложение, но use-case выдачи ключа и
аутентификацию подменяют фейками через ``dependency_overrides``. Проверяют
RBAC-гард выдачи (H-B2B: только admin) и валидацию суточной квоты.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.b2b.api.dependencies import get_issue_api_key
from app.modules.b2b.application.use_cases import IssueApiKey
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from tests.b2b.fakes import FakeApiKeyRepository, FakeAuditTrail, FakeKeyGenerator


def _user(role: UserRole) -> User:
    """Аутентифицированный пользователь с заданной ролью."""
    return User(
        esia_oid=f"oid-{uuid.uuid4()}",
        snils_hash=f"hash-{uuid.uuid4()}",
        username=f"user-{uuid.uuid4().hex[:8]}",
        display_name="Тест",
        real_name_enc=None,
        role=role,
    )


@dataclass
class Ctx:
    """Контекст интеграционного клиента с доступом к общим фейкам."""

    client: TestClient
    repo: FakeApiKeyRepository
    audit: FakeAuditTrail
    holder: dict


@pytest.fixture
def ctx():
    """Клиент с фейковым use-case выдачи и переключаемым пользователем."""
    repo = FakeApiKeyRepository()
    audit = FakeAuditTrail()
    holder: dict = {"user": None}

    app = create_app()
    app.dependency_overrides[get_issue_api_key] = lambda: IssueApiKey(
        keys=repo,
        generator=FakeKeyGenerator(),
        audit=audit,
        default_quota=1000,
    )

    def _current_user() -> User:
        user = holder["user"]
        if user is None:  # имитация отсутствия аутентификации
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return user

    app.dependency_overrides[get_current_user] = _current_user

    client = TestClient(app)
    try:
        yield Ctx(client=client, repo=repo, audit=audit, holder=holder)
    finally:
        client.close()


def _act_as(ctx: Ctx, user: User) -> None:
    ctx.holder["user"] = user


# ── H-B2B: выдача ключа — только администратор ────────────────────────────────


def test_create_key_requires_auth(ctx) -> None:
    resp = ctx.client.post("/b2b/keys", json={"name": "Аналитика"})
    assert resp.status_code == 401
    assert ctx.audit.actions() == []


def test_create_key_forbidden_for_plain_user(ctx) -> None:
    _act_as(ctx, _user(UserRole.USER))
    resp = ctx.client.post("/b2b/keys", json={"name": "Аналитика"})
    assert resp.status_code == 403
    # Ключ не выдан, аудит не записан.
    assert ctx.audit.actions() == []


def test_create_key_forbidden_for_editor(ctx) -> None:
    _act_as(ctx, _user(UserRole.EDITOR))
    resp = ctx.client.post("/b2b/keys", json={"name": "Аналитика"})
    assert resp.status_code == 403


def test_create_key_allowed_for_admin(ctx) -> None:
    admin = _user(UserRole.ADMIN)
    _act_as(ctx, admin)
    resp = ctx.client.post("/b2b/keys", json={"name": "Аналитика"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["secret"].startswith("vk_")
    assert body["key"]["daily_quota"] == 1000
    # Ключ принадлежит админу и записан факт выдачи в аудит.
    owned = ctx.repo._by_id  # noqa: SLF001 — прямой доступ к состоянию фейка
    assert len(owned) == 1
    assert next(iter(owned.values())).owner_user_id == admin.id
    assert ctx.audit.actions() == ["b2b.key.issued"]


# ── H-B2B: верхняя граница суточной квоты ─────────────────────────────────────


def test_create_key_rejects_quota_above_upper_bound(ctx) -> None:
    _act_as(ctx, _user(UserRole.ADMIN))
    resp = ctx.client.post(
        "/b2b/keys", json={"name": "Абьюз", "daily_quota": 1_000_001}
    )
    assert resp.status_code == 422
    assert ctx.audit.actions() == []


def test_create_key_accepts_quota_at_upper_bound(ctx) -> None:
    _act_as(ctx, _user(UserRole.ADMIN))
    resp = ctx.client.post(
        "/b2b/keys", json={"name": "Максимум", "daily_quota": 1_000_000}
    )
    assert resp.status_code == 201
    assert resp.json()["key"]["daily_quota"] == 1_000_000


def test_create_key_rejects_zero_quota(ctx) -> None:
    _act_as(ctx, _user(UserRole.ADMIN))
    resp = ctx.client.post(
        "/b2b/keys", json={"name": "Ноль", "daily_quota": 0}
    )
    assert resp.status_code == 422
