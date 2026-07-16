"""Настройки выплат Jump.Finance (JumpSettings) — env и fail-closed в проде."""

import pytest

from app.config import JumpSettings, Settings, WebhookSettings


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


def test_prod_requires_jump_key_and_agent_when_enabled():
    with pytest.raises(ValueError, match="JUMP_API_KEY"):
        Settings(
            app_env="prod",
            database_url="postgresql+asyncpg://x/x",
            webhooks=WebhookSettings(
                yookassa_payment_secret="s1", yookassa_payout_secret="s2"
            ),
            jump=JumpSettings(enabled=True),
        )


def test_prod_allows_disabled_jump_without_secrets():
    s = Settings(
        app_env="prod",
        database_url="postgresql+asyncpg://x/x",
        webhooks=WebhookSettings(
            yookassa_payment_secret="s1", yookassa_payout_secret="s2"
        ),
        jump=JumpSettings(enabled=False),
    )
    assert s.jump.enabled is False
