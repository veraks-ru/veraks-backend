"""Адаптеры платёжных шлюзов billing.

TODO(billing-infra): здесь подключаются реальные интеграции — ЮKassa
(рекурренты подписок, выплаты физлицам), СБП, T-Bank. Пока это точки стыка:
реализации поднимают ``NotImplementedError``, чтобы не было «тихих» заглушек в
проде. В тестах порты подменяются фейками через ``dependency_overrides``.

Раздельные договоры/счета операционки и приза — на уровне этих адаптеров
(зеркало раздельного ledger'а): подписочный эквайринг и выплаты из призового
фонда идут через разные конфигурации провайдера.
"""

from __future__ import annotations

import uuid

from app.modules.billing.ports.gateways import CheckoutIntent, PayoutInstruction


class YookassaSubscriptionCheckoutGateway:
    """Создание рекуррентной оплаты подписки в ЮKassa (операционная касса)."""

    async def create_checkout(
        self, *, subscription_id: uuid.UUID, amount_kopecks: int, description: str
    ) -> CheckoutIntent:
        """TODO(billing-infra): создать платёж ЮKassa и вернуть confirmation_url."""
        raise NotImplementedError(
            "Интеграция с ЮKassa для подписок ещё не подключена (billing-infra)"
        )


class YookassaPayoutGateway:
    """Отправка выплаты физлицу через ЮKassa Payouts/СБП (призовая касса)."""

    async def send_payout(
        self, *, payout_id: uuid.UUID, user_id: uuid.UUID, amount_kopecks: int
    ) -> PayoutInstruction:
        """TODO(billing-infra): инициировать выплату и вернуть provider_payout_id."""
        raise NotImplementedError(
            "Интеграция выплат физлицам ещё не подключена (billing-infra)"
        )
