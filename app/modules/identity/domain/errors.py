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


class InvalidTokenError(IdentityError):
    """Сессионный токен (JWT) недействителен или просрочен."""


class UserNotFoundError(IdentityError):
    """Запрошенный пользователь не найден."""
