"""Доменные примитивы журнала проводок (двойная запись, две кассы).

Здесь живут инварианты, которые в задании дублируются на уровне схемы БД
(триггеры ``enforce_ledger_separation`` и проверка баланса транзакции):

* проводка целиком принадлежит ОДНОЙ кассе (``ledger_type``); перетекание
  между кассами структурно невозможно — нет транзакции с ногами в разных кассах;
* сумма дебетов равна сумме кредитов внутри транзакции;
* каждая нога — строго положительная сумма в копейках.

Деньги — только целые копейки (``int``); никаких float. Сущности — обычные
dataclass'ы без I/O.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.billing.domain.errors import (
    CrossLedgerEntryError,
    DegenerateTransactionError,
    NonPositiveAmountError,
    TransactionKindLedgerMismatchError,
    UnbalancedTransactionError,
)


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


class LedgerType(str, enum.Enum):
    """Линия раздела двух касс. Проводка целиком в одной из них."""

    OPERATIONS = "operations"
    PRIZE = "prize"


class EntryDirection(str, enum.Enum):
    """Сторона ноги двойной записи."""

    DEBIT = "debit"
    CREDIT = "credit"


class TransactionKind(str, enum.Enum):
    """Вид проводки. Каждый вид жёстко закреплён за своей кассой."""

    SUBSCRIPTION_PAYMENT = "subscription_payment"
    B2B_INVOICE = "b2b_invoice"
    PROVIDER_FEE = "provider_fee"
    REFUND = "refund"
    SPONSOR_DEPOSIT = "sponsor_deposit"
    PRIZE_PAYOUT = "prize_payout"
    PRIZE_TAX = "prize_tax"


# Жёсткая привязка вида проводки к кассе — доменное зеркало раздельного учёта.
_KIND_LEDGER: dict[TransactionKind, LedgerType] = {
    TransactionKind.SUBSCRIPTION_PAYMENT: LedgerType.OPERATIONS,
    TransactionKind.B2B_INVOICE: LedgerType.OPERATIONS,
    TransactionKind.PROVIDER_FEE: LedgerType.OPERATIONS,
    TransactionKind.REFUND: LedgerType.OPERATIONS,
    TransactionKind.SPONSOR_DEPOSIT: LedgerType.PRIZE,
    TransactionKind.PRIZE_PAYOUT: LedgerType.PRIZE,
    TransactionKind.PRIZE_TAX: LedgerType.PRIZE,
}


def ledger_of_kind(kind: TransactionKind) -> LedgerType:
    """Касса, которой принадлежит данный вид проводки."""
    return _KIND_LEDGER[kind]


@dataclass(slots=True)
class LedgerAccount:
    """Счёт плана счетов. ``ledger_type`` приписывает счёт к одной кассе."""

    ledger_type: LedgerType
    account_code: str
    title: str
    currency: str = "RUB"
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Нога двойной записи (append-only). Сумма всегда строго положительна."""

    account_id: uuid.UUID
    direction: EntryDirection
    amount_kopecks: int
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if self.amount_kopecks <= 0:
            raise NonPositiveAmountError(
                f"Сумма ноги должна быть > 0, получено {self.amount_kopecks}"
            )


@dataclass(frozen=True, slots=True)
class PostingLeg:
    """Описание ноги до сборки транзакции: счёт целиком (с его кассой)."""

    account: LedgerAccount
    direction: EntryDirection
    amount_kopecks: int


@dataclass(slots=True)
class LedgerTransaction:
    """Транзакция как единица проводки. ``ledger_type`` фиксирует кассу.

    Создаётся только через :meth:`post`, который проверяет инварианты двойной
    записи и раздельности касс. После создания не мутируется (append-only).
    """

    ledger_type: LedgerType
    kind: TransactionKind
    entries: tuple[LedgerEntry, ...]
    external_ref: str | None = None
    description: str = ""
    created_at: datetime = field(default_factory=_utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @classmethod
    def post(
        cls,
        *,
        kind: TransactionKind,
        legs: tuple[PostingLeg, ...],
        external_ref: str | None = None,
        description: str = "",
        now: datetime | None = None,
    ) -> LedgerTransaction:
        """Собирает сбалансированную транзакцию одной кассы.

        Проверяет: ≥2 ног; каждая сумма > 0; сумма дебетов = сумма кредитов;
        все счета принадлежат кассе вида проводки (никакого перетекания между
        кассами). Бросает доменную ошибку при нарушении любого инварианта.
        """
        ledger_type = ledger_of_kind(kind)

        if len(legs) < 2:
            raise DegenerateTransactionError(
                "Двойная запись требует минимум двух ног"
            )

        debit = 0
        credit = 0
        for leg in legs:
            if leg.amount_kopecks <= 0:
                raise NonPositiveAmountError(
                    f"Сумма ноги должна быть > 0, получено {leg.amount_kopecks}"
                )
            if leg.account.ledger_type is not ledger_type:
                raise CrossLedgerEntryError(
                    f"Счёт {leg.account.account_code} принадлежит кассе "
                    f"{leg.account.ledger_type.value}, а проводка — "
                    f"{ledger_type.value}: перетекание между кассами запрещено"
                )
            if leg.direction is EntryDirection.DEBIT:
                debit += leg.amount_kopecks
            else:
                credit += leg.amount_kopecks

        if debit != credit:
            raise UnbalancedTransactionError(
                f"Дебет ({debit}) ≠ кредит ({credit}) в копейках"
            )

        # Перепроверка соответствия вида и кассы (на случай ручной подмены).
        if ledger_of_kind(kind) is not ledger_type:  # pragma: no cover - инвариант
            raise TransactionKindLedgerMismatchError(kind.value)

        entries = tuple(
            LedgerEntry(
                account_id=leg.account.id,
                direction=leg.direction,
                amount_kopecks=leg.amount_kopecks,
            )
            for leg in legs
        )
        return cls(
            ledger_type=ledger_type,
            kind=kind,
            entries=entries,
            external_ref=external_ref,
            description=description,
            created_at=now or _utcnow(),
        )

    def total(self) -> int:
        """Оборот транзакции в копейках (сумма дебетовых ног)."""
        return sum(
            e.amount_kopecks for e in self.entries if e.direction is EntryDirection.DEBIT
        )
