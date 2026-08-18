"""Адаптеры платёжных шлюзов billing, не завязанные на внешний HTTP.

Раздельные договоры/счета операционки и приза — на уровне этих адаптеров
(зеркало раздельного ledger'а): подписочный эквайринг и выплаты из призового
фонда идут через разные конфигурации провайдера.

Реальные интеграции (ТБанк — эквайринг, Jump.Finance — выплаты) живут в
соседних ``tbank_gateway.py``/``jump_gateway.py``; здесь — заглушки для
явных, не-«провайдерских» режимов (``local``/``manual``), которые
composition root (``api/dependencies.py``) выбирает по
``BILLING_CHECKOUT_PROVIDER``/``BILLING_PAYOUT_PROVIDER``.
"""

from __future__ import annotations

import uuid

from app.modules.billing.domain.errors import ManualPayoutDispatchError
from app.modules.billing.ports.gateways import (
    CheckoutIntent,
    PayoutInstruction,
    PayoutRecipient,
)


class LocalSubscriptionCheckoutGateway:
    """Локальная заглушка оплаты (``BILLING_CHECKOUT_PROVIDER=local``).

    Возвращает фиктивный intent; активация подписки происходит сразу в
    ``StartSubscription`` (``instant_activate``). Допустима только при
    ``APP_ENV=local`` — вне local валидатор ``Settings`` не даст подняться с
    этим провайдером.
    """

    async def create_checkout(
        self,
        *,
        subscription_id: uuid.UUID,
        amount_kopecks: int,
        description: str,
        customer_key: str | None = None,
    ) -> CheckoutIntent:
        return CheckoutIntent(
            provider_subscription_id=f"local-{subscription_id}",
            confirmation_url=f"local://subscription/{subscription_id}",
        )

    async def charge_recurrent(
        self,
        *,
        subscription_id: uuid.UUID,
        amount_kopecks: int,
        description: str,
        rebill_id: str,
        customer_key: str,
    ) -> str:
        """Локально списывать нечего — возвращаем правдоподобный идентификатор."""
        return f"local-charge-{subscription_id}"


class ManualPayoutGateway:
    """Выплаты вручную (``BILLING_PAYOUT_PROVIDER=manual``): без провайдера.

    Проводка в кассе PRIZE уже сделана на шаге подтверждения выплаты —
    получателю переводят деньги вне системы (по реквизитам СБП из
    ``PayoutRequisites``). Попытка автоматической отправки через API
    (``/admin/payouts/{id}/dispatch``) в этом режиме — явная доменная ошибка,
    а не тихое падение в несуществующего провайдера.
    """

    async def send_payout(
        self,
        *,
        payout_id: uuid.UUID,
        user_id: uuid.UUID,
        amount_kopecks: int,
        recipient: PayoutRecipient,
    ) -> PayoutInstruction:
        raise ManualPayoutDispatchError(
            "Выплаты в этом окружении отправляются вручную "
            "(BILLING_PAYOUT_PROVIDER=manual) — автоматическая отправка "
            "провайдеру недоступна, переведите средства получателю напрямую "
            "по его реквизитам СБП."
        )
