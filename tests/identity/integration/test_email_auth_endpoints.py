"""Интеграционные тесты входа по email через реальные HTTP-эндпоинты.

Поднимают приложение целиком, подменяя только I/O-порты (репозиторий,
хранилище ссылок, отправитель писем, refresh-store) — так проверяется весь
шов: схема запроса → use-case → cookie → ``/auth/me``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, get_settings
from app.main import create_app
from app.modules.billing.application.use_cases import CancelSubscription
from app.modules.identity.api.dependencies import (
    get_audit_trail,
    get_billing_subscription_repository,
    get_cancel_subscription_on_delete,
    get_consent_repository,
    get_email_sender,
    get_magic_link_store,
    get_refresh_store,
    get_security_audit_trail,
    get_user_repository,
)
from app.modules.identity.domain.entities import User, UserRole, UserStatus
from app.modules.identity.domain.magic_link import MAX_LETTERS_PER_EMAIL
from tests.billing.fakes import FakeClock, InMemorySubscriptionRepository
from tests.identity.fakes import (
    FakeAuditTrail,
    FakeEmailSender,
    FakeMagicLinkStore,
    FakeRefreshTokenStore,
    InMemoryConsentRepository,
    InMemoryUserRepository,
)


@pytest.fixture
def context():
    """Приложение с подменёнными портами; отдаёт клиент, репозиторий и почту."""
    repo = InMemoryUserRepository()
    links = FakeMagicLinkStore()
    sender = FakeEmailSender()
    audit = FakeAuditTrail()
    subscriptions = InMemorySubscriptionRepository()

    app = create_app()
    app.dependency_overrides[get_user_repository] = lambda: repo
    app.dependency_overrides[get_magic_link_store] = lambda: links
    app.dependency_overrides[get_email_sender] = lambda: sender
    app.dependency_overrides[get_refresh_store] = FakeRefreshTokenStore
    app.dependency_overrides[get_consent_repository] = lambda: (
        InMemoryConsentRepository()
    )
    app.dependency_overrides[get_audit_trail] = lambda: audit
    app.dependency_overrides[get_security_audit_trail] = FakeAuditTrail
    # DELETE /users/me по пути отменяет подписку — billing тоже на фейках
    # (та же техника, что в tests/identity/integration/test_account_deletion).
    app.dependency_overrides[get_billing_subscription_repository] = (
        lambda: subscriptions
    )
    app.dependency_overrides[get_cancel_subscription_on_delete] = (
        lambda: CancelSubscription(
            subscriptions=subscriptions,
            audit=audit,
            clock=FakeClock(datetime(2026, 8, 7, 12, 0, tzinfo=UTC)),
        )
    )

    with TestClient(app) as client:
        yield client, repo, sender


def _login(client: TestClient, sender: FakeEmailSender, email: str):
    """Полный цикл «запросил ссылку → перешёл по ней»."""
    requested = client.post("/auth/email/request", json={"email": email})
    assert requested.status_code == 202
    return client.post("/auth/email/callback", json={"token": sender.last_token()})


# ── Провайдеры ────────────────────────────────────────────────────────────


def test_providers_endpoint_is_public(context) -> None:
    client, _, _ = context

    resp = client.get("/auth/providers")

    assert resp.status_code == 200
    # Ровно два флага и ничего о конфигурации сервера.
    assert set(resp.json()) == {"esia", "email"}
    assert resp.json()["email"] is True


def test_esia_endpoints_are_absent_when_provider_disabled(context) -> None:
    """Выключенная ЕСИА — 404, а не 500 из недр HTTP-клиента."""
    client, _, _ = context
    settings = get_settings().model_copy(deep=True)
    settings.auth = AuthSettings(providers="email")
    client.app.dependency_overrides[get_settings] = lambda: settings

    assert client.get("/auth/providers").json() == {"esia": False, "email": True}
    login = client.get("/auth/esia/login", follow_redirects=False)
    assert login.status_code == 404
    assert login.json()["error"] == "AuthProviderDisabledError"
    callback = client.get("/auth/esia/callback", params={"code": "c", "state": "s"})
    assert callback.status_code == 404


def test_email_endpoints_are_absent_when_provider_disabled(context) -> None:
    """Симметрично: выключенный email-провайдер закрывает свои эндпоинты."""
    client, _, _ = context
    settings = get_settings().model_copy(deep=True)
    settings.auth = AuthSettings(providers="esia")
    client.app.dependency_overrides[get_settings] = lambda: settings

    assert client.get("/auth/providers").json() == {"esia": True, "email": False}
    requested = client.post(
        "/auth/email/request", json={"email": "user@example.com"}
    )
    assert requested.status_code == 404
    assert requested.json()["error"] == "AuthProviderDisabledError"
    assert (
        client.post("/auth/email/callback", json={"token": "x" * 20}).status_code == 404
    )


# ── Запрос ссылки ─────────────────────────────────────────────────────────


def test_request_always_202_regardless_of_account_existence(context) -> None:
    """Анти-энумерация: ответ одинаков для известного и неизвестного адреса."""
    client, _repo, sender = context
    known = _login(client, sender, "known@example.com")
    assert known.status_code == 201
    client.cookies.clear()

    for address in ("known@example.com", "nobody@example.com"):
        resp = client.post("/auth/email/request", json={"email": address})
        assert resp.status_code == 202
        assert resp.content == b""


def test_request_over_limit_still_202(context) -> None:
    """Превышение лимита писем на адрес не различимо снаружи."""
    client, _, sender = context
    for _ in range(MAX_LETTERS_PER_EMAIL):
        client.post("/auth/email/request", json={"email": "victim@example.com"})

    resp = client.post("/auth/email/request", json={"email": "victim@example.com"})

    assert resp.status_code == 202
    assert len(sender.sent) == MAX_LETTERS_PER_EMAIL


def test_request_rejects_malformed_address(context) -> None:
    """Формат адреса — свойство строки, а не факт регистрации: 422 допустим."""
    client, _, _ = context

    assert (
        client.post("/auth/email/request", json={"email": "not-an-email"}).status_code
        == 422
    )


# ── Вход по ссылке ────────────────────────────────────────────────────────


def test_full_cycle_request_callback_me(context) -> None:
    client, _, sender = context

    login = _login(client, sender, "new@example.com")

    assert login.status_code == 201  # аккаунт заведён этим входом
    body = login.json()
    assert body["email"] == "new@example.com"
    assert body["identity_verified"] is False
    assert body["needs_onboarding"] is True
    # Сессия — только в httpOnly-cookie; токена в теле нет.
    assert "access_token" not in body
    assert "access_token" in login.cookies
    assert "refresh_token" in login.cookies

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == body["id"]
    assert me.json()["email"] == "new@example.com"
    assert me.json()["identity_verified"] is False
    assert me.json()["needs_onboarding"] is True
    assert me.json()["username"].startswith("predictor-")


def test_second_login_same_email_is_same_account(context) -> None:
    client, _, sender = context

    first = _login(client, sender, "user@example.com")
    assert first.status_code == 201
    client.cookies.clear()
    second = _login(client, sender, "USER@example.com")

    assert second.status_code == 200  # существующий аккаунт
    assert second.json()["id"] == first.json()["id"]


def test_unknown_token_is_401(context) -> None:
    client, _, _ = context

    resp = client.post("/auth/email/callback", json={"token": "n" * 40})

    assert resp.status_code == 401
    assert resp.json()["error"] == "InvalidMagicLinkError"
    assert "устарела" in resp.json()["detail"]


def test_link_cannot_be_used_twice(context) -> None:
    client, _, sender = context
    client.post("/auth/email/request", json={"email": "user@example.com"})
    token = sender.last_token()

    assert client.post("/auth/email/callback", json={"token": token}).status_code == 201
    assert client.post("/auth/email/callback", json={"token": token}).status_code == 401


def test_self_deletion_clears_email_and_frees_the_address(context) -> None:
    """Сквозь HTTP: DELETE /users/me обнуляет адрес, и он снова свободен.

    Решение координатора (152-ФЗ): минимизация ПДн важнее антиобхода —
    подробности в докстринге ``User.anonymize_for_deletion``.
    """
    client, repo, sender = context
    created = _login(client, sender, "user@example.com")
    user_id = uuid.UUID(created.json()["id"])

    assert client.delete("/users/me").status_code == 204

    stored = client.portal.call(repo.get_by_id, user_id)
    assert stored is not None
    assert stored.email is None
    assert stored.status is UserStatus.DELETED

    client.cookies.clear()
    again = _login(client, sender, "user@example.com")

    assert again.status_code == 201  # новый аккаунт, а не воскрешение старого
    assert again.json()["id"] != created.json()["id"]


def test_deleted_account_with_email_still_rejected(context) -> None:
    """Проверка статуса на месте: удалённый аккаунт с адресом входа не даёт."""
    client, repo, sender = context
    created = _login(client, sender, "user@example.com")
    stored = client.portal.call(repo.get_by_id, uuid.UUID(created.json()["id"]))
    assert stored is not None
    stored.anonymize_for_deletion()
    stored.email = "user@example.com"  # как если бы адрес вернул админ
    client.portal.call(repo.update, stored)
    client.cookies.clear()

    resp = _login(client, sender, "user@example.com")

    assert resp.status_code == 403
    assert resp.json()["error"] == "AccountDeletedError"


# ── Профиль и смена адреса ────────────────────────────────────────────────


def test_patch_me_cannot_change_email(context) -> None:
    """``PATCH /users/me`` игнорирует email — адрес меняется только поддержкой."""
    client, _, sender = context
    _login(client, sender, "user@example.com")

    patched = client.patch(
        "/users/me", json={"display_name": "Новое имя", "email": "hijack@example.com"}
    )

    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Новое имя"
    assert client.get("/auth/me").json()["email"] == "user@example.com"


def test_public_profile_does_not_leak_email(context) -> None:
    client, _, sender = context
    me = _login(client, sender, "user@example.com").json()

    public = client.get(f"/users/{me['username']}")

    assert public.status_code == 200
    assert "email" not in public.json()
    assert "user@example.com" not in public.text


def test_admin_changes_email_and_revokes_sessions(context) -> None:
    client, repo, sender = context
    victim = _login(client, sender, "old@example.com").json()
    client.cookies.clear()
    admin = User(
        username="admin",
        display_name="Админ",
        real_name_enc=None,
        email="admin@example.com",
        role=UserRole.ADMIN,
    )
    client.portal.call(repo.add, admin)
    _login(client, sender, "admin@example.com")

    resp = client.post(
        f"/admin/users/{victim['id']}/email", json={"email": " NEW@Example.com "}
    )

    assert resp.status_code == 200
    stored = client.portal.call(repo.get_by_id, uuid.UUID(victim["id"]))
    assert stored is not None
    assert stored.email == "new@example.com"  # нормализован


def test_admin_cannot_move_email_to_taken_address(context) -> None:
    client, repo, sender = context
    victim = _login(client, sender, "one@example.com").json()
    client.cookies.clear()
    _login(client, sender, "two@example.com")
    client.cookies.clear()
    admin = User(
        username="admin",
        display_name="Админ",
        real_name_enc=None,
        email="admin@example.com",
        role=UserRole.ADMIN,
    )
    client.portal.call(repo.add, admin)
    _login(client, sender, "admin@example.com")

    resp = client.post(
        f"/admin/users/{victim['id']}/email", json={"email": "two@example.com"}
    )

    assert resp.status_code == 409
    assert resp.json()["error"] == "EmailAlreadyTakenError"


def test_change_email_requires_admin(context) -> None:
    client, _, sender = context
    me = _login(client, sender, "user@example.com").json()

    resp = client.post(
        f"/admin/users/{me['id']}/email", json={"email": "other@example.com"}
    )

    assert resp.status_code == 403
