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
    PaymentProvider,
    PaymentStatus,
    Payout,
    PayoutRequisites,
    PayoutStatus,
    PrizeFund,
    Subscription,
)
from app.modules.billing.domain.errors import PaymentGatewayError
from app.modules.billing.domain.ledger import (
    EntryDirection,
    LedgerAccount,
    LedgerTransaction,
    LedgerType,
)
from app.modules.billing.ports.gateways import (
    CheckoutIntent,
    PayoutInstruction,
    PayoutRecipient,
    PayoutStatusView,
    RefundResult,
)
from app.shared.audit.domain.entities import AuditActorType, AuditEntry


class FakeClock:
    """Часы с фиксированным временем."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeNotifier:
    """Нотификатор-заглушка: копит эмиссии, ничего не пишет в БД/сеть."""

    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []

    async def emit(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        title: str,
        body: str = "",
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> None:
        self.emitted.append(
            {"user_id": user_id, "kind": kind, "title": title, "body": body}
        )


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

    async def totals_by_type(self, ledger_type: LedgerType) -> tuple[int, int]:
        debit = credit = 0
        for txn in self.transactions:
            if txn.ledger_type is not ledger_type:
                continue
            for entry in txn.entries:
                if entry.direction is EntryDirection.DEBIT:
                    debit += entry.amount_kopecks
                else:
                    credit += entry.amount_kopecks
        return debit, credit

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

    async def list_by_user(self, user_id: uuid.UUID) -> list[Subscription]:
        owned = [s for s in self.items.values() if s.user_id == user_id]
        return sorted(owned, key=lambda s: s.created_at, reverse=True)

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

    async def get_by_id(self, payment_id: uuid.UUID) -> Payment | None:
        for p in self.items:
            if p.id == payment_id:
                return p
        return None

    async def get_latest_succeeded_by_subscription(
        self, subscription_id: uuid.UUID
    ) -> Payment | None:
        owned = [
            p
            for p in self.items
            if p.subscription_id == subscription_id
            and p.status is PaymentStatus.SUCCEEDED
        ]
        if not owned:
            return None
        return max(owned, key=lambda p: p.created_at)

    async def add(self, payment: Payment) -> Payment:
        self.items.append(payment)
        return payment

    async def update(self, payment: Payment) -> Payment:
        for i, p in enumerate(self.items):
            if p.id == payment.id:
                self.items[i] = payment
                return payment
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

    async def get_for_update(self, fund_id: uuid.UUID) -> PrizeFund | None:
        # In-memory фейк однопоточный — блокировка строки не нужна.
        return self.items.get(fund_id)

    async def list_by_season(self, season_id: uuid.UUID) -> list[PrizeFund]:
        return [f for f in self.items.values() if f.season_id == season_id]

    async def update(self, fund: PrizeFund) -> PrizeFund:
        self.items[fund.id] = fund
        return fund


class FakeSeasonDirectory:
    """Резолв сезона по slug в памяти (slug → id)."""

    def __init__(self, by_slug: dict[str, uuid.UUID] | None = None) -> None:
        self._by_slug = by_slug or {}

    def set(self, slug: str, season_id: uuid.UUID) -> None:
        self._by_slug[slug] = season_id

    async def resolve_slug(self, slug: str) -> uuid.UUID | None:
        return self._by_slug.get(slug)


class InMemoryPayoutRepository:
    """Выплаты в памяти."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Payout] = {}

    async def add(self, payout: Payout) -> Payout:
        self.items[payout.id] = payout
        return payout

    async def get_by_id(self, payout_id: uuid.UUID) -> Payout | None:
        return self.items.get(payout_id)

    async def get_for_update(self, payout_id: uuid.UUID) -> Payout | None:
        # In-memory фейк однопоточный — блокировка строки не нужна.
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

    async def list_all(self, *, season_id: uuid.UUID | None = None) -> list[Payout]:
        items = list(self.items.values())
        if season_id is not None:
            items = [p for p in items if p.season_id == season_id]
        return sorted(items, key=lambda p: p.created_at, reverse=True)

    async def list_by_user(self, user_id: uuid.UUID) -> list[Payout]:
        owned = [p for p in self.items.values() if p.user_id == user_id]
        return sorted(owned, key=lambda p: p.created_at, reverse=True)

    async def list_by_status(
        self,
        status: PayoutStatus,
        *,
        provider: PaymentProvider | None = None,
    ) -> list[Payout]:
        matched = [
            p
            for p in self.items.values()
            if p.status is status and (provider is None or p.provider is provider)
        ]
        return sorted(matched, key=lambda p: p.created_at)

    async def update(self, payout: Payout) -> Payout:
        self.items[payout.id] = payout
        return payout


class InMemoryPayoutRequisiteRepository:
    """Реквизиты выплат в памяти (одна запись на пользователя, без шифрования)."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, PayoutRequisites] = {}

    async def get_by_user(self, user_id: uuid.UUID) -> PayoutRequisites | None:
        return self.items.get(user_id)

    async def upsert(self, requisites: PayoutRequisites) -> PayoutRequisites:
        self.items[requisites.user_id] = requisites
        return requisites


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
    """Шлюз выплат физлицам: копит вызовы, отдаёт детерминированную инструкцию."""

    def __init__(self, provider: str = "yookassa") -> None:
        self.provider = provider
        self.calls: list[dict[str, Any]] = []

    async def send_payout(
        self,
        *,
        payout_id: uuid.UUID,
        user_id: uuid.UUID,
        amount_kopecks: int,
        recipient: PayoutRecipient,
    ) -> PayoutInstruction:
        self.calls.append(
            {
                "payout_id": payout_id,
                "user_id": user_id,
                "amount_kopecks": amount_kopecks,
                "recipient": recipient,
            }
        )
        return PayoutInstruction(
            provider=self.provider, provider_payout_id=f"po-{payout_id}"
        )


class FakePayoutStatusProbe:
    """Опрос статусов выплат: управляемый словарь ответов и «сломанных» ссылок."""

    def __init__(self) -> None:
        self.statuses: dict[str, PayoutStatusView] = {}
        self.errors: set[str] = set()

    async def get_payout_status(
        self, *, provider_payout_id: str
    ) -> PayoutStatusView:
        if provider_payout_id in self.errors:
            raise PaymentGatewayError(f"Jump недоступен для {provider_payout_id}")
        return self.statuses[provider_payout_id]


class FakeRefundGateway:
    """Шлюз возврата: фиксирует вызовы, отдаёт детерминированный результат."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def cancel_payment(
        self,
        *,
        provider_payment_id: str,
        amount_kopecks: int,
        receipt: dict | None,
    ) -> RefundResult:
        self.calls.append(
            {
                "provider_payment_id": provider_payment_id,
                "amount_kopecks": amount_kopecks,
                "receipt": receipt,
            }
        )
        return RefundResult(
            provider_payment_id=provider_payment_id, status="REFUNDED"
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
