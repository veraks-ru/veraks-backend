"""Доменные сущности денежных доменов billing.

Подписки/платежи — операционная касса; призовые фонды/выплаты — призовая.
Сами сущности не считают проводки: движение денег фиксируется в журнале
(:mod:`app.modules.billing.domain.ledger`), а здесь хранится прикладное
состояние (статусы, периоды, суммы-зеркала) и переходы между статусами.
"""

from __future__ import annotations

import enum
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.modules.billing.domain.errors import (
    InvalidAmountError,
    InvalidInviteError,
    InvalidRecurrentError,
    InvalidRequisiteError,
    InviteAlreadyRedeemedError,
    InviteRevokedError,
    PayoutAlreadyDecidedError,
)


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(UTC)


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
    JUMP = "jump"


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
    # Токен провайдера для списаний без участия человека (ТБанк: RebillId).
    # Появляется из уведомления о первом — родительском — платеже.
    rebill_id: str | None = None
    # Продлевать ли автоматически. Включается вместе с rebill_id: без токена
    # списывать нечем, поэтому одно без другого бессмысленно.
    auto_renew: bool = False
    # Подряд идущие неудачные попытки списания. Сбрасывается успешной оплатой.
    renewal_attempts: int = 0
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
        # Удачное списание закрывает череду неудачных: следующий цикл
        # начинается с чистого счётчика.
        self.renewal_attempts = 0

    def cancel(self, *, now: datetime) -> None:
        """Отменить подписку (идемпотентно для уже отменённой).

        Автопродление выключается вместе с отменой: списать с карты человека,
        который отказался от услуги, нельзя ни при каких обстоятельствах.
        Оплаченный период при этом не отбирается — доступ живёт до
        ``current_period_end`` (так же обещает оферта).
        """
        self.auto_renew = False
        if self.status is SubscriptionStatus.CANCELED:
            return
        self.status = SubscriptionStatus.CANCELED
        self.canceled_at = now

    # ── Автопродление ──────────────────────────────────────────────────────

    def enable_auto_renew(self, *, rebill_id: str) -> None:
        """Запомнить токен провайдера и включить автосписание."""
        if not rebill_id:
            raise InvalidRecurrentError("Провайдер не вернул токен списания")
        self.rebill_id = rebill_id
        self.auto_renew = True
        self.renewal_attempts = 0

    def stop_auto_renew(self) -> None:
        """Больше не продлевать; оплаченный период остаётся за человеком.

        Токен списания стираем, а не просто снимаем флаг: хранить средство
        платежа без основания незачем, а вернуть автопродление всё равно можно
        только новой оплатой.
        """
        self.auto_renew = False
        self.rebill_id = None

    def is_due_for_renewal(self, *, now: datetime, lead: timedelta) -> bool:
        """Пора ли списывать: период кончается в пределах ``lead``."""
        if not self.auto_renew or not self.rebill_id:
            return False
        if self.status not in (
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.PAST_DUE,
        ):
            return False
        if self.current_period_end is None:
            return False
        return self.current_period_end <= now + lead

    def note_renewal_failure(self, *, max_attempts: int) -> None:
        """Учесть неудачное списание; после лимита — прекратить попытки.

        Банк отклоняет по разным причинам (нет денег, карта истекла), и часть
        из них проходит сама. Поэтому не сдаёмся с первого раза, но и не
        долбим карту бесконечно: после ``max_attempts`` человек продлевает
        руками.
        """
        self.renewal_attempts += 1
        self.status = SubscriptionStatus.PAST_DUE
        if self.renewal_attempts >= max_attempts:
            self.auto_renew = False


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
    # Пользователь-спонсор (владелец кабинета); ``None`` — фонд заведён админом.
    sponsor_user_id: uuid.UUID | None = None
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


def _normalize_sbp_phone(raw: str) -> str:
    """Нормализовать телефон СБП к виду ``+7XXXXXXXXXX``.

    Принимаются записи с пробелами/скобками/дефисами и префиксами
    ``8``/``7``/``+7``; всё остальное — :class:`InvalidRequisiteError`.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.strip().startswith("+") and not raw.strip().startswith("+7"):
        raise InvalidRequisiteError(f"Телефон СБП должен быть российским: {raw!r}")
    if len(digits) != 11 or digits[0] not in ("7", "8"):
        raise InvalidRequisiteError(f"Некорректный телефон СБП: {raw!r}")
    return f"+7{digits[1:]}"


@dataclass
class PayoutRequisites:
    """Реквизиты выплат пользователя: СБП по номеру телефона.

    Единственный поддерживаемый маршрут выплат — СБП; карты не храним.
    ФИО пользователь вводит раздельными полями (Jump требует их порознь,
    а в identity ФИО хранится одной строкой). ПДн шифруются адаптером
    хранения, домен работает с открытыми значениями.
    """

    user_id: uuid.UUID
    phone: str
    sbp_bank_id: str
    last_name: str
    first_name: str
    middle_name: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        self.phone = _normalize_sbp_phone(self.phone)
        self.sbp_bank_id = self.sbp_bank_id.strip()
        self.last_name = self.last_name.strip()
        self.first_name = self.first_name.strip()
        if self.middle_name is not None:
            self.middle_name = self.middle_name.strip() or None
        if not self.sbp_bank_id.isdigit():
            # У Jump id банка СБП — целое число (словарь /dictionaries).
            raise InvalidRequisiteError("Не указан банк СБП")
        if not self.last_name or not self.first_name:
            raise InvalidRequisiteError("Фамилия и имя обязательны")


# ── Пригласительный доступ (без денег) ────────────────────────────────────
#
# Голосовать можно с активной подпиской. Приглашение открывает ту же
# возможность, но без оплаты: платформе на старте нужны участники, а первым
# из них платить не за что — прогнозов ещё нет, рейтинга тоже.
#
# Намеренно НЕ подписка с нулевой ценой: подписка — платёжная сущность, у неё
# цена всегда положительная (см. ``Subscription.__post_init__``), и каждое
# списание отражается проводкой в кассе OPERATIONS. У выданного доступа нет
# ни платежа, ни проводки, и заводить ради него фиктивную запись в денежном
# контуре нельзя — это исказило бы отчётность по кассе.


def new_invite_code() -> str:
    """Код приглашения: 11 символов base64url, как в публичных ссылках."""
    return secrets.token_urlsafe(8)


@dataclass(slots=True)
class Invite:
    """Одноразовая пригласительная ссылка, дающая доступ без оплаты.

    ``duration_days is None`` — доступ бессрочный; иначе он истекает через
    указанное число дней после активации. Срок отсчитывается от активации, а
    не от создания: иначе приглашение, пролежавшее неделю в переписке,
    досталось бы человеку наполовину истёкшим.
    """

    created_by: uuid.UUID
    duration_days: int | None = None
    note: str = ""
    code: str = field(default_factory=new_invite_code)
    redeemed_by: uuid.UUID | None = None
    redeemed_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if self.duration_days is not None and self.duration_days <= 0:
            raise InvalidInviteError("Срок доступа должен быть больше нуля дней")

    @property
    def is_redeemed(self) -> bool:
        return self.redeemed_by is not None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def redeem(self, *, user_id: uuid.UUID, now: datetime) -> AccessGrant:
        """Погасить приглашение и выдать доступ.

        Проверки здесь, а не в use-case: одноразовость — свойство самого
        приглашения. Гонку двух одновременных активаций ловит уникальный
        индекс в БД, а не эта проверка.
        """
        if self.is_revoked:
            raise InviteRevokedError("Приглашение отозвано")
        if self.is_redeemed:
            raise InviteAlreadyRedeemedError("Приглашение уже использовано")

        self.redeemed_by = user_id
        self.redeemed_at = now
        expires_at = (
            None
            if self.duration_days is None
            else now + timedelta(days=self.duration_days)
        )
        return AccessGrant(
            user_id=user_id, invite_id=self.id, expires_at=expires_at, granted_at=now
        )

    def revoke(self, *, now: datetime) -> None:
        """Отозвать неиспользованное приглашение (для использованного — поздно)."""
        if self.is_redeemed:
            raise InviteAlreadyRedeemedError(
                "Приглашение уже использовано — доступ отзывается отдельно"
            )
        if self.is_revoked:
            return
        self.revoked_at = now


@dataclass(slots=True)
class AccessGrant:
    """Право голосовать, выданное по приглашению (без оплаты).

    ``expires_at is None`` — бессрочно. Когда срок выйдет, доступ придётся
    продлевать уже подпиской за деньги.
    """

    user_id: uuid.UUID
    invite_id: uuid.UUID
    expires_at: datetime | None = None
    granted_at: datetime = field(default_factory=_utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def is_active(self, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at > now
