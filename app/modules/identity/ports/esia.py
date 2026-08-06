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

    def build_authorization_url(
        self, *, state: str, code_verifier: str, nonce: str
    ) -> str:
        """Формирует URL страницы авторизации ЕСИА с подписанными параметрами.

        ``code_verifier`` наружу не уходит: адаптер кладёт в URL только его
        S256-производную (``code_challenge``) — вычисление хеша это крипто и
        живёт в адаптере, прикладной слой лишь генерирует случайный секрет.
        """
        ...

    async def exchange_code(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> EsiaTokens:
        """Меняет authorization code на маркеры.

        Адаптер предъявляет ``code_verifier`` (PKCE) и проверяет ``id_token``
        (подпись по JWKS, ``iss``/``aud``/``exp`` и совпадение ``nonce``).
        """
        ...

    async def fetch_identity(self, tokens: EsiaTokens) -> EsiaIdentity:
        """Запрашивает атрибуты гражданина (СНИЛС, ФИО, уровень УЗ) по маркерам.

        Адаптер сверяет субъекта атрибутов с ``sub`` проверенного ``id_token``
        (``EsiaTokens.subject``), если проверка маркера включена.
        """
        ...
