"""In-memory фейки портов billing для юнит- и интеграционных тестов.

Фейки повторяют контракт Protocol-портов, но без БД. Доменные инварианты
(баланс, раздельность касс) проверяются самим доменом при сборке проводки,
поэтому фейк журнала просто хранит и считает сальдо.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.modules.billing.domain.entities import (
    Payment,
    Payout,
    PrizeFund,
    Subscription,
)
from app.modules.billing.domain.ledger import (
    EntryDirection,
    LedgerAccount,
    LedgerTransaction,
)
from app.modules.billing.ports.gateways import CheckoutIntent, PayoutInstruction
from app.shared.audit.domain.entities import AuditActorType, AuditEntry


class FakeClock:
    """Часы с фиксированным временем."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class InMemoryLedgerRepository:
    """План счетов и журнал проводок в памяти."""

    def __init__(self) -> None:
        self.accounts: dict[uuid.UUID, LedgerAccount] = {}
        self.transactions: list[LedgerTransaction] = []

    async def get_account_by_code(self, account_code: str) -> LedgerAccount | None:
        for acc in self.accounts.values():
            if acc.account_code == account_code:
                return acc
        return None

    async def add_account(self, account: LedgerAccount) -> LedgerAccount:
        self.accounts[account.id] = account
        return account

    async def add_transaction(
        self, transaction: LedgerTransaction
    ) -> LedgerTransaction:
        self.transactions.append(transaction)
        return transaction

    async def balance(self, account_id: uuid.UUID) -> int:
        total = 0
        for txn in self.transactions:
            for entry in txn.entries:
                if entry.account_id != account_id:
                    continue
                if entry.direction is EntryDirection.DEBIT:
                    total += entry.amount_kopecks
                else:
                    total -= entry.amount_kopecks
        return total

    def seed_account(self, account: LedgerAccount) -> None:
        """Хелпер: засеять счёт плана счетов."""
        self.accounts[account.id] = account


class InMemorySubscriptionRepository:
    """Подписки в памяти."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Subscription] = {}

    async def add(self, subscription: Subscription) -> Subscription:
        self.items[subscription.id] = subscription
        return subscription

    async def get_by_id(self, subscription_id: uuid.UUID) -> Subscription | None:
        return self.items.get(subscription_id)

    async def get_latest_by_user(self, user_id: uuid.UUID) -> Subscription | None:
        owned = [s for s in self.items.values() if s.user_id == user_id]
        if not owned:
            return None
        return max(owned, key=lambda s: s.created_at)

    async def update(self, subscription: Subscription) -> Subscription:
        self.items[subscription.id] = subscription
        return subscription


class InMemoryPaymentRepository:
    """Платежи в памяти (идемпотентность по provider-ref)."""

    def __init__(self) -> None:
        self.items: list[Payment] = []

    async def get_by_provider_ref(
        self, *, provider: str, provider_payment_id: str
    ) -> Payment | None:
        for p in self.items:
            if (
                p.provider.value == provider
                and p.provider_payment_id == provider_payment_id
            ):
                return p
        return None

    async def add(self, payment: Payment) -> Payment:
        self.items.append(payment)
        return payment


class InMemoryPrizeFundRepository:
    """Призовые фонды в памяти."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, PrizeFund] = {}

    async def add(self, fund: PrizeFund) -> PrizeFund:
        self.items[fund.id] = fund
        return fund

    async def get_by_id(self, fund_id: uuid.UUID) -> PrizeFund | None:
        return self.items.get(fund_id)

    async def update(self, fund: PrizeFund) -> PrizeFund:
        self.items[fund.id] = fund
        return fund


class InMemoryPayoutRepository:
    """Выплаты в памяти."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Payout] = {}

    async def add(self, payout: Payout) -> Payout:
        self.items[payout.id] = payout
        return payout

    async def get_by_id(self, payout_id: uuid.UUID) -> Payout | None:
        return self.items.get(payout_id)

    async def get_by_provider_ref(
        self, *, provider: str, provider_payout_id: str
    ) -> Payout | None:
        for p in self.items.values():
            if (
                p.provider is not None
                and p.provider.value == provider
                and p.provider_payout_id == provider_payout_id
            ):
                return p
        return None

    async def list(self, *, season_id: uuid.UUID | None = None) -> list[Payout]:
        items = list(self.items.values())
        if season_id is not None:
            items = [p for p in items if p.season_id == season_id]
        return sorted(items, key=lambda p: p.created_at, reverse=True)

    async def update(self, payout: Payout) -> Payout:
        self.items[payout.id] = payout
        return payout


class FakeCheckoutGateway:
    """Шлюз оплаты подписок: отдаёт детерминированный intent."""

    async def create_checkout(
        self, *, subscription_id: uuid.UUID, amount_kopecks: int, description: str
    ) -> CheckoutIntent:
        return CheckoutIntent(
            confirmation_url=f"https://pay.example/{subscription_id}",
            provider_subscription_id=f"sub-{subscription_id}",
        )


class FakePayoutGateway:
    """Шлюз выплат физлицам: отдаёт детерминированную инструкцию."""

    async def send_payout(
        self, *, payout_id: uuid.UUID, user_id: uuid.UUID, amount_kopecks: int
    ) -> PayoutInstruction:
        return PayoutInstruction(
            provider="yookassa", provider_payout_id=f"po-{payout_id}"
        )


class FakeAuditTrail:
    """Запоминает записанные действия (без хеш-цепочки)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_type: AuditActorType,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        self.records.append(
            {
                "actor_id": actor_id,
                "actor_type": actor_type,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "after": dict(after) if after is not None else None,
            }
        )
        return AuditEntry(
            occurred_at=datetime.now(),  # noqa: DTZ005 — фейк, время не важно
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            hash="fake",
        )

    def actions(self) -> list[str]:
        """Список зафиксированных action'ов (для ассертов)."""
        return [r["action"] for r in self.records]
