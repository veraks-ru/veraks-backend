"""Порты-репозитории billing (Protocol-интерфейсы хранилищ).

Прикладной слой зависит от этих абстракций, а не от SQLAlchemy. Журнал
проводок (``ledger_*``) — append-only: у репозитория нет update/delete для
транзакций и ног, только ``add``.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.modules.billing.domain.entities import (
    Payment,
    PrizeFund,
    Payout,
    Subscription,
)
from app.modules.billing.domain.ledger import LedgerAccount, LedgerTransaction


@runtime_checkable
class LedgerRepository(Protocol):
    """План счетов и append-only журнал двойной записи."""

    async def get_account_by_code(self, account_code: str) -> LedgerAccount | None:
        """Счёт по коду или ``None``."""
        ...

    async def add_account(self, account: LedgerAccount) -> LedgerAccount:
        """Создать счёт плана счетов."""
        ...

    async def add_transaction(
        self, transaction: LedgerTransaction
    ) -> LedgerTransaction:
        """Записать транзакцию вместе с её ногами (append-only)."""
        ...

    async def balance(self, account_id: uuid.UUID) -> int:
        """Сальдо счёта в копейках как ``сумма(debit) − сумма(credit)``."""
        ...


@runtime_checkable
class SubscriptionRepository(Protocol):
    """Хранилище подписок."""

    async def add(self, subscription: Subscription) -> Subscription:
        """Создать подписку."""
        ...

    async def get_by_id(self, subscription_id: uuid.UUID) -> Subscription | None:
        """Подписка по идентификатору или ``None``."""
        ...

    async def get_latest_by_user(self, user_id: uuid.UUID) -> Subscription | None:
        """Последняя (по ``created_at``) подписка пользователя или ``None``."""
        ...

    async def update(self, subscription: Subscription) -> Subscription:
        """Синхронизировать изменяемые поля (статус, период, отмена)."""
        ...


@runtime_checkable
class PaymentRepository(Protocol):
    """Хранилище платежей (приём средств, операционная касса)."""

    async def get_by_provider_ref(
        self, *, provider: str, provider_payment_id: str
    ) -> Payment | None:
        """Платёж по ключу идемпотентности вебхука или ``None``."""
        ...

    async def add(self, payment: Payment) -> Payment:
        """Зафиксировать платёж."""
        ...


@runtime_checkable
class PrizeFundRepository(Protocol):
    """Хранилище призовых фондов."""

    async def add(self, fund: PrizeFund) -> PrizeFund:
        """Завести фонд."""
        ...

    async def get_by_id(self, fund_id: uuid.UUID) -> PrizeFund | None:
        """Фонд по идентификатору или ``None``."""
        ...

    async def list_by_season(self, season_id: uuid.UUID) -> list[PrizeFund]:
        """Фонды сезона (для публичной прозрачности по сезону)."""
        ...

    async def update(self, fund: PrizeFund) -> PrizeFund:
        """Синхронизировать изменяемые поля (deposited, статус)."""
        ...


@runtime_checkable
class PayoutRepository(Protocol):
    """Хранилище выплат победителям (призовая касса, maker-checker)."""

    async def add(self, payout: Payout) -> Payout:
        """Создать выплату (статус ``pending``)."""
        ...

    async def get_by_id(self, payout_id: uuid.UUID) -> Payout | None:
        """Выплата по идентификатору или ``None``."""
        ...

    async def get_by_provider_ref(
        self, *, provider: str, provider_payout_id: str
    ) -> Payout | None:
        """Выплата по ключу идемпотентности вебхука или ``None``."""
        ...

    async def list(
        self, *, season_id: uuid.UUID | None = None
    ) -> list[Payout]:
        """Выплаты (опц. фильтр по сезону), новые сверху. Для админ-обзора."""
        ...

    async def update(self, payout: Payout) -> Payout:
        """Синхронизировать изменяемые поля (статус, approver, provider, paid_at)."""
        ...
