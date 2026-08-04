"""Интеграционные тесты `DELETE /users/me` — самостоятельное удаление (T4, 152-ФЗ).

Поднимают реальное приложение; порты identity и (для отмены автопродления)
billing подменяются in-memory фейками через ``dependency_overrides`` — та же
техника, что используют tests/events для кросс-доменной композиции
(``get_lock_event_predictions``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.billing.application.use_cases import CancelSubscription
from app.modules.billing.domain.entities import (
    PaymentProvider,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.identity.api.dependencies import (
    get_audit_trail,
    get_billing_subscription_repository,
    get_cancel_subscription_on_delete,
    get_consent_repository,
    get_esia_gateway,
    get_refresh_store,
    get_state_store,
    get_user_repository,
)
from tests.billing.fakes import FakeClock, InMemorySubscriptionRepository
from tests.identity.fakes import (
    FakeAuditTrail,
    FakeEsiaGateway,
    FakeRefreshTokenStore,
    FakeStateStore,
    InMemoryConsentRepository,
    InMemoryUserRepository,
)

_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def context(confirmed_identity):
    repo = InMemoryUserRepository()
    state_store = FakeStateStore()
    refresh_store = FakeRefreshTokenStore()
    gateway = FakeEsiaGateway(confirmed_identity)
    consents = InMemoryConsentRepository()
    audit = FakeAuditTrail()
    subscriptions = InMemorySubscriptionRepository()

    app = create_app()
    app.dependency_overrides[get_user_repository] = lambda: repo
    app.dependency_overrides[get_esia_gateway] = lambda: gateway
    app.dependency_overrides[get_state_store] = lambda: state_store
    app.dependency_overrides[get_refresh_store] = lambda: refresh_store
    app.dependency_overrides[get_consent_repository] = lambda: consents
    app.dependency_overrides[get_audit_trail] = lambda: audit
    app.dependency_overrides[get_billing_subscription_repository] = (
        lambda: subscriptions
    )
    app.dependency_overrides[get_cancel_subscription_on_delete] = (
        lambda: CancelSubscription(
            subscriptions=subscriptions, audit=audit, clock=FakeClock(_NOW)
        )
    )

    with TestClient(app) as client:
        yield client, repo, subscriptions, audit


def _login(client: TestClient) -> str:
    """Проходит OIDC-поток и возвращает access-токен нового пользователя."""
    resp = client.get("/auth/esia/login", follow_redirects=False)
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    callback = client.get(
        "/auth/esia/callback", params={"code": "abc", "state": state}
    )
    assert callback.status_code == 201
    return callback.json()["access_token"]


def test_delete_me_requires_auth(context) -> None:
    client, _, _, _ = context
    assert client.delete("/users/me").status_code == 401


def test_delete_me_returns_204_and_ends_session(context) -> None:
    client, repo, _, audit = context
    _login(client)  # cookie-сессия выставлена TestClient'ом

    resp = client.delete("/users/me")
    assert resp.status_code == 204
    # Cookie сессии сброшены (как при logout).
    assert "access_token" not in client.cookies
    assert "refresh_token" not in client.cookies

    # Живой (ещё не истёкший) access-токен больше не аутентифицирует.
    me = client.get("/auth/me")
    assert me.status_code == 401

    assert audit.actions() == ["identity.user.deleted"]


async def test_delete_me_anonymizes_profile(context) -> None:
    client, repo, _, _ = context
    access = _login(client)
    user_id = uuid.UUID(
        client.get("/auth/me", headers={"Authorization": f"Bearer {access}"}).json()[
            "id"
        ]
    )

    resp = client.delete("/users/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 204

    stored = await repo.get_by_id(user_id)
    assert stored is not None
    assert stored.status.value == "deleted"
    assert stored.real_name_enc is None
    assert stored.display_name == "Удалённый аккаунт"
    assert stored.username == f"deleted-{user_id.hex[:8]}"


async def test_delete_me_cancels_active_subscription(context) -> None:
    client, repo, subscriptions, _ = context
    access = _login(client)
    user_id = uuid.UUID(
        client.get("/auth/me", headers={"Authorization": f"Bearer {access}"}).json()[
            "id"
        ]
    )
    subscription = Subscription(
        user_id=user_id,
        plan=SubscriptionPlan.MONTHLY,
        price_kopecks=29900,
        provider=PaymentProvider.TBANK,
        status=SubscriptionStatus.ACTIVE,
    )
    await subscriptions.add(subscription)

    resp = client.delete("/users/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 204

    stored_subscription = await subscriptions.get_by_id(subscription.id)
    assert stored_subscription is not None
    assert stored_subscription.status is SubscriptionStatus.CANCELED
    assert stored_subscription.canceled_at is not None
