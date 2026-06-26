"""HTTP-эндпоинты billing (тонкий транспорт).

Парсит вход → делегирует use-case → маппит домен в схему. Доменные ошибки
превращаются в HTTP централизованно в ``app/main.py``. RBAC/SoD — в use-cases
через ``Actor``, а не в роутере.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.modules.billing.api.dependencies import (
    ActorDep,
    PlanPricesDep,
    get_announce_prize_fund,
    get_approve_payout,
    get_cancel_subscription,
    get_create_payout,
    get_dispatch_payout,
    get_list_my_payouts,
    get_list_payouts,
    get_my_subscription,
    get_prize_fund,
    get_record_payout_result,
    get_season_prize_fund,
    get_record_sponsor_deposit,
    get_record_subscription_payment,
    get_start_subscription,
    verify_payment_webhook,
    verify_payout_webhook,
)
from app.modules.billing.api.schemas import (
    AnnouncePrizeFundRequest,
    CreatePayoutRequest,
    PaymentResponse,
    PaymentWebhookRequest,
    PayoutResponse,
    PayoutWebhookRequest,
    PlanResponse,
    PlansResponse,
    PrizeFundResponse,
    RecordDepositRequest,
    SeasonPrizeFundResponse,
    StartSubscriptionRequest,
    StartSubscriptionResponse,
    SubscriptionResponse,
)
from app.modules.billing.application.use_cases import (
    AnnouncePrizeFund,
    ApprovePayout,
    CancelSubscription,
    CreatePayout,
    DispatchPayout,
    GetMySubscription,
    GetPrizeFund,
    GetSeasonPrizeFund,
    ListMyPayouts,
    ListPayouts,
    RecordPayoutResult,
    RecordSponsorDeposit,
    RecordSubscriptionPayment,
    StartSubscription,
)

router = APIRouter(tags=["billing"])


# ── Тарифы ────────────────────────────────────────────────────────────────


@router.get(
    "/billing/plans",
    response_model=PlansResponse,
    summary="Тарифы подписки",
)
async def list_plans(plan_prices: PlanPricesDep) -> PlansResponse:
    """Вернуть доступные тарифы и их цены (копейки) из конфигурации."""
    return PlansResponse(
        plans=[
            PlanResponse(plan=plan, price_kopecks=price)
            for plan, price in plan_prices.items()
        ]
    )


# ── Подписки (операционная касса) ─────────────────────────────────────────


@router.post(
    "/billing/subscriptions",
    response_model=StartSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Оформить подписку → URL оплаты",
)
async def start_subscription(
    payload: StartSubscriptionRequest,
    actor: ActorDep,
    uc: Annotated[StartSubscription, Depends(get_start_subscription)],
) -> StartSubscriptionResponse:
    """Создать подписку текущего пользователя и вернуть URL оплаты провайдера."""
    subscription, confirmation_url = await uc.execute(
        user_id=actor.user_id, plan=payload.plan, provider=payload.provider
    )
    return StartSubscriptionResponse(
        subscription=SubscriptionResponse.from_domain(subscription),
        confirmation_url=confirmation_url,
    )


@router.get(
    "/users/me/payouts",
    response_model=list[PayoutResponse],
    summary="Свои выплаты",
)
async def read_my_payouts(
    actor: ActorDep,
    uc: Annotated[ListMyPayouts, Depends(get_list_my_payouts)],
) -> list[PayoutResponse]:
    """Вернуть выплаты текущего пользователя (новые сверху)."""
    payouts = await uc.execute(user_id=actor.user_id)
    return [PayoutResponse.from_domain(p) for p in payouts]


@router.get(
    "/billing/subscriptions/me",
    response_model=SubscriptionResponse,
    summary="Своя подписка",
)
async def read_my_subscription(
    actor: ActorDep,
    uc: Annotated[GetMySubscription, Depends(get_my_subscription)],
) -> SubscriptionResponse:
    """Вернуть текущую (последнюю) подписку пользователя; 404, если нет."""
    subscription = await uc.execute(user_id=actor.user_id)
    return SubscriptionResponse.from_domain(subscription)


@router.post(
    "/billing/subscriptions/{subscription_id}/cancel",
    response_model=SubscriptionResponse,
    summary="Отменить подписку",
)
async def cancel_subscription(
    subscription_id: uuid.UUID,
    actor: ActorDep,
    uc: Annotated[CancelSubscription, Depends(get_cancel_subscription)],
) -> SubscriptionResponse:
    """Отменить подписку (владелец или admin)."""
    subscription = await uc.execute(subscription_id=subscription_id, actor=actor)
    return SubscriptionResponse.from_domain(subscription)


@router.post(
    "/webhooks/payments/yookassa",
    response_model=PaymentResponse,
    summary="Вебхук приёма платежа (→ операционная касса)",
    dependencies=[Depends(verify_payment_webhook)],
)
async def yookassa_payment_webhook(
    payload: PaymentWebhookRequest,
    uc: Annotated[RecordSubscriptionPayment, Depends(get_record_subscription_payment)],
) -> PaymentResponse:
    """Идемпотентно принять платёж и провести его в OPERATIONS.

    Подпись вебхука проверяется зависимостью ``verify_payment_webhook`` до входа
    (HMAC по телу; при заданном ``WEBHOOK_YOOKASSA_PAYMENT_SECRET``).
    """
    payment = await uc.execute(
        provider=payload.provider,
        provider_payment_id=payload.provider_payment_id,
        amount_kopecks=payload.amount_kopecks,
        subscription_id=payload.subscription_id,
    )
    return PaymentResponse.from_domain(payment)


# ── Призовой фонд (призовая касса) ────────────────────────────────────────


@router.get(
    "/prize-funds/{fund_id}",
    response_model=PrizeFundResponse,
    summary="Призовой фонд + сальдо (публично, прозрачность)",
)
async def read_prize_fund(
    fund_id: uuid.UUID,
    uc: Annotated[GetPrizeFund, Depends(get_prize_fund)],
) -> PrizeFundResponse:
    """Вернуть фонд и фактическое сальдо его счёта PRIZE."""
    view = await uc.execute(fund_id=fund_id)
    return PrizeFundResponse.from_view(view)


@router.get(
    "/seasons/{slug}/prize-fund",
    response_model=SeasonPrizeFundResponse,
    summary="Фонд сезона + история выплат (публично, прозрачность)",
)
async def read_season_prize_fund(
    slug: str,
    uc: Annotated[GetSeasonPrizeFund, Depends(get_season_prize_fund)],
) -> SeasonPrizeFundResponse:
    """Вернуть фонды сезона (с сальдо) и историю выплат; 404 для неизвестного сезона."""
    view = await uc.execute(slug=slug)
    return SeasonPrizeFundResponse.from_view(view)


@router.post(
    "/admin/prize-funds",
    response_model=PrizeFundResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Завести призовой фонд (admin)",
)
async def announce_prize_fund(
    payload: AnnouncePrizeFundRequest,
    actor: ActorDep,
    uc: Annotated[AnnouncePrizeFund, Depends(get_announce_prize_fund)],
) -> PrizeFundResponse:
    """Создать фонд и его счёт в кассе PRIZE."""
    fund = await uc.execute(
        actor=actor,
        sponsor_name=payload.sponsor_name,
        committed_kopecks=payload.committed_kopecks,
        season_id=payload.season_id,
        sponsor_ref=payload.sponsor_ref,
    )
    return PrizeFundResponse.from_domain(fund, balance_kopecks=0)


@router.post(
    "/admin/prize-funds/{fund_id}/deposit",
    response_model=PrizeFundResponse,
    summary="Зарегистрировать поступление спонсора (admin)",
)
async def record_sponsor_deposit(
    fund_id: uuid.UUID,
    payload: RecordDepositRequest,
    actor: ActorDep,
    uc: Annotated[RecordSponsorDeposit, Depends(get_record_sponsor_deposit)],
) -> PrizeFundResponse:
    """Провести депозит спонсора в фонд (касса PRIZE)."""
    fund = await uc.execute(
        actor=actor,
        fund_id=fund_id,
        amount_kopecks=payload.amount_kopecks,
        external_ref=payload.external_ref,
    )
    return PrizeFundResponse.from_domain(fund, balance_kopecks=fund.deposited_kopecks)


# ── Выплаты призов (призовая касса, maker-checker) ────────────────────────


@router.get(
    "/admin/payouts",
    response_model=list[PayoutResponse],
    summary="Список выплат (admin, опц. фильтр по сезону)",
)
async def list_payouts(
    actor: ActorDep,
    uc: Annotated[ListPayouts, Depends(get_list_payouts)],
    season_id: uuid.UUID | None = None,
) -> list[PayoutResponse]:
    """Вернуть выплаты (новые сверху); только admin."""
    payouts = await uc.execute(actor=actor, season_id=season_id)
    return [PayoutResponse.from_domain(p) for p in payouts]


@router.post(
    "/admin/payouts",
    response_model=PayoutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Начислить выплату (maker, admin)",
)
async def create_payout(
    payload: CreatePayoutRequest,
    actor: ActorDep,
    uc: Annotated[CreatePayout, Depends(get_create_payout)],
) -> PayoutResponse:
    """Инициировать выплату победителю (статус pending, ждёт подтверждения)."""
    payout = await uc.execute(
        actor=actor,
        user_id=payload.user_id,
        prize_fund_id=payload.prize_fund_id,
        amount_kopecks=payload.amount_kopecks,
        tax_withheld_kopecks=payload.tax_withheld_kopecks,
        season_id=payload.season_id,
    )
    return PayoutResponse.from_domain(payout)


@router.post(
    "/admin/payouts/{payout_id}/approve",
    response_model=PayoutResponse,
    summary="Подтвердить выплату (checker, admin)",
)
async def approve_payout(
    payout_id: uuid.UUID,
    actor: ActorDep,
    uc: Annotated[ApprovePayout, Depends(get_approve_payout)],
) -> PayoutResponse:
    """Подтвердить выплату (другой admin) и провести её в кассе PRIZE."""
    payout = await uc.execute(actor=actor, payout_id=payout_id)
    return PayoutResponse.from_domain(payout)


@router.post(
    "/admin/payouts/{payout_id}/dispatch",
    response_model=PayoutResponse,
    summary="Отправить подтверждённую выплату провайдеру (admin)",
)
async def dispatch_payout(
    payout_id: uuid.UUID,
    actor: ActorDep,
    uc: Annotated[DispatchPayout, Depends(get_dispatch_payout)],
) -> PayoutResponse:
    """Отправляет выплату провайдеру (``approved → processing``)."""
    payout = await uc.execute(actor=actor, payout_id=payout_id)
    return PayoutResponse.from_domain(payout)


@router.post(
    "/webhooks/payouts/yookassa",
    response_model=PayoutResponse,
    summary="Вебхук результата выплаты (→ paid/failed)",
    dependencies=[Depends(verify_payout_webhook)],
)
async def yookassa_payout_webhook(
    payload: PayoutWebhookRequest,
    uc: Annotated[RecordPayoutResult, Depends(get_record_payout_result)],
) -> PayoutResponse:
    """Идемпотентно фиксирует исход выплаты у провайдера.

    Подпись вебхука проверяется зависимостью ``verify_payout_webhook`` до входа
    (HMAC по телу; при заданном ``WEBHOOK_YOOKASSA_PAYOUT_SECRET``).
    """
    payout = await uc.execute(
        provider=payload.provider,
        provider_payout_id=payload.provider_payout_id,
        succeeded=payload.succeeded,
    )
    return PayoutResponse.from_domain(payout)
