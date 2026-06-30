"""Доменные сущности денежных доменов billing.

Подписки/платежи — операционная касса; призовые фонды/выплаты — призовая.
Сами сущности не считают проводки: движение денег фиксируется в журнале
(:mod:`app.modules.billing.domain.ledger`), а здесь хранится прикладное
состояние (статусы, периоды, суммы-зеркала) и переходы между статусами.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.billing.domain.errors import (
    InvalidAmountError,
    PayoutAlreadyDecidedError,
)


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


# ── Подписки и платежи (OPERATIONS) ───────────────────────────────────────


class SubscriptionPlan(str, enum.Enum):
    """Тариф подписки."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ANNUAL = "annual"


class SubscriptionStatus(str, enum.Enum):
    """Жизненный цикл подписки."""

    INCOMPLETE = "incomplete"  # создана, ждёт первого успешного платежа
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"


class PaymentProvider(str, enum.Enum):
    """Платёжный провайдер."""

    YOOKASSA = "yookassa"
    TBANK = "tbank"


class PaymentStatus(str, enum.Enum):
    """Статус платежа из вебхуков провайдера."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"
    REFUNDED = "refunded"


class PaymentPurpose(str, enum.Enum):
    """Назначение платежа (всегда операционная касса)."""

    SUBSCRIPTION = "subscription"
    B2B = "b2b"


@dataclass(slots=True)
class Subscription:
    """Подписка пользователя. Каждое списание → проводка OPERATIONS."""

    user_id: uuid.UUID
    plan: SubscriptionPlan
    price_kopecks: int
    provider: PaymentProvider
    status: SubscriptionStatus = SubscriptionStatus.INCOMPLETE
    provider_subscription_id: str | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)
    canceled_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if self.price_kopecks <= 0:
            raise InvalidAmountError("Цена подписки должна быть > 0")

    def activate(
        self, *, period_start: datetime, period_end: datetime
    ) -> None:
        """Перевести в ``active`` и установить оплаченный период."""
        self.status = SubscriptionStatus.ACTIVE
        self.current_period_start = period_start
        self.current_period_end = period_end

    def cancel(self, *, now: datetime) -> None:
        """Отменить подписку (идемпотентно для уже отменённой)."""
        if self.status is SubscriptionStatus.CANCELED:
            return
        self.status = SubscriptionStatus.CANCELED
        self.canceled_at = now


@dataclass(slots=True)
class Payment:
    """Факт приёма средств из вебхука провайдера. Всегда OPERATIONS.

    ``ledger_transaction_id`` связывает платёж с проводкой операционной кассы.
    Идемпотентность вебхуков — по ``UNIQUE(provider, provider_payment_id)``.
    """

    provider: PaymentProvider
    provider_payment_id: str
    amount_kopecks: int
    purpose: PaymentPurpose
    status: PaymentStatus
    user_id: uuid.UUID | None = None
    subscription_id: uuid.UUID | None = None
    fiscal_receipt_id: str | None = None  # TODO(billing-infra): чек 54-ФЗ от ОФД
    ledger_transaction_id: uuid.UUID | None = None
    paid_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)


# ── Призовой фонд и выплаты (PRIZE) ───────────────────────────────────────


class PrizeFundStatus(str, enum.Enum):
    """Жизненный цикл призового фонда."""

    ANNOUNCED = "announced"
    FUNDED = "funded"
    DISTRIBUTING = "distributing"
    CLOSED = "closed"


class PayoutStatus(str, enum.Enum):
    """Жизненный цикл выплаты победителю (maker-checker)."""

    PENDING = "pending"  # создана инициатором (maker), ждёт подтверждения
    APPROVED = "approved"  # подтверждена другим (checker), проведена в PRIZE
    PROCESSING = "processing"  # отправлена провайдеру выплат
    PAID = "paid"
    FAILED = "failed"


@dataclass(slots=True)
class PrizeFund:
    """Призовой фонд (спонсорские деньги на номинальном/эскроу-счёте PRIZE).

    ``ledger_account_id`` — счёт кассы PRIZE, на котором копится фонд. Поступление
    спонсора → проводка ``sponsor_deposit``; ``deposited_kopecks`` — зеркало.
    """

    sponsor_name: str
    ledger_account_id: uuid.UUID
    committed_kopecks: int
    season_id: uuid.UUID | None = None
    sponsor_ref: str = ""
    deposited_kopecks: int = 0
    status: PrizeFundStatus = PrizeFundStatus.ANNOUNCED
    created_at: datetime = field(default_factory=_utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if self.committed_kopecks < 0:
            raise InvalidAmountError("Заявленная сумма фонда не может быть < 0")

    def record_deposit(self, amount_kopecks: int) -> None:
        """Зарегистрировать поступление от спонсора; обновить статус."""
        if amount_kopecks <= 0:
            raise InvalidAmountError("Поступление в фонд должно быть > 0")
        self.deposited_kopecks += amount_kopecks
        if self.status is PrizeFundStatus.ANNOUNCED:
            self.status = PrizeFundStatus.FUNDED


@dataclass(slots=True)
class Payout:
    """Выплата победителю из призового фонда (касса PRIZE).

    ``amount_kopecks`` — сумма к получению победителем (нетто);
    ``tax_withheld_kopecks`` — удержанный НДФЛ (платформа как налоговый агент,
    TODO с юристом). Брутто, списываемое с фонда, = нетто + налог.
    Подтверждение — другим пользователем (maker-checker): ``created_by`` ≠
    ``approved_by``.
    """

    user_id: uuid.UUID
    prize_fund_id: uuid.UUID
    amount_kopecks: int
    created_by: uuid.UUID
    season_id: uuid.UUID | None = None
    tax_withheld_kopecks: int = 0
    status: PayoutStatus = PayoutStatus.PENDING
    provider: PaymentProvider | None = None
    provider_payout_id: str | None = None
    approved_by: uuid.UUID | None = None
    ledger_transaction_id: uuid.UUID | None = None
    created_at: datetime = field(default_factory=_utcnow)
    paid_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if self.amount_kopecks <= 0:
            raise InvalidAmountError("Сумма выплаты должна быть > 0")
        if self.tax_withheld_kopecks < 0:
            raise InvalidAmountError("Удержанный налог не может быть < 0")

    @property
    def gross_kopecks(self) -> int:
        """Брутто, списываемое с фонда: нетто + удержанный налог."""
        return self.amount_kopecks + self.tax_withheld_kopecks

    def approve(self, *, approver_id: uuid.UUID) -> None:
        """Подтвердить выплату (checker). Один раз; не самоподтверждение.

        Проверку ``approver_id != created_by`` делает прикладной слой (политика),
        чтобы вернуть специализированную ошибку до перевода статуса.
        """
        if self.status is not PayoutStatus.PENDING:
            raise PayoutAlreadyDecidedError(
                f"Выплата уже в статусе {self.status.value}"
            )
        self.status = PayoutStatus.APPROVED
        self.approved_by = approver_id

    def mark_processing(
        self, *, provider: PaymentProvider, provider_payout_id: str
    ) -> None:
        """Отметить отправку провайдеру выплат (только из ``approved``)."""
        if self.status is not PayoutStatus.APPROVED:
            raise PayoutAlreadyDecidedError(
                f"Отправить можно только подтверждённую выплату, статус "
                f"{self.status.value}"
            )
        self.status = PayoutStatus.PROCESSING
        self.provider = provider
        self.provider_payout_id = provider_payout_id

    def mark_paid(self, *, now: datetime) -> None:
        """Зафиксировать успешную выплату (только из ``processing``)."""
        if self.status is not PayoutStatus.PROCESSING:
            raise PayoutAlreadyDecidedError(
                f"Отметить оплаченной можно только отправленную выплату, статус "
                f"{self.status.value}"
            )
        self.status = PayoutStatus.PAID
        self.paid_at = now

    def mark_failed(self) -> None:
        """Зафиксировать неуспех выплаты у провайдера (только из ``processing``)."""
        if self.status is not PayoutStatus.PROCESSING:
            raise PayoutAlreadyDecidedError(
                f"Отметить неуспешной можно только отправленную выплату, статус "
                f"{self.status.value}"
            )
        self.status = PayoutStatus.FAILED
