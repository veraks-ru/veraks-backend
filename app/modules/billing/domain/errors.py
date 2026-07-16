"""Доменные ошибки billing.

Все наследуются от :class:`BillingError`; маппинг в HTTP-статусы — централизованно
в ``app/main.py`` (а не в роутерах). Слой домена не знает о транспорте.
"""

from __future__ import annotations


class BillingError(Exception):
    """Базовая ошибка домена billing."""


# ── Леджер: инварианты двойной записи и раздельности касс ─────────────────


class LedgerError(BillingError):
    """Базовая ошибка журнала проводок."""


class UnbalancedTransactionError(LedgerError):
    """Сумма дебетов не равна сумме кредитов внутри транзакции."""


class DegenerateTransactionError(LedgerError):
    """В проводке меньше двух ног — двойная запись невозможна."""


class NonPositiveAmountError(LedgerError):
    """Сумма ноги должна быть строго положительной (копейки > 0)."""


class CrossLedgerEntryError(LedgerError):
    """Нога указывает на счёт чужой кассы — перетекание между кассами запрещено.

    Это доменное зеркало схемного триггера ``enforce_ledger_separation``:
    операционка и призовой фонд не смешиваются ни на одном уровне.
    """


class TransactionKindLedgerMismatchError(LedgerError):
    """Вид проводки не принадлежит кассе, в которой её пытаются провести."""


class LedgerAccountNotFoundError(LedgerError):
    """Счёт плана счетов не найден."""


# ── Подписки и платежи (операционная касса) ───────────────────────────────


class SubscriptionNotFoundError(BillingError):
    """Подписка не найдена."""


class SubscriptionPermissionError(BillingError):
    """Недостаточно прав на операцию с подпиской."""


class PaymentNotFoundError(BillingError):
    """Платёж не найден."""


class InvalidPaymentError(BillingError):
    """Некорректные данные платежа (сумма, валюта, назначение)."""


class PaymentGatewayError(BillingError):
    """Ошибка внешнего платёжного шлюза (сеть/отказ провайдера при Init/Cancel)."""


# ── Призовой фонд и выплаты (призовая касса) ──────────────────────────────


class PrizeFundNotFoundError(BillingError):
    """Призовой фонд не найден."""


class SeasonNotFoundError(BillingError):
    """Сезон (по slug) для прозрачности фонда не найден."""


class PayoutNotFoundError(BillingError):
    """Выплата не найдена."""


class BillingPermissionError(BillingError):
    """Недостаточно прав (RBAC) на финансовую операцию."""


class InvalidAmountError(BillingError):
    """Некорректная денежная сумма (отрицательная, налог больше суммы и т.п.)."""


class PayoutAlreadyDecidedError(BillingError):
    """Выплата уже прошла подтверждение/отклонение — повторное решение запрещено."""


class SelfApprovalError(BillingError):
    """Нарушение maker-checker: подтверждающий совпадает с инициатором выплаты."""


class InsufficientPrizeFundError(BillingError):
    """В фонде недостаточно средств для выплаты."""


class InvalidRequisiteError(BillingError):
    """Некорректные реквизиты выплаты (телефон СБП, банк, ФИО)."""


class PayoutRequisitesMissingError(BillingError):
    """У получателя выплаты не заполнены реквизиты — отправка невозможна."""
