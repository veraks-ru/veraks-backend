"""Доменные исключения identity.

Все ошибки наследуются от ``IdentityError`` — это позволяет API-слою
единообразно маппить их в HTTP-ответы, не завязываясь на конкретику.
"""

from __future__ import annotations


class IdentityError(Exception):
    """Базовая ошибка домена identity."""


class InvalidSnilsError(IdentityError):
    """СНИЛС не прошёл валидацию формата/контрольной суммы."""


class UnconfirmedEsiaAccountError(IdentityError):
    """Учётная запись ЕСИА не «подтверждённая» — вход запрещён."""


class AccountDeletedError(IdentityError):
    """Аккаунт удалён (надгробие по snils_hash); повторная регистрация запрещена."""


class AccountSuspendedError(IdentityError):
    """Аккаунт заблокирован (suspended) — доступ запрещён."""


class InvalidStateError(IdentityError):
    """OIDC-параметр ``state`` не найден/просрочен — возможна CSRF-атака."""


class EsiaExchangeError(IdentityError):
    """Сбой обмена кодом авторизации или получения атрибутов в ЕСИА."""


class InvalidIdTokenError(EsiaExchangeError):
    """``id_token`` ЕСИА не прошёл проверку (подпись, iss/aud/exp или nonce).

    Наследуется от ``EsiaExchangeError``: с точки зрения клиента это сбой
    вышестоящего шлюза (502), а не ошибка пользователя — он ничего не мог
    сделать иначе.
    """


class EsiaAuthorizationDeniedError(IdentityError):
    """Пользователь не дал согласие/прервал вход на стороне Госуслуг.

    Соответствует OIDC-ошибкам ``access_denied``/``consent_required``/
    ``login_required``/``interaction_required`` в callback'е. Это не сбой:
    фронт показывает «Вход отменён» и предлагает повторить.
    """


class InvalidTokenError(IdentityError):
    """Сессионный токен (JWT) недействителен или просрочен."""


class InvalidMagicLinkError(IdentityError):
    """Ссылка входа по email не найдена, просрочена или уже использована.

    Все три случая намеренно неразличимы снаружи (один текст, один код 401):
    иначе по ответу можно было бы выяснить, существовал ли токен вообще.
    """


class EmailAlreadyTakenError(IdentityError):
    """Адрес уже привязан к другому аккаунту (частичный ``UNIQUE(email)``).

    Доменная обёртка над ``ports.repositories.EmailAlreadyExistsError`` — та
    не наследуется от ``IdentityError`` и до HTTP-слоя как есть не долетает
    (во входе по ссылке она перехватывается как гонка регистраций, а не
    отдаётся клиенту).
    """


class AuthProviderDisabledError(IdentityError):
    """Способ входа выключен в этой инсталляции (``AUTH_PROVIDERS``).

    В HTTP — 404, а не 503: 503 обещает «сервис временно недоступен, зайдите
    позже», тогда как выключенный провайдер — это не сбой и не временное
    состояние, а отсутствие такого способа входа в конкретной установке.
    404 к тому же ничего не рассказывает о конфигурации сервера.
    """


class UserNotFoundError(IdentityError):
    """Запрошенный пользователь не найден."""


class UsernameAlreadyTakenError(IdentityError):
    """Публичный хэндл (username) занят другим аккаунтом.

    Доменная обёртка над ``ports.repositories.UsernameTakenError``
    (нарушением ``UNIQUE(username)``) — та не наследуется от ``IdentityError``
    и не долетает до HTTP-слоя как есть (в логине она перехватывается для
    переаллокации хэндла, а не отдаётся клиенту).
    """


class IncompleteConsentsError(IdentityError):
    """При онбординге переданы не все обязательные согласия текущих версий."""


class ConsentRequiredError(IdentityError):
    """Действие требует завершённого онбординга (принятых актуальных согласий).

    Юридическая рамка (PRD §7): платформа — публичный конкурс (гл. 57 ГК РФ),
    участие в котором возможно только после акцепта оферты и согласия на
    обработку ПДн (152-ФЗ). Клиентский гард (редирект на ``/onboarding``) —
    это UX, а не гарантия: прямой вызов API должен получать отказ. Поднимается
    гардом ``require_onboarded_user``; в HTTP — 403.
    """


class CannotSuspendSelfError(IdentityError):
    """Нельзя заблокировать самого себя (иначе некому будет снять блокировку)."""


class CannotSuspendAdminError(IdentityError):
    """Нельзя заблокировать другого администратора рядовой модерацией."""


class InvalidUserStatusError(IdentityError):
    """Переход статуса аккаунта невозможен из текущего состояния."""
