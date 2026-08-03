"""Composition root billing: выбор шлюза по явному провайдеру (T8).

До фикса ``get_checkout_gateway``/``get_payout_gateway`` при незаданных
``tbank.enabled``/``jump.enabled`` молча уходили в мёртвые ЮKassa-адаптеры
(``NotImplementedError`` только в рантайме). Теперь выбор — по явному
``billing.checkout_provider``/``billing.payout_provider``, без ЮKassa вовсе.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.config import BillingSettings, JumpSettings, Settings, TBankSettings
from app.modules.billing.adapters.gateways import (
    LocalSubscriptionCheckoutGateway,
    ManualPayoutGateway,
)
from app.modules.billing.adapters.jump_gateway import JumpGateway
from app.modules.billing.adapters.tbank_gateway import TBankGateway
from app.modules.billing.api.dependencies import get_checkout_gateway, get_payout_gateway
from app.modules.billing.domain.errors import ManualPayoutDispatchError


def _settings(**billing_overrides: object) -> Settings:
    return Settings(
        app_env="local",
        database_url="postgresql+asyncpg://x/x",
        billing=BillingSettings(**billing_overrides),  # type: ignore[arg-type]
        tbank=TBankSettings(enabled=True, terminal_key="T", password="p"),
        jump=JumpSettings(enabled=True, api_key="k", agent_id=1),
    )


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def test_checkout_local_provider_returns_local_gateway(client: httpx.AsyncClient) -> None:
    gateway = get_checkout_gateway(_settings(checkout_provider="local"), client)
    assert isinstance(gateway, LocalSubscriptionCheckoutGateway)


def test_checkout_tbank_provider_returns_tbank_gateway(client: httpx.AsyncClient) -> None:
    gateway = get_checkout_gateway(_settings(checkout_provider="tbank"), client)
    assert isinstance(gateway, TBankGateway)


def test_payout_manual_provider_returns_manual_gateway(client: httpx.AsyncClient) -> None:
    gateway = get_payout_gateway(_settings(payout_provider="manual"), client)
    assert isinstance(gateway, ManualPayoutGateway)


def test_payout_jump_provider_returns_jump_gateway(client: httpx.AsyncClient) -> None:
    gateway = get_payout_gateway(_settings(payout_provider="jump"), client)
    assert isinstance(gateway, JumpGateway)


async def test_manual_payout_gateway_raises_explicit_domain_error() -> None:
    """Нет мёртвого фолбэка: явная доменная ошибка, а не NotImplementedError."""
    gateway = ManualPayoutGateway()
    from app.modules.billing.ports.gateways import PayoutRecipient

    with pytest.raises(ManualPayoutDispatchError):
        await gateway.send_payout(
            payout_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            amount_kopecks=1_000,
            recipient=PayoutRecipient(
                phone="+79001234567",
                last_name="Иванов",
                first_name="Пётр",
                middle_name=None,
                sbp_bank_id="100000000004",
            ),
        )
