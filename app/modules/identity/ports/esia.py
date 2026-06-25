"""Порт шлюза ЕСИА.

Скрывает за собой OIDC-обмен и REST-запросы атрибутов. Реальная реализация
ходит к сертифицированному интегратору (ГОСТ-крипто на его стороне);
в тестах подставляется фейк, возвращающий заранее заданную ``EsiaIdentity``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.identity.domain.value_objects import EsiaIdentity, EsiaTokens


@runtime_checkable
class EsiaGateway(Protocol):
    """Интеграция с ЕСИА по authorization code flow."""

    def build_authorization_url(self, *, state: str) -> str:
        """Формирует URL страницы авторизации ЕСИА с подписанными параметрами."""
        ...

    async def exchange_code(self, *, code: str) -> EsiaTokens:
        """Меняет authorization code на маркеры (с проверкой подписи id_token)."""
        ...

    async def fetch_identity(self, tokens: EsiaTokens) -> EsiaIdentity:
        """Запрашивает атрибуты гражданина (СНИЛС, ФИО, уровень УЗ) по маркерам."""
        ...
