"""Fail-fast выбора платёжных провайдеров (T8, находка аудита №1).

Вне ``APP_ENV=local`` приложение не должно подниматься с провайдером,
которого фактически нет (мёртвые ЮKassa-адаптеры) или который настроен не
полностью — ошибка конфигурации обязана валить старт, а не проявляться в
рантайме на первом платеже/выплате.
"""

from __future__ import annotations

import pytest

from app.config import BillingSettings, JumpSettings, Settings, TBankSettings

_TBANK_FULL = TBankSettings(enabled=True, terminal_key="TDEMO", password="p")
_JUMP_FULL = JumpSettings(enabled=True, api_key="key", agent_id=1)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "prod",
        "database_url": "postgresql+asyncpg://x/x",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_local_env_works_as_before_without_any_provider_config() -> None:
    """local — валидатор не применяется, дефолты (local/manual) допустимы."""
    s = Settings(app_env="local", database_url="postgresql+asyncpg://x/x")
    assert s.billing.checkout_provider == "local"
    assert s.billing.payout_provider == "manual"


def test_non_local_rejects_default_local_checkout() -> None:
    """Вне local BILLING_CHECKOUT_PROVIDER по умолчанию (``local``) — ошибка старта."""
    with pytest.raises(ValueError, match="BILLING_CHECKOUT_PROVIDER"):
        _settings()


def test_non_local_tbank_checkout_requires_enabled_flag() -> None:
    with pytest.raises(ValueError, match="TBANK_ENABLED"):
        _settings(
            billing=BillingSettings(checkout_provider="tbank"),
            tbank=TBankSettings(enabled=False, terminal_key="T", password="p"),
        )


def test_non_local_tbank_checkout_requires_full_credentials() -> None:
    with pytest.raises(ValueError, match="TBANK_TERMINAL_KEY.*TBANK_PASSWORD|TBANK_PASSWORD.*TBANK_TERMINAL_KEY"):
        _settings(
            billing=BillingSettings(checkout_provider="tbank"),
            tbank=TBankSettings(enabled=True),
        )


def test_non_local_full_tbank_checkout_and_manual_payout_passes() -> None:
    s = _settings(
        billing=BillingSettings(checkout_provider="tbank"),
        tbank=_TBANK_FULL,
    )
    assert s.billing.checkout_provider == "tbank"
    assert s.billing.payout_provider == "manual"


def test_non_local_jump_payout_requires_full_credentials() -> None:
    with pytest.raises(ValueError, match="JUMP_API_KEY"):
        _settings(
            billing=BillingSettings(checkout_provider="tbank", payout_provider="jump"),
            tbank=_TBANK_FULL,
            jump=JumpSettings(enabled=True),
        )


def test_non_local_full_tbank_and_jump_passes() -> None:
    s = _settings(
        billing=BillingSettings(checkout_provider="tbank", payout_provider="jump"),
        tbank=_TBANK_FULL,
        jump=_JUMP_FULL,
    )
    assert s.billing.payout_provider == "jump"
    assert s.jump.enabled is True
