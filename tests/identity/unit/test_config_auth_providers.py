"""Конфигурация способов входа (``AUTH_PROVIDERS``).

Ключевое требование момента: приложение обязано стартовать БЕЗ переменных
``ESIA_*``, пока договор с интегратором не заключён, — но как только ЕСИА
включают обратно, её настройки снова обязательны (fail-fast, как у платёжных
провайдеров).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import AuthSettings, EsiaSettings, MailSettings, Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "local",
        "database_url": "postgresql+asyncpg://x/x",
    }
    base.update(overrides)
    return Settings(**base)


def test_default_is_email_only() -> None:
    """Дефолт — только email: ЕСИА выключена до заключения договора.

    Проверяем именно дефолт поля, а не ``AuthSettings()``: тестовое окружение
    (``tests/conftest.py``) намеренно включает оба провайдера через env,
    чтобы ЕСИА-тесты оставались осмысленными.
    """
    default = AuthSettings.model_fields["providers"].default
    auth = AuthSettings(providers=default)

    assert auth.email_enabled is True
    assert auth.esia_enabled is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("email", {"email"}),
        ("esia", {"esia"}),
        ("esia,email", {"esia", "email"}),
        (" Email , ESIA ", {"email", "esia"}),
        ("email,,email", {"email"}),
    ],
)
def test_providers_parsing(raw: str, expected: set[str]) -> None:
    assert set(AuthSettings(providers=raw).provider_set) == expected


def test_unknown_provider_rejected() -> None:
    """Опечатка не должна молча оставить платформу без входа."""
    with pytest.raises(ValueError, match="AUTH_PROVIDERS"):
        AuthSettings(providers="emails")


def test_empty_providers_rejected() -> None:
    with pytest.raises(ValueError, match="AUTH_PROVIDERS"):
        AuthSettings(providers="  ")


def test_app_starts_without_esia_settings_at_all() -> None:
    """Главный сценарий: ни одной переменной ESIA_* — и приложение поднимается."""
    settings = _settings(
        auth=AuthSettings(providers="email"),
        esia=EsiaSettings(
            client_id="",
            redirect_uri="",
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_url="",
            issuer="",
        ),
    )

    assert settings.auth.esia_enabled is False
    assert settings.esia.client_id == ""


def test_prod_starts_without_esia_when_provider_disabled() -> None:
    """Вне local пустой ESIA_JWKS_URL допустим, если ЕСИА выключена."""
    settings = _settings(
        app_env="prod",
        auth=AuthSettings(providers="email"),
        esia=EsiaSettings(jwks_url="", issuer=""),
        billing={"checkout_provider": "tbank"},
        tbank={"enabled": True, "terminal_key": "T", "password": "p"},
    )

    assert settings.esia.verify_id_token is False


def test_enabled_esia_requires_its_settings() -> None:
    """Включили ЕСИА — реквизиты снова обязательны (fail-fast)."""
    with pytest.raises(ValueError, match="ESIA_CLIENT_ID"):
        _settings(
            auth=AuthSettings(providers="esia,email"),
            esia=EsiaSettings(
                client_id="",
                redirect_uri="https://veraks.ru/auth/esia/callback",
                authorization_endpoint="https://gw.example/authorize",
                token_endpoint="https://gw.example/token",
                userinfo_endpoint="https://gw.example/userinfo",
                jwks_url="",
                issuer="",
            ),
        )


def test_mail_unconfigured_is_not_a_startup_failure() -> None:
    """Пустой MAIL_HOST вне local НЕ валит старт — только ERROR в логах.

    Обратное решение (fail-fast) означало бы, что при отсутствии SMTP-учётки
    платформа не поднимается вовсе, а вход по email — единственный.
    """
    settings = _settings(
        app_env="prod",
        auth=AuthSettings(providers="email"),
        mail=MailSettings(host=""),
        billing={"checkout_provider": "tbank"},
        tbank={"enabled": True, "terminal_key": "T", "password": "p"},
    )

    assert settings.mail.configured is False
