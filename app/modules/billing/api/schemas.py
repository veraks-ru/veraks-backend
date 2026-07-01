"""Pydantic-схемы запросов/ответов billing (тонкий транспортный слой)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.billing.application.dto import PrizeFundView, SeasonPrizeFundView
from app.modules.billing.application.use_cases import SponsorFundDetail
from app.modules.billing.domain.entities import (
    Payment,
    PaymentProvider,
    PaymentStatus,
    Payout,
    PayoutStatus,
    PrizeFund,
    PrizeFundStatus,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)


# ── Тарифы ────────────────────────────────────────────────────────────────


class PlanResponse(BaseModel):
    """Тариф подписки и его цена (копейки)."""

    plan: SubscriptionPlan
    price_kopecks: int


class PlansResponse(BaseModel):
    """Список доступных тарифов подписки."""

    plans: list[PlanResponse]


# ── Подписки ──────────────────────────────────────────────────────────────


class StartSubscriptionRequest(BaseModel):
    """Оформление подписки."""

    plan: SubscriptionPlan = Field(description="Тариф подписки")
    provider: PaymentProvider = Field(
        default=PaymentProvider.YOOKASSA, description="Платёжный провайдер"
    )


class SubscriptionResponse(BaseModel):
    """Проекция подписки."""

    id: uuid.UUID
    user_id: uuid.UUID
    plan: SubscriptionPlan
    price_kopecks: int
    provider: PaymentProvider
    status: SubscriptionStatus
    current_period_end: datetime | None

    @classmethod
    def from_domain(cls, sub: Subscription) -> "SubscriptionResponse":
        return cls(
            id=sub.id,
            user_id=sub.user_id,
            plan=sub.plan,
            price_kopecks=sub.price_kopecks,
            provider=sub.provider,
            status=sub.status,
            current_period_end=sub.current_period_end,
        )


class StartSubscriptionResponse(BaseModel):
    """Созданная подписка и URL оплаты у провайдера."""

    subscription: SubscriptionResponse
    confirmation_url: str


# ── Вебхуки платежей ──────────────────────────────────────────────────────


class PaymentWebhookRequest(BaseModel):
    """Упрощённое тело вебхука приёма платежа.

    TODO(billing-infra): заменить на реальную схему провайдера и проверять
    подпись вебхука в адаптере до вызова use-case.
    """

    provider: PaymentProvider
    provider_payment_id: str = Field(min_length=1)
    amount_kopecks: int = Field(gt=0)
    subscription_id: uuid.UUID | None = None


class PayoutWebhookRequest(BaseModel):
    """Упрощённое тело вебхука результата выплаты физлицу.

    TODO(billing-infra): заменить на реальную схему провайдера и проверять
    подпись вебхука в адаптере до вызова use-case.
    """

    provider: PaymentProvider
    provider_payout_id: str = Field(min_length=1)
    succeeded: bool


class PaymentResponse(BaseModel):
    """Проекция платежа."""

    id: uuid.UUID
    provider: PaymentProvider
    provider_payment_id: str
    amount_kopecks: int
    status: PaymentStatus
    ledger_transaction_id: uuid.UUID | None
    paid_at: datetime | None

    @classmethod
    def from_domain(cls, payment: Payment) -> "PaymentResponse":
        return cls(
            id=payment.id,
            provider=payment.provider,
            provider_payment_id=payment.provider_payment_id,
            amount_kopecks=payment.amount_kopecks,
            status=payment.status,
            ledger_transaction_id=payment.ledger_transaction_id,
            paid_at=payment.paid_at,
        )


# ── Призовой фонд ─────────────────────────────────────────────────────────


class AnnouncePrizeFundRequest(BaseModel):
    """Заведение призового фонда (admin)."""

    sponsor_name: str = Field(min_length=1)
    committed_kopecks: int = Field(ge=0)
    season_id: uuid.UUID | None = None
    sponsor_ref: str = ""


class RecordDepositRequest(BaseModel):
    """Регистрация поступления спонсора в фонд (admin)."""

    amount_kopecks: int = Field(gt=0)
    external_ref: str | None = None


class PrizeFundResponse(BaseModel):
    """Проекция фонда + фактическое сальдо кассы (прозрачность)."""

    id: uuid.UUID
    sponsor_name: str
    season_id: uuid.UUID | None
    committed_kopecks: int
    deposited_kopecks: int
    balance_kopecks: int
    status: PrizeFundStatus

    @classmethod
    def from_view(cls, view: PrizeFundView) -> "PrizeFundResponse":
        fund: PrizeFund = view.fund
        return cls(
            id=fund.id,
            sponsor_name=fund.sponsor_name,
            season_id=fund.season_id,
            committed_kopecks=fund.committed_kopecks,
            deposited_kopecks=fund.deposited_kopecks,
            balance_kopecks=view.balance_kopecks,
            status=fund.status,
        )

    @classmethod
    def from_domain(cls, fund: PrizeFund, *, balance_kopecks: int) -> "PrizeFundResponse":
        return cls(
            id=fund.id,
            sponsor_name=fund.sponsor_name,
            season_id=fund.season_id,
            committed_kopecks=fund.committed_kopecks,
            deposited_kopecks=fund.deposited_kopecks,
            balance_kopecks=balance_kopecks,
            status=fund.status,
        )


# ── Выплаты ───────────────────────────────────────────────────────────────


class CreatePayoutRequest(BaseModel):
    """Инициирование выплаты победителю (maker, admin)."""

    user_id: uuid.UUID
    prize_fund_id: uuid.UUID
    amount_kopecks: int = Field(gt=0, description="Сумма к получению (нетто)")
    tax_withheld_kopecks: int = Field(default=0, ge=0, description="Удержанный НДФЛ")
    season_id: uuid.UUID | None = None


class PayoutResponse(BaseModel):
    """Проекция выплаты."""

    id: uuid.UUID
    user_id: uuid.UUID
    prize_fund_id: uuid.UUID
    amount_kopecks: int
    tax_withheld_kopecks: int
    status: PayoutStatus
    created_by: uuid.UUID
    approved_by: uuid.UUID | None
    ledger_transaction_id: uuid.UUID | None

    @classmethod
    def from_domain(cls, payout: Payout) -> "PayoutResponse":
        return cls(
            id=payout.id,
            user_id=payout.user_id,
            prize_fund_id=payout.prize_fund_id,
            amount_kopecks=payout.amount_kopecks,
            tax_withheld_kopecks=payout.tax_withheld_kopecks,
            status=payout.status,
            created_by=payout.created_by,
            approved_by=payout.approved_by,
            ledger_transaction_id=payout.ledger_transaction_id,
        )


class SeasonPrizeFundResponse(BaseModel):
    """Прозрачность по сезону: фонды (с сальдо) и история выплат."""

    season_slug: str
    funds: list[PrizeFundResponse]
    payouts: list[PayoutResponse]

    @classmethod
    def from_view(cls, view: SeasonPrizeFundView) -> "SeasonPrizeFundResponse":
        return cls(
            season_slug=view.season_slug,
            funds=[PrizeFundResponse.from_view(f) for f in view.funds],
            payouts=[PayoutResponse.from_domain(p) for p in view.payouts],
        )


# ── Кабинет спонсора ────────────────────────────────────────────────────────


class SponsorFundDetailResponse(BaseModel):
    """Детали фонда спонсора: проекция фонда, остаток и его выплаты."""

    fund: PrizeFundResponse
    available_kopecks: int
    payouts: list[PayoutResponse]

    @classmethod
    def from_detail(cls, detail: "SponsorFundDetail") -> "SponsorFundDetailResponse":
        return cls(
            fund=PrizeFundResponse.from_domain(
                detail.fund, balance_kopecks=detail.available_kopecks
            ),
            available_kopecks=detail.available_kopecks,
            payouts=[PayoutResponse.from_domain(p) for p in detail.payouts],
        )
