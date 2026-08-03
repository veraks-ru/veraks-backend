"""Настройки выплат Jump.Finance (JumpSettings) — env и fail-closed в проде."""

import pytest

from app.config import BillingSettings, JumpSettings, Settings, TBankSettings

# Вне local checkout обязателен (tbank) — минимальный валидный набор настроек,
# чтобы тесты ниже проверяли именно требования к Jump, а не к чекауту.
_TBANK_OK = TBankSettings(enabled=True, terminal_key="TDEMO", password="p")


def test_jump_settings_defaults_and_env(monkeypatch):
    monkeypatch.setenv("JUMP_API_KEY", "jump-client-key")
    monkeypatch.setenv("JUMP_AGENT_ID", "42")
    s = JumpSettings()
    assert s.api_key == "jump-client-key"
    assert s.agent_id == 42
    assert s.api_base_url == "https://api.jump.finance/services/openapi"
    assert s.enabled is False
    assert s.bank_account_id is None
    # Выплаты — физлицам: НДФЛ удерживает платформа (решение продукта).
    assert s.legal_form_id == 1


def test_prod_requires_jump_key_and_agent_when_payout_provider_is_jump():
    with pytest.raises(ValueError, match="JUMP_API_KEY"):
        Settings(
            app_env="prod",
            database_url="postgresql+asyncpg://x/x",
            billing=BillingSettings(
                checkout_provider="tbank", payout_provider="jump"
            ),
            tbank=_TBANK_OK,
            jump=JumpSettings(enabled=True),
        )


def test_prod_allows_manual_payout_without_jump_secrets():
    s = Settings(
        app_env="prod",
        database_url="postgresql+asyncpg://x/x",
        billing=BillingSettings(checkout_provider="tbank"),
        tbank=_TBANK_OK,
    )
    assert s.billing.payout_provider == "manual"
    assert s.jump.enabled is False
