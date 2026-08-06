"""Fail-fast конфигурации ЕСИА (T12).

Режим «доверие каналу» (пустой ``ESIA_JWKS_URL``) означает, что ``id_token``
принимается без проверки подписи — подмена ответа token-эндпоинта даёт вход
под чужой учётной записью. Локально с моком это осознанный компромисс, вне
``local`` — приложение не должно подниматься вовсе (тот же паттерн, что у
платёжных провайдеров, см. tests/billing/unit/test_config_billing_providers).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import (
    BillingSettings,
    EsiaSettings,
    JumpSettings,
    Settings,
    TBankSettings,
)

_TBANK_FULL = TBankSettings(enabled=True, terminal_key="TDEMO", password="p")


def _esia(**overrides: Any) -> EsiaSettings:
    base: dict[str, Any] = {
        "client_id": "orakul",
        "redirect_uri": "https://veraks.ru/auth/esia/callback",
        "authorization_endpoint": "https://esia-gateway.example/authorize",
        "token_endpoint": "https://esia-gateway.example/token",
        "userinfo_endpoint": "https://esia-gateway.example/userinfo",
        "issuer": "https://esia-gateway.example",
        "jwks_url": "https://esia-gateway.example/jwks",
    }
    base.update(overrides)
    return EsiaSettings(**base)


def _prod_settings(**overrides: Any) -> Settings:
    """Настройки боевого окружения с валидными платёжными провайдерами."""
    base: dict[str, Any] = {
        "app_env": "prod",
        "database_url": "postgresql+asyncpg://x/x",
        "billing": BillingSettings(checkout_provider="tbank", payout_provider="manual"),
        "tbank": _TBANK_FULL,
        "jump": JumpSettings(),
        "esia": _esia(),
    }
    base.update(overrides)
    return Settings(**base)


def test_local_allows_trust_channel_mode() -> None:
    """В local пустой JWKS допустим — иначе не поднять мок ЕСИА."""
    s = Settings(
        app_env="local",
        database_url="postgresql+asyncpg://x/x",
        esia=_esia(jwks_url="", issuer=""),
    )
    assert s.esia.verify_id_token is False


def test_non_local_rejects_empty_jwks_url() -> None:
    with pytest.raises(ValueError, match="ESIA_JWKS_URL"):
        _prod_settings(esia=_esia(jwks_url=""))


def test_non_local_rejects_empty_issuer() -> None:
    """Без ожидаемого iss проверка неполна — тоже валим старт."""
    with pytest.raises(ValueError, match="ESIA_ISSUER"):
        _prod_settings(esia=_esia(issuer="   "))


def test_jwks_without_issuer_rejected_even_in_local() -> None:
    """JWKS задан, issuer пуст — ни один маркер не пройдёт; это ошибка конфига.

    Проверка привязана к включённости JWKS, а не к окружению: иначе локально
    это выглядело бы как невнятный отказ во входе.
    """
    with pytest.raises(ValueError, match="ESIA_ISSUER"):
        Settings(
            app_env="local",
            database_url="postgresql+asyncpg://x/x",
            esia=_esia(issuer=""),
        )


def test_non_local_with_full_esia_config_passes() -> None:
    s = _prod_settings()
    assert s.esia.verify_id_token is True
    assert s.esia.id_token_algorithm_list == ["RS256"]
