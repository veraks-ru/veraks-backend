"""Use-cases billing (по одному классу на операцию).

Каждый use-case оркеструет порты, полученные через конструктор, и не знает о
FastAPI/SQLAlchemy. Любое движение денег идёт строго через журнал двойной
записи; операционка и приз не пересекаются ни в одной транзакции.

Все записи в одном use-case выполняются в одной БД-транзакции (сессия-на-запрос
из ``app/db/session.py``): проводка, обновление зеркал и аудит коммитятся
атомарно либо откатываются целиком.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import timedelta

from app.modules.billing.application.dto import Actor, PrizeFundView
from app.modules.billing.domain import chart
from app.modules.billing.domain.entities import (
    Payment,
    PaymentProvider,
    PaymentPurpose,
    PaymentStatus,
    PrizeFund,
    Payout,
    Subscription,
    SubscriptionPlan,
)
from app.modules.billing.domain.errors import (
    BillingPermissionError,
    InsufficientPrizeFundError,
    LedgerAccountNotFoundError,
    PayoutNotFoundError,
    PrizeFundNotFoundError,
    SubscriptionNotFoundError,
)
from app.modules.billing.domain.ledger import (
    EntryDirection,
    LedgerAccount,
    LedgerTransaction,
    LedgerType,
    PostingLeg,
    TransactionKind,
)
from app.modules.billing.domain.policies import (
    ensure_can_approve_payout,
    ensure_can_create_payout,
    ensure_can_manage_prize_funds,
    ensure_distinct_approver,
)
from app.modules.billing.ports.clock import Clock
from app.modules.billing.ports.gateways import SubscriptionCheckoutGateway
from app.modules.billing.ports.repositories import (
    LedgerRepository,
    PaymentRepository,
    PayoutRepository,
    PrizeFundRepository,
    SubscriptionRepository,
)
from app.modules.identity.domain.entities import UserRole
from app.shared.audit.domain.entities import AuditActorType
from app.shared.audit.ports.audit_trail import AuditTrail

_ACTOR_TYPE_BY_ROLE: dict[UserRole, AuditActorType] = {
    UserRole.USER: AuditActorType.USER,
    UserRole.EDITOR: AuditActorType.EDITOR,
    UserRole.ARBITER: AuditActorType.ARBITER,
    UserRole.ADMIN: AuditActorType.ADMIN,
}

# Длительность оплаченного периода по тарифу.
_PLAN_PERIOD: dict[SubscriptionPlan, timedelta] = {
    SubscriptionPlan.MONTHLY: timedelta(days=30),
    SubscriptionPlan.ANNUAL: timedelta(days=365),
}


def _actor_type(role: UserRole) -> AuditActorType:
    """Маппинг роли identity → тип актора аудита."""
    return _ACTOR_TYPE_BY_ROLE.get(role, AuditActorType.USER)


class _LedgerOps:
    """Утилита резолва счетов и сборки проводок (общая для use-cases)."""

    def __init__(self, ledger: LedgerRepository) -> None:
        self._ledger = ledger

    async def account(self, account_code: str) -> LedgerAccount:
        """Счёт по коду или доменная ошибка."""
        acc = await self._ledger.get_account_by_code(account_code)
        if acc is None:
            raise LedgerAccountNotFoundError(account_code)
        return acc


# ── Подписки (OPERATIONS) ─────────────────────────────────────────────────


class StartSubscription:
    """Оформить подписку: создать запись и платёжную сессию у провайдера."""

    def __init__(
        self,
        *,
        subscriptions: SubscriptionRepository,
        checkout: SubscriptionCheckoutGateway,
        audit: AuditTrail,
        clock: Clock,
        plan_prices: Mapping[SubscriptionPlan, int],
    ) -> None:
        self._subscriptions = subscriptions
        self._checkout = checkout
        self._audit = audit
        self._clock = clock
        self._plan_prices = plan_prices

    async def execute(
        self,
        *,
        user_id: uuid.UUID,
        plan: SubscriptionPlan,
        provider: PaymentProvider = PaymentProvider.YOOKASSA,
    ) -> tuple[Subscription, str]:
        """Создать подписку (incomplete) и вернуть её и URL оплаты."""
        price = self._plan_prices[plan]
        subscription = Subscription(
            user_id=user_id, plan=plan, price_kopecks=price, provider=provider
        )
        saved = await self._subscriptions.add(subscription)

        intent = await self._checkout.create_checkout(
            subscription_id=saved.id,
            amount_kopecks=price,
            description=f"Подписка {plan.value}",
        )
        saved.provider_subscription_id = intent.provider_subscription_id
        saved = await self._subscriptions.update(saved)

        await self._audit.record(
            actor_id=user_id,
            actor_type=AuditActorType.USER,
            action="subscription.started",
            entity_type="subscription",
            entity_id=saved.id,
            after={"plan": plan.value, "price_kopecks": price},
        )
        return saved, intent.confirmation_url


class CancelSubscription:
    """Отменить подписку (владелец или admin)."""

    def __init__(
        self,
        *,
        subscriptions: SubscriptionRepository,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._subscriptions = subscriptions
        self._audit = audit
        self._clock = clock

    async def execute(
        self, *, subscription_id: uuid.UUID, actor: Actor
    ) -> Subscription:
        """Перевести подписку в ``canceled``."""
        subscription = await self._subscriptions.get_by_id(subscription_id)
        if subscription is None:
            raise SubscriptionNotFoundError(str(subscription_id))
        if subscription.user_id != actor.user_id and actor.role is not UserRole.ADMIN:
            raise BillingPermissionError("Отменить можно только свою подписку")

        subscription.cancel(now=self._clock.now())
        saved = await self._subscriptions.update(subscription)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="subscription.canceled",
            entity_type="subscription",
            entity_id=saved.id,
            after={"status": saved.status.value},
        )
        return saved


class RecordSubscriptionPayment:
    """Принять успешный платёж по подписке из вебхука провайдера (OPERATIONS).

    Идемпотентно по ``(provider, provider_payment_id)``: повторный вебхук не
    создаёт второй платёж и вторую проводку. Источник истины движения денег —
    журнал двойной записи; ``payments`` лишь ссылается на проводку.
    """

    def __init__(
        self,
        *,
        payments: PaymentRepository,
        subscriptions: SubscriptionRepository,
        ledger: LedgerRepository,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._payments = payments
        self._subscriptions = subscriptions
        self._ledger = _LedgerOps(ledger)
        self._ledger_repo = ledger
        self._audit = audit
        self._clock = clock

    async def execute(
        self,
        *,
        provider: PaymentProvider,
        provider_payment_id: str,
        amount_kopecks: int,
        subscription_id: uuid.UUID | None = None,
    ) -> Payment:
        """Записать платёж и провести его в операционной кассе."""
        existing = await self._payments.get_by_provider_ref(
            provider=provider.value, provider_payment_id=provider_payment_id
        )
        if existing is not None:
            return existing  # идемпотентность: вебхук повторился

        now = self._clock.now()
        cash = await self._ledger.account(chart.OPS_CASH_YOOKASSA)
        revenue = await self._ledger.account(chart.OPS_REVENUE_SUBSCRIPTIONS)

        transaction = LedgerTransaction.post(
            kind=TransactionKind.SUBSCRIPTION_PAYMENT,
            legs=(
                PostingLeg(cash, EntryDirection.DEBIT, amount_kopecks),
                PostingLeg(revenue, EntryDirection.CREDIT, amount_kopecks),
            ),
            external_ref=provider_payment_id,
            description="Платёж по подписке",
            now=now,
        )
        saved_txn = await self._ledger_repo.add_transaction(transaction)

        user_id: uuid.UUID | None = None
        if subscription_id is not None:
            subscription = await self._subscriptions.get_by_id(subscription_id)
            if subscription is None:
                raise SubscriptionNotFoundError(str(subscription_id))
            user_id = subscription.user_id
            subscription.activate(
                period_start=now,
                period_end=now + _PLAN_PERIOD[subscription.plan],
            )
            await self._subscriptions.update(subscription)

        payment = Payment(
            provider=provider,
            provider_payment_id=provider_payment_id,
            amount_kopecks=amount_kopecks,
            purpose=PaymentPurpose.SUBSCRIPTION,
            status=PaymentStatus.SUCCEEDED,
            user_id=user_id,
            subscription_id=subscription_id,
            ledger_transaction_id=saved_txn.id,
            paid_at=now,
        )
        saved = await self._payments.add(payment)
        await self._audit.record(
            actor_id=None,
            actor_type=AuditActorType.SYSTEM,
            action="subscription.payment.recorded",
            entity_type="payment",
            entity_id=saved.id,
            after={
                "amount_kopecks": amount_kopecks,
                "ledger_transaction_id": str(saved_txn.id),
                "ledger_type": LedgerType.OPERATIONS.value,
            },
            metadata={"provider": provider.value, "external_ref": provider_payment_id},
        )
        return saved


# ── Призовой фонд (PRIZE) ─────────────────────────────────────────────────


class AnnouncePrizeFund:
    """Завести призовой фонд и его счёт в кассе PRIZE (admin)."""

    def __init__(
        self,
        *,
        funds: PrizeFundRepository,
        ledger: LedgerRepository,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._funds = funds
        self._ledger = ledger
        self._audit = audit
        self._clock = clock

    async def execute(
        self,
        *,
        actor: Actor,
        sponsor_name: str,
        committed_kopecks: int,
        season_id: uuid.UUID | None = None,
        sponsor_ref: str = "",
    ) -> PrizeFund:
        """Создать фонд (announced) и привязанный счёт PRIZE."""
        ensure_can_manage_prize_funds(actor.role)

        fund_id = uuid.uuid4()
        account = LedgerAccount(
            ledger_type=LedgerType.PRIZE,
            account_code=chart.prize_fund_account_code(fund_id),
            title=f"Призовой фонд: {sponsor_name}",
        )
        saved_account = await self._ledger.add_account(account)

        fund = PrizeFund(
            id=fund_id,
            sponsor_name=sponsor_name,
            ledger_account_id=saved_account.id,
            committed_kopecks=committed_kopecks,
            season_id=season_id,
            sponsor_ref=sponsor_ref,
        )
        saved = await self._funds.add(fund)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="prize_fund.announced",
            entity_type="prize_fund",
            entity_id=saved.id,
            after={
                "sponsor_name": sponsor_name,
                "committed_kopecks": committed_kopecks,
                "ledger_account_id": str(saved_account.id),
            },
        )
        return saved


class RecordSponsorDeposit:
    """Зарегистрировать поступление спонсора в фонд (проводка PRIZE)."""

    def __init__(
        self,
        *,
        funds: PrizeFundRepository,
        ledger: LedgerRepository,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._funds = funds
        self._ledger = _LedgerOps(ledger)
        self._ledger_repo = ledger
        self._audit = audit
        self._clock = clock

    async def execute(
        self,
        *,
        actor: Actor,
        fund_id: uuid.UUID,
        amount_kopecks: int,
        external_ref: str | None = None,
    ) -> PrizeFund:
        """Провести депозит спонсора и обновить зеркало фонда."""
        ensure_can_manage_prize_funds(actor.role)

        fund = await self._funds.get_by_id(fund_id)
        if fund is None:
            raise PrizeFundNotFoundError(str(fund_id))

        now = self._clock.now()
        sponsor_cash = await self._ledger.account(chart.PRIZE_CASH_SPONSOR)
        fund_account = await self._ledger.account(
            chart.prize_fund_account_code(fund_id)
        )

        transaction = LedgerTransaction.post(
            kind=TransactionKind.SPONSOR_DEPOSIT,
            legs=(
                PostingLeg(sponsor_cash, EntryDirection.DEBIT, amount_kopecks),
                PostingLeg(fund_account, EntryDirection.CREDIT, amount_kopecks),
            ),
            external_ref=external_ref,
            description="Депозит спонсора в призовой фонд",
            now=now,
        )
        saved_txn = await self._ledger_repo.add_transaction(transaction)

        fund.record_deposit(amount_kopecks)
        saved = await self._funds.update(fund)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="prize_fund.deposit.recorded",
            entity_type="prize_fund",
            entity_id=saved.id,
            after={
                "deposited_kopecks": saved.deposited_kopecks,
                "status": saved.status.value,
                "ledger_transaction_id": str(saved_txn.id),
                "ledger_type": LedgerType.PRIZE.value,
            },
        )
        return saved


class GetPrizeFund:
    """Прозрачность: фонд и его фактическое сальдо (публично)."""

    def __init__(self, *, funds: PrizeFundRepository, ledger: LedgerRepository) -> None:
        self._funds = funds
        self._ledger = ledger

    async def execute(self, *, fund_id: uuid.UUID) -> PrizeFundView:
        """Вернуть фонд и баланс его счёта PRIZE."""
        fund = await self._funds.get_by_id(fund_id)
        if fund is None:
            raise PrizeFundNotFoundError(str(fund_id))
        # Счёт фонда — кредит-нормальный (депозит кредитует, выплата дебетует):
        # доступный остаток = кредит − дебет = −(дебет − кредит).
        available = -await self._ledger.balance(fund.ledger_account_id)
        return PrizeFundView(fund=fund, balance_kopecks=available)


class GetMySubscription:
    """Чтение текущей (последней) подписки пользователя."""

    def __init__(self, *, subscriptions: SubscriptionRepository) -> None:
        self._subscriptions = subscriptions

    async def execute(self, *, user_id: uuid.UUID) -> Subscription:
        """Вернуть последнюю подписку пользователя или доменную ошибку 404."""
        subscription = await self._subscriptions.get_latest_by_user(user_id)
        if subscription is None:
            raise SubscriptionNotFoundError("У пользователя нет подписки")
        return subscription


class ListPayouts:
    """Админ-обзор выплат (опц. фильтр по сезону)."""

    def __init__(self, *, payouts: PayoutRepository) -> None:
        self._payouts = payouts

    async def execute(
        self, *, actor: Actor, season_id: uuid.UUID | None = None
    ) -> list[Payout]:
        """Список выплат, новые сверху. Только для admin."""
        ensure_can_manage_prize_funds(actor.role)
        return await self._payouts.list(season_id=season_id)


# ── Выплаты призов (PRIZE, maker-checker) ─────────────────────────────────


class CreatePayout:
    """maker-шаг: инициировать выплату победителю (admin). Без проводки."""

    def __init__(
        self,
        *,
        payouts: PayoutRepository,
        funds: PrizeFundRepository,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._payouts = payouts
        self._funds = funds
        self._audit = audit
        self._clock = clock

    async def execute(
        self,
        *,
        actor: Actor,
        user_id: uuid.UUID,
        prize_fund_id: uuid.UUID,
        amount_kopecks: int,
        tax_withheld_kopecks: int = 0,
        season_id: uuid.UUID | None = None,
    ) -> Payout:
        """Создать выплату в статусе ``pending`` (ждёт подтверждения)."""
        ensure_can_create_payout(actor.role)

        fund = await self._funds.get_by_id(prize_fund_id)
        if fund is None:
            raise PrizeFundNotFoundError(str(prize_fund_id))

        payout = Payout(
            user_id=user_id,
            prize_fund_id=prize_fund_id,
            amount_kopecks=amount_kopecks,
            tax_withheld_kopecks=tax_withheld_kopecks,
            season_id=season_id,
            created_by=actor.user_id,
        )
        saved = await self._payouts.add(payout)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="prize.payout.created",
            entity_type="payout",
            entity_id=saved.id,
            after={
                "user_id": str(user_id),
                "amount_kopecks": amount_kopecks,
                "tax_withheld_kopecks": tax_withheld_kopecks,
                "status": saved.status.value,
            },
        )
        return saved


class ApprovePayout:
    """checker-шаг: подтвердить выплату (другой admin) и провести её в PRIZE.

    maker-checker: подтверждающий обязан отличаться от инициатора. Проводка
    списывает брутто с фонда и разносит нетто (к получению) и НДФЛ.
    """

    def __init__(
        self,
        *,
        payouts: PayoutRepository,
        funds: PrizeFundRepository,
        ledger: LedgerRepository,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._payouts = payouts
        self._funds = funds
        self._ledger = _LedgerOps(ledger)
        self._ledger_repo = ledger
        self._audit = audit
        self._clock = clock

    async def execute(self, *, actor: Actor, payout_id: uuid.UUID) -> Payout:
        """Подтвердить выплату и записать проводку призовой кассы."""
        ensure_can_approve_payout(actor.role)

        payout = await self._payouts.get_by_id(payout_id)
        if payout is None:
            raise PayoutNotFoundError(str(payout_id))
        ensure_distinct_approver(
            created_by=payout.created_by, approver_id=actor.user_id
        )

        fund = await self._funds.get_by_id(payout.prize_fund_id)
        if fund is None:
            raise PrizeFundNotFoundError(str(payout.prize_fund_id))

        # Счёт фонда кредит-нормальный: доступно = кредит − дебет = −balance.
        available = -await self._ledger_repo.balance(fund.ledger_account_id)
        if available < payout.gross_kopecks:
            raise InsufficientPrizeFundError(
                f"В фонде {available} коп., требуется {payout.gross_kopecks} коп."
            )

        now = self._clock.now()
        fund_account = await self._ledger.account(
            chart.prize_fund_account_code(fund.id)
        )
        payable = await self._ledger.account(chart.PRIZE_PAYABLE_WINNERS)

        legs = [
            PostingLeg(fund_account, EntryDirection.DEBIT, payout.gross_kopecks),
            PostingLeg(payable, EntryDirection.CREDIT, payout.amount_kopecks),
        ]
        if payout.tax_withheld_kopecks > 0:
            tax_account = await self._ledger.account(chart.PRIZE_TAX_WITHHELD)
            legs.append(
                PostingLeg(
                    tax_account, EntryDirection.CREDIT, payout.tax_withheld_kopecks
                )
            )

        transaction = LedgerTransaction.post(
            kind=TransactionKind.PRIZE_PAYOUT,
            legs=tuple(legs),
            external_ref=str(payout.id),
            description="Выплата приза победителю",
            now=now,
        )
        saved_txn = await self._ledger_repo.add_transaction(transaction)

        payout.approve(approver_id=actor.user_id)
        payout.ledger_transaction_id = saved_txn.id
        saved = await self._payouts.update(payout)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="prize.payout.approved",
            entity_type="payout",
            entity_id=saved.id,
            before={"created_by": str(payout.created_by)},
            after={
                "status": saved.status.value,
                "approved_by": str(actor.user_id),
                "ledger_transaction_id": str(saved_txn.id),
                "ledger_type": LedgerType.PRIZE.value,
            },
        )
        return saved
