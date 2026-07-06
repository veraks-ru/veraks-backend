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
from dataclasses import dataclass as _sponsor_dataclass
from collections.abc import Mapping
from datetime import timedelta

from app.modules.billing.application.dto import (
    Actor,
    LedgerReconciliation,
    PrizeFundView,
    SeasonPrizeFundView,
)
from app.modules.billing.domain import chart
from app.modules.billing.domain.entities import (
    Payment,
    PaymentProvider,
    PaymentPurpose,
    PaymentStatus,
    PrizeFund,
    Payout,
    PayoutStatus,
    Subscription,
    SubscriptionPlan,
)
from app.modules.billing.domain.errors import (
    BillingPermissionError,
    InsufficientPrizeFundError,
    InvalidAmountError,
    LedgerAccountNotFoundError,
    PayoutAlreadyDecidedError,
    PayoutNotFoundError,
    PrizeFundNotFoundError,
    SeasonNotFoundError,
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
from app.modules.billing.ports.notifications import Notifier
from app.modules.billing.domain.policies import (
    ensure_can_announce_fund,
    ensure_can_approve_payout,
    ensure_can_create_payout,
    ensure_can_deposit_to_fund,
    ensure_can_manage_prize_funds,
    ensure_distinct_approver,
)
from app.modules.billing.ports.clock import Clock
from app.modules.billing.ports.gateways import (
    PayoutGateway,
    SeasonDirectory,
    SubscriptionCheckoutGateway,
)
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
    SubscriptionPlan.DAILY: timedelta(days=1),
    SubscriptionPlan.WEEKLY: timedelta(days=7),
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
        instant_activate: bool = False,
    ) -> None:
        self._subscriptions = subscriptions
        self._checkout = checkout
        self._audit = audit
        self._clock = clock
        self._plan_prices = plan_prices
        # Локальный режим без реального провайдера: активируем сразу (дизайн —
        # в проде активация приходит вебхуком об оплате).
        self._instant_activate = instant_activate

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
        if self._instant_activate:
            now = self._clock.now()
            saved.activate(period_start=now, period_end=now + _PLAN_PERIOD[plan])
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

        # Сумму вебхука сверяем с ценой подписки ДО проводки: провайдер не может
        # активировать подписку платежом на произвольную (заниженную) сумму.
        user_id: uuid.UUID | None = None
        subscription = None
        if subscription_id is not None:
            subscription = await self._subscriptions.get_by_id(subscription_id)
            if subscription is None:
                raise SubscriptionNotFoundError(str(subscription_id))
            if amount_kopecks != subscription.price_kopecks:
                raise InvalidAmountError(
                    f"Сумма платежа {amount_kopecks} коп. не совпадает с ценой "
                    f"подписки {subscription.price_kopecks} коп."
                )
            user_id = subscription.user_id

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

        if subscription is not None:
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
        sponsor_user_id: uuid.UUID | None = None,
    ) -> PrizeFund:
        """Создать фонд (announced) и привязанный счёт PRIZE."""
        ensure_can_announce_fund(
            role=actor.role,
            actor_user_id=actor.user_id,
            sponsor_user_id=sponsor_user_id,
        )

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
            sponsor_user_id=sponsor_user_id,
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
    ) -> PrizeFundView:
        """Провести депозит спонсора и вернуть фонд с доступным остатком."""
        fund = await self._funds.get_by_id(fund_id)
        if fund is None:
            raise PrizeFundNotFoundError(str(fund_id))
        ensure_can_deposit_to_fund(
            role=actor.role,
            actor_user_id=actor.user_id,
            fund_sponsor_user_id=fund.sponsor_user_id,
        )

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
        # Доступный остаток (депозиты − выплаты) = −сальдо кредит-нормального счёта.
        available = -await self._ledger_repo.balance(saved.ledger_account_id)
        return PrizeFundView(fund=saved, balance_kopecks=available)


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


class GetSeasonPrizeFund:
    """Прозрачность по сезону: фонды сезона (с сальдо) + история выплат (публично)."""

    def __init__(
        self,
        *,
        seasons: SeasonDirectory,
        funds: PrizeFundRepository,
        payouts: PayoutRepository,
        ledger: LedgerRepository,
    ) -> None:
        self._seasons = seasons
        self._funds = funds
        self._payouts = payouts
        self._ledger = ledger

    async def execute(self, *, slug: str) -> SeasonPrizeFundView:
        """Резолвит сезон по slug, собирает его фонды и выплаты.

        Неизвестный сезон → :class:`SeasonNotFoundError` (маппится в 404).
        """
        season_id = await self._seasons.resolve_slug(slug)
        if season_id is None:
            raise SeasonNotFoundError(f"Сезон '{slug}' не найден")
        funds = await self._funds.list_by_season(season_id)
        views = [
            PrizeFundView(
                fund=fund,
                balance_kopecks=-await self._ledger.balance(fund.ledger_account_id),
            )
            for fund in funds
        ]
        payouts = await self._payouts.list_all(season_id=season_id)
        return SeasonPrizeFundView(season_slug=slug, funds=views, payouts=payouts)


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
        return await self._payouts.list_all(season_id=season_id)


class ListMyPayouts:
    """Свои выплаты (для личного профиля)."""

    def __init__(self, *, payouts: PayoutRepository) -> None:
        self._payouts = payouts

    async def execute(self, *, user_id: uuid.UUID) -> list[Payout]:
        """Выплаты текущего пользователя, новые сверху."""
        return await self._payouts.list_by_user(user_id)


class ReconcileLedger:
    """Сверка целостности журнала: баланс книг по каждой кассе.

    Двойная запись гарантирует ``debit == credit`` в каждой транзакции (триггер),
    значит и по кассе целиком суммы обязаны сходиться. Расхождение — признак
    повреждения данных в обход триггеров; воркер ``reconcile`` поднимает тревогу.

    TODO(billing-infra): к внутренней сверке добавить сверку с внешними
    источниками — операционный кэш ↔ сеттлменты провайдера, призовой кэш ↔
    депозиты спонсора и выплаты (нужны данные провайдера/банка).
    """

    def __init__(self, *, ledger: LedgerRepository) -> None:
        self._ledger = ledger

    async def execute(self) -> list[LedgerReconciliation]:
        """Возвращает по строке на кассу с суммами дебетов/кредитов."""
        reports: list[LedgerReconciliation] = []
        for ledger_type in LedgerType:
            debit, credit = await self._ledger.totals_by_type(ledger_type)
            reports.append(
                LedgerReconciliation(
                    ledger_type=ledger_type,
                    total_debit_kopecks=debit,
                    total_credit_kopecks=credit,
                )
            )
        return reports


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
        notifier: Notifier | None = None,
    ) -> None:
        self._payouts = payouts
        self._funds = funds
        self._ledger = _LedgerOps(ledger)
        self._ledger_repo = ledger
        self._audit = audit
        self._clock = clock
        self._notifier = notifier

    async def execute(self, *, actor: Actor, payout_id: uuid.UUID) -> Payout:
        """Подтвердить выплату и записать проводку призовой кассы.

        Порядок операций критичен для безопасности денег:
        1) строка выплаты берётся ``FOR UPDATE`` — конкурентный второй approve
           блокируется до нашего коммита;
        2) ``approve()`` вызывается ДО проводки — если выплата уже не ``pending``
           (второй approve увидит ``approved``), поднимается ошибка и деньги не
           двигаются;
        3) строка фонда берётся ``FOR UPDATE`` до чтения остатка — две выплаты из
           одного фонда не могут «увидеть» один и тот же остаток и увести фонд в минус.
        """
        ensure_can_approve_payout(actor.role)

        payout = await self._payouts.get_for_update(payout_id)
        if payout is None:
            raise PayoutNotFoundError(str(payout_id))
        ensure_distinct_approver(
            created_by=payout.created_by, approver_id=actor.user_id
        )
        # Проверка «можно ли подтвердить» — ДО любого движения денег.
        payout.approve(approver_id=actor.user_id)

        fund = await self._funds.get_for_update(payout.prize_fund_id)
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
        if self._notifier is not None:
            await self._notifier.emit(
                user_id=saved.user_id,
                kind="payout.approved",
                title="Выплата подтверждена",
                body=f"Приз {saved.amount_kopecks / 100:.0f} ₽ подтверждён к выплате",
                entity_type="payout",
                entity_id=saved.id,
            )
        return saved


class DispatchPayout:
    """Отправка подтверждённой выплаты провайдеру (``approved → processing``).

    Вызывает шлюз выплат физлицам и фиксирует ``provider``/``provider_payout_id``
    для последующего сопоставления с вебхуком. Проводка кассы PRIZE уже сделана
    на шаге подтверждения — здесь только внешняя отправка и смена статуса.
    """

    def __init__(
        self,
        *,
        payouts: PayoutRepository,
        gateway: PayoutGateway,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._payouts = payouts
        self._gateway = gateway
        self._audit = audit
        self._clock = clock

    async def execute(self, *, actor: Actor, payout_id: uuid.UUID) -> Payout:
        """Отправляет выплату провайдеру и переводит её в ``processing``."""
        ensure_can_approve_payout(actor.role)
        payout = await self._payouts.get_for_update(payout_id)
        if payout is None:
            raise PayoutNotFoundError(str(payout_id))
        # Статус проверяем ДО внешнего вызова: нельзя отправить не-approved
        # выплату (обход maker-checker), а FOR UPDATE не даёт двум dispatch
        # отправить одну выплату провайдеру дважды.
        if payout.status is not PayoutStatus.APPROVED:
            raise PayoutAlreadyDecidedError(
                f"Выплата в статусе {payout.status.value}, ожидался approved"
            )

        # payout.id — идемпотентный ключ на стороне провайдера: повтор отправки
        # той же выплаты не создаёт второй перевод.
        instruction = await self._gateway.send_payout(
            payout_id=payout.id,
            user_id=payout.user_id,
            amount_kopecks=payout.amount_kopecks,
        )
        payout.mark_processing(
            provider=PaymentProvider(instruction.provider),
            provider_payout_id=instruction.provider_payout_id,
        )
        saved = await self._payouts.update(payout)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="prize.payout.dispatched",
            entity_type="payout",
            entity_id=saved.id,
            after={
                "status": saved.status.value,
                "provider": instruction.provider,
                "provider_payout_id": instruction.provider_payout_id,
            },
        )
        return saved


class RecordPayoutResult:
    """Приём результата выплаты из вебхука провайдера (``processing → paid/failed``).

    Идемпотентно: повторный вебхук по уже терминальной выплате (paid/failed) —
    no-op (возвращает текущее состояние). Сопоставление — по
    ``(provider, provider_payout_id)``, проставленным на шаге отправки.
    """

    def __init__(
        self,
        *,
        payouts: PayoutRepository,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._payouts = payouts
        self._audit = audit
        self._clock = clock

    async def execute(
        self,
        *,
        provider: PaymentProvider,
        provider_payout_id: str,
        succeeded: bool,
    ) -> Payout:
        """Фиксирует исход выплаты у провайдера (paid/failed), идемпотентно."""
        payout = await self._payouts.get_by_provider_ref(
            provider=provider.value, provider_payout_id=provider_payout_id
        )
        if payout is None:
            raise PayoutNotFoundError(
                f"Выплата {provider.value}:{provider_payout_id} не найдена"
            )
        if payout.status in (PayoutStatus.PAID, PayoutStatus.FAILED):
            return payout  # терминальное состояние — повторный вебхук игнорируем

        if succeeded:
            payout.mark_paid(now=self._clock.now())
        else:
            payout.mark_failed()
        saved = await self._payouts.update(payout)
        await self._audit.record(
            actor_id=None,
            actor_type=AuditActorType.SYSTEM,
            action="prize.payout.paid" if succeeded else "prize.payout.failed",
            entity_type="payout",
            entity_id=saved.id,
            after={"status": saved.status.value},
            metadata={"provider": provider.value, "provider_payout_id": provider_payout_id},
        )
        return saved


# ── Кабинет спонсора (read) ──────────────────────────────────────────────────


@_sponsor_dataclass(frozen=True, slots=True)
class SponsorFundView:
    """Фонд спонсора + доступный остаток (депозиты минус выплаты)."""

    fund: PrizeFund
    available_kopecks: int


@_sponsor_dataclass(frozen=True, slots=True)
class SponsorFundDetail:
    fund: PrizeFund
    available_kopecks: int
    payouts: list[Payout]


class ListMySponsorFunds:
    """Фонды пользователя-спонсора с доступным остатком (его кабинет)."""

    def __init__(
        self, *, funds: PrizeFundRepository, ledger: LedgerRepository
    ) -> None:
        self._funds = funds
        self._ledger = ledger

    async def execute(
        self, *, sponsor_user_id: uuid.UUID
    ) -> list[SponsorFundView]:
        out: list[SponsorFundView] = []
        for fund in await self._funds.list_for_sponsor(sponsor_user_id):
            # balance = debit − credit; фонд держит кредит → доступно = −balance.
            balance = await self._ledger.balance(fund.ledger_account_id)
            out.append(SponsorFundView(fund=fund, available_kopecks=-balance))
        return out


class GetMySponsorFund:
    """Детали фонда спонсора: остаток + выплаты (только владельцу)."""

    def __init__(
        self,
        *,
        funds: PrizeFundRepository,
        ledger: LedgerRepository,
        payouts: PayoutRepository,
    ) -> None:
        self._funds = funds
        self._ledger = ledger
        self._payouts = payouts

    async def execute(
        self, *, fund_id: uuid.UUID, sponsor_user_id: uuid.UUID
    ) -> SponsorFundDetail:
        fund = await self._funds.get_by_id(fund_id)
        # Чужие фонды не раскрываем (404, а не 403 — не палим существование).
        if fund is None or fund.sponsor_user_id != sponsor_user_id:
            raise PrizeFundNotFoundError(str(fund_id))
        balance = await self._ledger.balance(fund.ledger_account_id)
        payouts = await self._payouts.list_by_fund(fund_id)
        return SponsorFundDetail(
            fund=fund, available_kopecks=-balance, payouts=payouts
        )
