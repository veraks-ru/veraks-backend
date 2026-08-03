"""Доменные политики identity — чистые правила без I/O.

Здесь живёт ядро гарантии «один человек — один аккаунт»: проверки,
которые не зависят от способа хранения данных и легко юнит-тестируются.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.modules.identity.domain.consent import Consent, ConsentDocument
from app.modules.identity.domain.entities import User, UserStatus
from app.modules.identity.domain.errors import (
    AccountDeletedError,
    AccountSuspendedError,
    UnconfirmedEsiaAccountError,
)
from app.modules.identity.domain.value_objects import EsiaIdentity


def ensure_esia_confirmed(identity: EsiaIdentity, *, require_confirmed: bool) -> None:
    """Требует «подтверждённую» учётную запись ЕСИА.

    Упрощённая/стандартная учётки не дают надёжной привязки к гражданину,
    поэтому при ``require_confirmed`` они отклоняются.
    """
    if require_confirmed and not identity.trusted:
        raise UnconfirmedEsiaAccountError(
            "Требуется подтверждённая учётная запись ЕСИА"
        )


def ensure_account_can_authenticate(user: User) -> None:
    """Проверяет, что существующий аккаунт вправе войти.

    - ``deleted`` — надгробие по snils_hash: повторная регистрация/вход
      структурно запрещены (обойти удаление новой регистрацией нельзя);
    - ``suspended`` — доступ заблокирован модерацией.
    """
    if user.status is UserStatus.DELETED:
        raise AccountDeletedError("Аккаунт удалён; повторная регистрация запрещена")
    if user.status is UserStatus.SUSPENDED:
        raise AccountSuspendedError("Аккаунт заблокирован")


def missing_consents(
    required: Sequence[ConsentDocument], accepted: Iterable[Consent]
) -> list[ConsentDocument]:
    """Обязательные документы, для которых нет согласия на ТЕКУЩУЮ версию.

    Если пользователь принял более старую версию, а реестр (конфигурация)
    обновили, документ снова считается недостающим — так юрист, поменяв
    версию оферты/ПДн через env, заставляет всех пользователей заново
    подтвердить согласие при следующем входе.
    """
    accepted_list = list(accepted)
    return [
        doc
        for doc in required
        if not any(consent.satisfies(doc) for consent in accepted_list)
    ]
