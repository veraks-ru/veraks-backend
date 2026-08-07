"""Интеграционные тесты HTTP-эндпоинтов `/auth`.

Поднимают реальное FastAPI-приложение, но порты I/O (репозиторий, шлюз ЕСИА,
state/refresh-хранилища) подменяются in-memory фейками через
``dependency_overrides``. Крипто-порты и настройки берутся из тест-окружения.
БД-интеграция с Postgres покрывается отдельно (см. TODO ниже).

TODO(identity-infra): добавить end-to-end тест против реального Postgres
(testcontainers) для проверки UNIQUE-constraint'ов и enum-типов миграции.
"""

from __future__ import annotations

import uuid
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
    get_security_audit_trail,
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


@pytest.fixture
def context(confirmed_identity):
    """Приложение с подменёнными портами и общими фейками."""
    repo = InMemoryUserRepository()
    state_store = FakeStateStore()
    refresh_store = FakeRefreshTokenStore()
    gateway = FakeEsiaGateway(confirmed_identity)
    consents = InMemoryConsentRepository()
    audit = FakeAuditTrail()
    # Отдельный фейк для get_security_audit_trail (Critical-1, T10 фикс-раунд
    # 1): в проде это НЕ сессия запроса (см. её докстринг) — здесь достаточно
    # фейка, чтобы проверить, что RefreshSession вызывает именно эту
    # зависимость с правильными данными; переживание реального отката
    # транзакции проверяется e2e (tests/e2e/test_audit_chain.py).
    security_audit = FakeAuditTrail()

    app = create_app()
    app.dependency_overrides[get_user_repository] = lambda: repo
    app.dependency_overrides[get_esia_gateway] = lambda: gateway
    app.dependency_overrides[get_state_store] = lambda: state_store
    app.dependency_overrides[get_refresh_store] = lambda: refresh_store
    app.dependency_overrides[get_consent_repository] = lambda: consents
    app.dependency_overrides[get_audit_trail] = lambda: audit
    app.dependency_overrides[get_security_audit_trail] = lambda: security_audit

    with TestClient(app) as client:
        client.security_audit = security_audit  # type: ignore[attr-defined]
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
    client, _repo, _ = context
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
    client, _repo, _ = context

    state1 = _login_and_get_state(client)
    first = client.get("/auth/esia/callback", params={"code": "a", "state": state1})
    assert first.status_code == 201

    state2 = _login_and_get_state(client)
    second = client.get("/auth/esia/callback", params={"code": "b", "state": state2})
    assert second.status_code == 200  # существующий аккаунт, не создан новый


async def test_deleted_account_login_rejected(context) -> None:
    """Повторный вход через ЕСИА тем же СНИЛС после удаления аккаунта — отказ (T4).

    ``ensure_account_can_authenticate`` уже запрещает вход в ``DELETED``-аккаунт;
    здесь фиксируем это тестом сквозь реальный HTTP-эндпоинт callback'а, а не
    только на уровне use-case (см. ``tests/identity/unit/test_use_cases.py``).
    """
    client, repo, _ = context

    state1 = _login_and_get_state(client)
    created = client.get("/auth/esia/callback", params={"code": "a", "state": state1})
    assert created.status_code == 201
    access = created.json()["access_token"]
    user_id = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {access}"}
    ).json()["id"]

    stored = await repo.get_by_id(uuid.UUID(user_id))
    assert stored is not None
    assert stored.anonymize_for_deletion() is True
    await repo.update(stored)

    state2 = _login_and_get_state(client)
    resp = client.get("/auth/esia/callback", params={"code": "b", "state": state2})

    assert resp.status_code == 403
    assert resp.json()["error"] == "AccountDeletedError"
    assert "удал" in resp.json()["detail"].lower()


def test_login_url_carries_pkce_and_nonce(context) -> None:
    """Шаг login отдаёт URL с code_challenge/S256 и nonce (B5/B6)."""
    client, _, gateway = context

    resp = client.get("/auth/esia/login", follow_redirects=False)

    assert resp.status_code == 307
    location = resp.headers["location"]
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert "nonce=" in location
    # Секреты потока сгенерированы и НЕ отданы клиенту: наружу ушёл только state.
    _state, verifier, nonce = gateway.authorize_args[0]
    assert verifier and nonce
    assert verifier not in location and verifier not in str(resp.cookies)


def test_callback_passes_stored_flow_secrets_to_gateway(context) -> None:
    """На callback'е из стора достаются ИМЕННО те code_verifier/nonce, что на login."""
    client, _, gateway = context

    state = _login_and_get_state(client)
    resp = client.get("/auth/esia/callback", params={"code": "abc", "state": state})

    assert resp.status_code == 201
    _, verifier, nonce = gateway.authorize_args[0]
    assert gateway.exchange_args == [("abc", verifier, nonce)]


def test_callback_user_denied_access(context) -> None:
    """Отказ пользователя в Госуслугах → 403 с человеческой ошибкой, а не 422."""
    client, _, gateway = context
    _login_and_get_state(client)

    resp = client.get(
        "/auth/esia/callback",
        params={
            "error": "access_denied",
            "error_description": "user cancelled",
            "state": "whatever",
        },
    )

    assert resp.status_code == 403
    assert resp.json()["error"] == "EsiaAuthorizationDeniedError"
    assert "отмен" in resp.json()["detail"].lower()
    # Обмена кода не было: до шлюза запрос не дошёл.
    assert gateway.exchange_args == []


def test_callback_provider_error_is_gateway_failure(context) -> None:
    """Прочие OIDC-ошибки (сбой провайдера) — 502, это не отмена пользователем."""
    client, _, _ = context

    resp = client.get(
        "/auth/esia/callback", params={"error": "server_error", "state": "x"}
    )

    assert resp.status_code == 502
    assert resp.json()["error"] == "EsiaExchangeError"
    assert "server_error" in resp.json()["detail"]


def test_callback_does_not_echo_unknown_error_code(context) -> None:
    """Сырой ``error`` из query-string не попадает в ответ (отражённый текст)."""
    client, _, _ = context

    resp = client.get(
        "/auth/esia/callback",
        params={"error": "<script>alert(1)</script>", "state": "x"},
    )

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "script" not in detail
    assert "unknown" in detail


def test_callback_rejects_oversized_error_code(context) -> None:
    """Длина ``error`` ограничена схемой — мусор не доезжает до обработчика."""
    client, _, _ = context

    resp = client.get("/auth/esia/callback", params={"error": "x" * 100, "state": "x"})

    assert resp.status_code == 422


def test_callback_without_code_and_without_error_is_400(context) -> None:
    """Пустой callback — 400 с внятным текстом (раньше схема давала 422)."""
    client, _, _ = context

    resp = client.get("/auth/esia/callback", params={"state": "x"})

    assert resp.status_code == 400
    assert "код" in resp.json()["detail"].lower()


def _set_cookie_headers(resp) -> list[str]:
    """Все заголовки Set-Cookie ответа."""
    return resp.headers.get_list("set-cookie")


def test_cookies_are_deleted_with_configured_domain(context) -> None:
    """Удаление cookie идёт с тем же ``domain``, что и установка.

    Регрессия: при ``SECURITY_COOKIE_DOMAIN=.veraks.ru`` ``delete_cookie`` без
    ``domain`` ставил Set-Cookie на хост api-домена, а cookie родительского
    домена оставалась жить — logout «не срабатывал».
    """
    client, _, _ = context
    settings = get_settings().model_copy(deep=True)
    settings.security.cookie_domain = ".veraks.test"
    client.app.dependency_overrides[get_settings] = lambda: settings

    state = _login_and_get_state(client)
    # Cookie домена .veraks.test клиент к хосту testserver сам не пошлёт —
    # подставляем заголовок вручную (в браузере это делает сам домен).
    callback = client.get(
        "/auth/esia/callback",
        params={"code": "abc", "state": state},
        headers={"Cookie": f"oidc_state={state}"},
    )
    assert callback.status_code == 201
    # Гашение служебной state-cookie — тоже с доменом.
    state_cookie = [h for h in _set_cookie_headers(callback) if h.startswith("oidc_state=")]
    assert state_cookie and "Domain=.veraks.test" in state_cookie[0]

    logout = client.post("/auth/logout")
    assert logout.status_code == 204
    deletions = {
        h.split("=", 1)[0]: h
        for h in _set_cookie_headers(logout)
        if h.startswith(("access_token=", "refresh_token="))
    }
    assert set(deletions) == {"access_token", "refresh_token"}
    for header in deletions.values():
        assert "Domain=.veraks.test" in header
    assert "Path=/auth" in deletions["refresh_token"]


def test_refresh_reuse_writes_security_audit_before_401(context) -> None:
    """Повтор уже ротированного refresh → 401, но событие безопасности пишется.

    Critical-1 (T10, ревью, фикс-раунд 1): раньше запись
    ``identity.refresh.reuse_detected`` уходила через сессию запроса и
    откатывалась вместе с последующим ``InvalidTokenError`` (``get_session``
    делает rollback при исключении) — след инцидента терялся. Здесь
    проверяем именно бизнес-wiring (``get_security_audit_trail`` реально
    вызывается use-case'ом с правильными данными); что запись переживает
    настоящий откат транзакции — доказывает
    ``tests/e2e/test_audit_chain.py`` (нужен реальный Postgres).
    """
    client, _, _ = context

    state = _login_and_get_state(client)
    client.get("/auth/esia/callback", params={"code": "abc", "state": state})
    old_refresh_cookie = client.cookies.get("refresh_token")
    assert old_refresh_cookie

    first = client.post("/auth/refresh")
    assert first.status_code == 200
    assert client.security_audit.actions() == []  # обычная ротация — не событие безопасности

    # Подсовываем СТАРЫЙ (уже ротированный) refresh — детект повторного использования.
    client.cookies.set("refresh_token", old_refresh_cookie)
    reused = client.post("/auth/refresh")

    assert reused.status_code == 401
    assert client.security_audit.actions() == ["identity.refresh.reuse_detected"]
