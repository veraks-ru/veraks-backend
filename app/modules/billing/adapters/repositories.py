"""Реализации портов-репозиториев billing поверх async SQLAlchemy.

Транзакцией управляет зависимость сессии (``app/db/session.py``): репозитории
делают ``flush``, а не ``commit``. Журнал проводок только дополняется
(``add_*``) — update/delete для него отсутствуют по дизайну (append-only).
"""

from __future__ import annotations

import uuid

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.billing.adapters.orm import (
    LedgerAccountORM,
    LedgerEntryORM,
    LedgerTransactionORM,
    PaymentORM,
    PayoutORM,
    PrizeFundORM,
    SubscriptionORM,
)
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
    LedgerType,
)


class _RowVanishedError(RuntimeError):
    """Строка исчезла между чтением и записью (не должно случаться)."""


class SqlAlchemyLedgerRepository:
    """План счетов и append-only журнал двойной записи."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_account_by_code(self, account_code: str) -> LedgerAccount | None:
        """Счёт по коду или ``None``."""
        stmt = select(LedgerAccountORM).where(
            LedgerAccountORM.account_code == account_code
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def add_account(self, account: LedgerAccount) -> LedgerAccount:
        """Создать счёт плана счетов."""
        orm = LedgerAccountORM.from_domain(account)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def add_transaction(
        self, transaction: LedgerTransaction
    ) -> LedgerTransaction:
        """Записать транзакцию и её ноги одной вставкой (append-only).

        Доменная транзакция уже прошла проверку баланса и раздельности касс;
        схемные триггеры (миграция ``0010``) дублируют это на уровне БД.
        """
        self._session.add(LedgerTransactionORM.from_domain(transaction))
        # Транзакция должна существовать в БД ДО вставки ног: под asyncpg
        # unit-of-work иначе батчит ноги раньше транзакции и ловит нарушение
        # FK ledger_entries.transaction_id → ledger_transactions.id.
        await self._session.flush()
        for entry in transaction.entries:
            self._session.add(
                LedgerEntryORM.from_domain(
                    entry,
                    transaction_id=transaction.id,
                    created_at=transaction.created_at,
                )
            )
        await self._session.flush()
        return transaction

    async def balance(self, account_id: uuid.UUID) -> int:
        """Сальдо счёта в копейках как ``сумма(debit) − сумма(credit)``."""
        signed = case(
            (LedgerEntryORM.direction == EntryDirection.DEBIT, LedgerEntryORM.amount_kopecks),
            else_=-LedgerEntryORM.amount_kopecks,
        )
        stmt = select(func.coalesce(func.sum(signed), 0)).where(
            LedgerEntryORM.account_id == account_id
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def totals_by_type(self, ledger_type: LedgerType) -> tuple[int, int]:
        """Суммы дебетов и кредитов всех ног кассы (join ноги → транзакции)."""
        debit = func.coalesce(
            func.sum(
                case(
                    (
                        LedgerEntryORM.direction == EntryDirection.DEBIT,
                        LedgerEntryORM.amount_kopecks,
                    ),
                    else_=0,
                )
            ),
            0,
        )
        credit = func.coalesce(
            func.sum(
                case(
                    (
                        LedgerEntryORM.direction == EntryDirection.CREDIT,
                        LedgerEntryORM.amount_kopecks,
                    ),
                    else_=0,
                )
            ),
            0,
        )
        stmt = (
            select(debit, credit)
            .join(
                LedgerTransactionORM,
                LedgerTransactionORM.id == LedgerEntryORM.transaction_id,
            )
            .where(LedgerTransactionORM.ledger_type == ledger_type)
        )
        row = (await self._session.execute(stmt)).one()
        return int(row[0]), int(row[1])


class SqlAlchemySubscriptionRepository:
    """Хранилище подписок."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, subscription: Subscription) -> Subscription:
        orm = SubscriptionORM.from_domain(subscription)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def get_by_id(self, subscription_id: uuid.UUID) -> Subscription | None:
        orm = await self._session.get(SubscriptionORM, subscription_id)
        return orm.to_domain() if orm else None

    async def get_latest_by_user(self, user_id: uuid.UUID) -> Subscription | None:
        stmt = (
            select(SubscriptionORM)
            .where(SubscriptionORM.user_id == user_id)
            .order_by(SubscriptionORM.created_at.desc())
            .limit(1)
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def update(self, subscription: Subscription) -> Subscription:
        orm = await self._session.get(SubscriptionORM, subscription.id)
        if orm is None:  # pragma: no cover — вызывается только для существующих
            raise _RowVanishedError(str(subscription.id))
        orm.status = subscription.status
        orm.provider_subscription_id = subscription.provider_subscription_id
        orm.current_period_start = subscription.current_period_start
        orm.current_period_end = subscription.current_period_end
        orm.canceled_at = subscription.canceled_at
        await self._session.flush()
        return orm.to_domain()


class SqlAlchemyPaymentRepository:
    """Хранилище платежей (приём средств, операционная касса)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_provider_ref(
        self, *, provider: str, provider_payment_id: str
    ) -> Payment | None:
        stmt = select(PaymentORM).where(
            PaymentORM.provider == provider,
            PaymentORM.provider_payment_id == provider_payment_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def add(self, payment: Payment) -> Payment:
        orm = PaymentORM.from_domain(payment)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()


class SqlAlchemyPrizeFundRepository:
    """Хранилище призовых фондов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, fund: PrizeFund) -> PrizeFund:
        orm = PrizeFundORM.from_domain(fund)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def get_by_id(self, fund_id: uuid.UUID) -> PrizeFund | None:
        orm = await self._session.get(PrizeFundORM, fund_id)
        return orm.to_domain() if orm else None

    async def list_by_season(self, season_id: uuid.UUID) -> list[PrizeFund]:
        stmt = (
            select(PrizeFundORM)
            .where(PrizeFundORM.season_id == season_id)
            .order_by(PrizeFundORM.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [orm.to_domain() for orm in rows]

    async def update(self, fund: PrizeFund) -> PrizeFund:
        orm = await self._session.get(PrizeFundORM, fund.id)
        if orm is None:  # pragma: no cover
            raise _RowVanishedError(str(fund.id))
        orm.deposited_kopecks = fund.deposited_kopecks
        orm.status = fund.status
        await self._session.flush()
        return orm.to_domain()


class SqlAlchemyPayoutRepository:
    """Хранилище выплат победителям (призовая касса, maker-checker)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, payout: Payout) -> Payout:
        orm = PayoutORM.from_domain(payout)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def get_by_id(self, payout_id: uuid.UUID) -> Payout | None:
        orm = await self._session.get(PayoutORM, payout_id)
        return orm.to_domain() if orm else None

    async def get_by_provider_ref(
        self, *, provider: str, provider_payout_id: str
    ) -> Payout | None:
        stmt = select(PayoutORM).where(
            PayoutORM.provider == provider,
            PayoutORM.provider_payout_id == provider_payout_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def list_all(self, *, season_id: uuid.UUID | None = None) -> list[Payout]:
        stmt = select(PayoutORM).order_by(PayoutORM.created_at.desc())
        if season_id is not None:
            stmt = stmt.where(PayoutORM.season_id == season_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [orm.to_domain() for orm in rows]

    async def list_by_user(self, user_id: uuid.UUID) -> list[Payout]:
        stmt = (
            select(PayoutORM)
            .where(PayoutORM.user_id == user_id)
            .order_by(PayoutORM.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [orm.to_domain() for orm in rows]

    async def update(self, payout: Payout) -> Payout:
        orm = await self._session.get(PayoutORM, payout.id)
        if orm is None:  # pragma: no cover
            raise _RowVanishedError(str(payout.id))
        orm.status = payout.status
        orm.approved_by = payout.approved_by
        orm.provider = payout.provider
        orm.provider_payout_id = payout.provider_payout_id
        orm.ledger_transaction_id = payout.ledger_transaction_id
        orm.paid_at = payout.paid_at
        await self._session.flush()
        return orm.to_domain()
