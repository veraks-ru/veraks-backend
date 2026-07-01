"""Composition root модуля billing (FastAPI DI).

Единственное место, где порты связываются с конкретными адаптерами и
собираются use-cases. В тестах достаточно переопределить провайдеры портов.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SettingsDep
from app.db.session import get_session
from app.modules.billing.adapters.clock import SystemClock
from app.modules.billing.adapters.gateways import (
    LocalSubscriptionCheckoutGateway,
    YookassaPayoutGateway,
    YookassaSubscriptionCheckoutGateway,
)
from app.modules.billing.adapters.repositories import (
    SqlAlchemyLedgerRepository,
    SqlAlchemyPaymentRepository,
    SqlAlchemyPayoutRepository,
    SqlAlchemyPrizeFundRepository,
    SqlAlchemySubscriptionRepository,
)
from app.modules.billing.adapters.season_directory import SqlAlchemySeasonDirectory
from app.modules.billing.application.dto import Actor
from app.modules.billing.application.use_cases import (
    AnnouncePrizeFund,
    ApprovePayout,
    CancelSubscription,
    CreatePayout,
    DispatchPayout,
    GetMySponsorFund,
    GetMySubscription,
    GetPrizeFund,
    GetSeasonPrizeFund,
    ListMyPayouts,
    ListMySponsorFunds,
    ListPayouts,
    RecordPayoutResult,
    RecordSponsorDeposit,
    RecordSubscriptionPayment,
    StartSubscription,
)
from app.modules.billing.domain.entities import SubscriptionPlan
from app.modules.billing.domain.webhooks import verify_signature
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
from app.modules.identity.api.dependencies import CurrentUser
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from app.shared.audit.ports.audit_trail import AuditTrail

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Порты → адаптеры ──────────────────────────────────────────────────────


def get_clock() -> Clock:
    """Серверные часы (в тестах подменяются фиксированным временем)."""
    return SystemClock()


def get_ledger_repository(session: SessionDep) -> LedgerRepository:
    """Репозиторий журнала проводок."""
    return SqlAlchemyLedgerRepository(session)


def get_subscription_repository(session: SessionDep) -> SubscriptionRepository:
    """Репозиторий подписок."""
    return SqlAlchemySubscriptionRepository(session)


def get_payment_repository(session: SessionDep) -> PaymentRepository:
    """Репозиторий платежей."""
    return SqlAlchemyPaymentRepository(session)


def get_prize_fund_repository(session: SessionDep) -> PrizeFundRepository:
    """Репозиторий призовых фондов."""
    return SqlAlchemyPrizeFundRepository(session)


def get_payout_repository(session: SessionDep) -> PayoutRepository:
    """Репозиторий выплат."""
    return SqlAlchemyPayoutRepository(session)


def get_checkout_gateway(settings: SettingsDep) -> SubscriptionCheckoutGateway:
    """Шлюз оплаты подписок. Локально — заглушка с мгновенной активацией."""
    if settings.app_env == "local":
        return LocalSubscriptionCheckoutGateway()
    return YookassaSubscriptionCheckoutGateway()


def get_payout_gateway() -> PayoutGateway:
    """Шлюз выплат физлицам (TODO(billing-infra): реальная интеграция)."""
    return YookassaPayoutGateway()


def get_season_directory(session: SessionDep) -> SeasonDirectory:
    """Резолв сезона по slug (чтение таблицы seasons в монолите)."""
    return SqlAlchemySeasonDirectory(session)


def get_audit_trail(session: SessionDep) -> AuditTrail:
    """Неизменяемый аудит-журнал (общая инфраструктура)."""
    return SqlAlchemyAuditTrail(session)


ClockDep = Annotated[Clock, Depends(get_clock)]
LedgerRepoDep = Annotated[LedgerRepository, Depends(get_ledger_repository)]
SubscriptionRepoDep = Annotated[
    SubscriptionRepository, Depends(get_subscription_repository)
]
PaymentRepoDep = Annotated[PaymentRepository, Depends(get_payment_repository)]
PrizeFundRepoDep = Annotated[PrizeFundRepository, Depends(get_prize_fund_repository)]
PayoutRepoDep = Annotated[PayoutRepository, Depends(get_payout_repository)]
CheckoutGatewayDep = Annotated[
    SubscriptionCheckoutGateway, Depends(get_checkout_gateway)
]
PayoutGatewayDep = Annotated[PayoutGateway, Depends(get_payout_gateway)]
SeasonDirectoryDep = Annotated[SeasonDirectory, Depends(get_season_directory)]
AuditDep = Annotated[AuditTrail, Depends(get_audit_trail)]


# ── Конфигурация ──────────────────────────────────────────────────────────


def get_plan_prices(settings: SettingsDep) -> Mapping[SubscriptionPlan, int]:
    """Карта «тариф → цена в копейках» из настроек."""
    return {
        SubscriptionPlan.DAILY: settings.billing.daily_price_kopecks,
        SubscriptionPlan.WEEKLY: settings.billing.weekly_price_kopecks,
        SubscriptionPlan.MONTHLY: settings.billing.monthly_price_kopecks,
        SubscriptionPlan.ANNUAL: settings.billing.annual_price_kopecks,
    }


PlanPricesDep = Annotated[Mapping[SubscriptionPlan, int], Depends(get_plan_prices)]


# ── Актор (RBAC/SoD) ──────────────────────────────────────────────────────


def get_actor(current_user: CurrentUser) -> Actor:
    """Актор операции из аутентифицированного пользователя identity."""
    return Actor(user_id=current_user.id, role=current_user.role)


ActorDep = Annotated[Actor, Depends(get_actor)]


# ── Use-cases ─────────────────────────────────────────────────────────────


def get_start_subscription(
    subscriptions: SubscriptionRepoDep,
    checkout: CheckoutGatewayDep,
    audit: AuditDep,
    clock: ClockDep,
    plan_prices: PlanPricesDep,
    settings: SettingsDep,
) -> StartSubscription:
    """Use-case оформления подписки (локально — мгновенная активация)."""
    return StartSubscription(
        subscriptions=subscriptions,
        checkout=checkout,
        audit=audit,
        clock=clock,
        plan_prices=plan_prices,
        instant_activate=settings.app_env == "local",
    )


def get_cancel_subscription(
    subscriptions: SubscriptionRepoDep, audit: AuditDep, clock: ClockDep
) -> CancelSubscription:
    """Use-case отмены подписки."""
    return CancelSubscription(subscriptions=subscriptions, audit=audit, clock=clock)


def get_my_subscription(subscriptions: SubscriptionRepoDep) -> GetMySubscription:
    """Use-case чтения текущей подписки пользователя."""
    return GetMySubscription(subscriptions=subscriptions)


def get_record_subscription_payment(
    payments: PaymentRepoDep,
    subscriptions: SubscriptionRepoDep,
    ledger: LedgerRepoDep,
    audit: AuditDep,
    clock: ClockDep,
) -> RecordSubscriptionPayment:
    """Use-case приёма платежа по подписке (вебхук → OPERATIONS)."""
    return RecordSubscriptionPayment(
        payments=payments,
        subscriptions=subscriptions,
        ledger=ledger,
        audit=audit,
        clock=clock,
    )


def get_announce_prize_fund(
    funds: PrizeFundRepoDep, ledger: LedgerRepoDep, audit: AuditDep, clock: ClockDep
) -> AnnouncePrizeFund:
    """Use-case заведения призового фонда."""
    return AnnouncePrizeFund(funds=funds, ledger=ledger, audit=audit, clock=clock)


def get_record_sponsor_deposit(
    funds: PrizeFundRepoDep, ledger: LedgerRepoDep, audit: AuditDep, clock: ClockDep
) -> RecordSponsorDeposit:
    """Use-case регистрации поступления спонсора (→ PRIZE)."""
    return RecordSponsorDeposit(funds=funds, ledger=ledger, audit=audit, clock=clock)


def get_list_my_sponsor_funds(
    funds: PrizeFundRepoDep, ledger: LedgerRepoDep
) -> ListMySponsorFunds:
    """Use-case списка фондов спонсора (кабинет)."""
    return ListMySponsorFunds(funds=funds, ledger=ledger)


def get_my_sponsor_fund(
    funds: PrizeFundRepoDep, ledger: LedgerRepoDep, payouts: PayoutRepoDep
) -> GetMySponsorFund:
    """Use-case деталей фонда спонсора (кабинет)."""
    return GetMySponsorFund(funds=funds, ledger=ledger, payouts=payouts)


def get_prize_fund(
    funds: PrizeFundRepoDep, ledger: LedgerRepoDep
) -> GetPrizeFund:
    """Use-case чтения фонда (прозрачность)."""
    return GetPrizeFund(funds=funds, ledger=ledger)


def get_create_payout(
    payouts: PayoutRepoDep, funds: PrizeFundRepoDep, audit: AuditDep, clock: ClockDep
) -> CreatePayout:
    """Use-case инициирования выплаты (maker)."""
    return CreatePayout(payouts=payouts, funds=funds, audit=audit, clock=clock)


def get_list_payouts(payouts: PayoutRepoDep) -> ListPayouts:
    """Use-case админ-обзора выплат."""
    return ListPayouts(payouts=payouts)


def get_list_my_payouts(payouts: PayoutRepoDep) -> ListMyPayouts:
    """Use-case своих выплат."""
    return ListMyPayouts(payouts=payouts)


def get_dispatch_payout(
    payouts: PayoutRepoDep,
    gateway: PayoutGatewayDep,
    audit: AuditDep,
    clock: ClockDep,
) -> DispatchPayout:
    """Use-case отправки подтверждённой выплаты провайдеру."""
    return DispatchPayout(
        payouts=payouts, gateway=gateway, audit=audit, clock=clock
    )


def get_record_payout_result(
    payouts: PayoutRepoDep, audit: AuditDep, clock: ClockDep
) -> RecordPayoutResult:
    """Use-case приёма результата выплаты из вебхука провайдера."""
    return RecordPayoutResult(payouts=payouts, audit=audit, clock=clock)


def get_season_prize_fund(
    seasons: SeasonDirectoryDep,
    funds: PrizeFundRepoDep,
    payouts: PayoutRepoDep,
    ledger: LedgerRepoDep,
) -> GetSeasonPrizeFund:
    """Use-case прозрачности фонда по сезону."""
    return GetSeasonPrizeFund(
        seasons=seasons, funds=funds, payouts=payouts, ledger=ledger
    )


def get_approve_payout(
    payouts: PayoutRepoDep,
    funds: PrizeFundRepoDep,
    ledger: LedgerRepoDep,
    audit: AuditDep,
    clock: ClockDep,
) -> ApprovePayout:
    """Use-case подтверждения выплаты (checker, → PRIZE)."""
    return ApprovePayout(
        payouts=payouts, funds=funds, ledger=ledger, audit=audit, clock=clock
    )


# ── Верификация подписи вебхуков ──────────────────────────────────────────

_SIGNATURE_HEADER = "x-signature"


async def verify_payment_webhook(request: Request, settings: SettingsDep) -> None:
    """Проверяет подпись вебхука приёма платежа (HMAC по телу).

    Пустой секрет (``WEBHOOK_YOOKASSA_PAYMENT_SECRET``) — верификация выключена
    (dev/тест). При заданном секрете неверная/отсутствующая подпись → 401.
    """
    body = await request.body()
    signature = request.headers.get(_SIGNATURE_HEADER)
    if not verify_signature(
        settings.webhooks.yookassa_payment_secret, body, signature
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверная подпись вебхука",
        )


async def verify_payout_webhook(request: Request, settings: SettingsDep) -> None:
    """Проверяет подпись вебхука результата выплаты (HMAC по телу)."""
    body = await request.body()
    signature = request.headers.get(_SIGNATURE_HEADER)
    if not verify_signature(
        settings.webhooks.yookassa_payout_secret, body, signature
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверная подпись вебхука",
        )
