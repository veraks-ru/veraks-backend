"""Порты внешних платёжных шлюзов billing.

Реальные интеграции (ЮKassa/СБП/T-Bank) подключаются адаптерами; домен и
прикладной слой зависят только от этих протоколов. Раздельные договоры/счета
операционки и приза — на стороне адаптеров (зеркало ledger'а).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CheckoutIntent:
    """Намерение оплаты: куда отправить пользователя и id у провайдера."""

    confirmation_url: str
    provider_subscription_id: str


@dataclass(frozen=True, slots=True)
class PayoutInstruction:
    """Принятая провайдером инструкция на выплату физлицу."""

    provider: str
    provider_payout_id: str


@dataclass(frozen=True, slots=True)
class RefundResult:
    """Результат возврата/отмены платежа у провайдера (операционка)."""

    provider_payment_id: str
    status: str


@runtime_checkable
class SubscriptionCheckoutGateway(Protocol):
    """Создание рекуррентной оплаты подписки у провайдера (операционка)."""

    async def create_checkout(
        self,
        *,
        subscription_id: uuid.UUID,
        amount_kopecks: int,
        description: str,
    ) -> CheckoutIntent:
        """Создать платёжную сессию и вернуть URL подтверждения."""
        ...


@runtime_checkable
class PayoutGateway(Protocol):
    """Отправка выплаты физлицу у провайдера (призовая касса)."""

    async def send_payout(
        self,
        *,
        payout_id: uuid.UUID,
        user_id: uuid.UUID,
        amount_kopecks: int,
    ) -> PayoutInstruction:
        """Инициировать выплату; вернуть идентификатор у провайдера."""
        ...


@runtime_checkable
class PaymentRefundGateway(Protocol):
    """Возврат/отмена подтверждённого платежа у провайдера (операционка)."""

    async def cancel_payment(
        self,
        *,
        provider_payment_id: str,
        amount_kopecks: int,
        receipt: dict[str, object] | None,
    ) -> RefundResult:
        """Инициировать возврат; вернуть статус у провайдера."""
        ...


@runtime_checkable
class SeasonDirectory(Protocol):
    """Резолв сезона по публичному ``slug`` (исходящая зависимость к seasons).

    Прозрачность фонда по сезону требует перевести slug → id; billing не тянет
    внутренние типы seasons, ему достаточно идентификатора.
    """

    async def resolve_slug(self, slug: str) -> uuid.UUID | None:
        """``id`` сезона по slug или ``None``, если такого сезона нет."""
        ...
