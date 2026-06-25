"""Адаптер шлюза ЕСИА (OIDC authorization code flow).

Интеграция идёт через сертифицированного интегратора/шлюз, который берёт на
себя ГОСТ-криптографию (подпись ``client_secret`` по ГОСТ Р 34.10-2012,
проверку подписи ``id_token``) и аттестацию СКЗИ КС3/ФСБ. Наш код общается
со шлюзом по обычному HTTPS+JSON.

TODO(identity-infra): согласовать со шлюзом точный формат ответа
``/userinfo`` (структуру атрибутов СНИЛС/ФИО и поля уровня учётной записи) —
маппинг в :class:`EsiaIdentity` ниже опирается на наиболее типовую форму и
должен быть выверен по документации конкретного интегратора.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import EsiaSettings
from app.modules.identity.domain.errors import EsiaExchangeError
from app.modules.identity.domain.value_objects import EsiaIdentity, EsiaTokens, Snils

# Уровни учётной записи ЕСИА, считающиеся «подтверждёнными».
_TRUSTED_LEVELS = {"CONFIRMED", "AAL2", "AAL3", "P3", "P2"}


class EsiaOidcGateway:
    """HTTP-клиент к шлюзу ЕСИА."""

    def __init__(self, settings: EsiaSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    def build_authorization_url(self, *, state: str) -> str:
        """Собирает URL страницы авторизации ЕСИА.

        Подпись ``client_secret`` по ГОСТ выполняет шлюз; здесь передаём
        исходные параметры запроса.
        """
        params = {
            "client_id": self._settings.client_id,
            "redirect_uri": self._settings.redirect_uri,
            "scope": " ".join(self._settings.scope_list),
            "response_type": "code",
            "state": state,
            "access_type": "online",
        }
        return f"{self._settings.authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(self, *, code: str) -> EsiaTokens:
        """Меняет authorization code на маркеры доступа."""
        data = {
            "client_id": self._settings.client_id,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self._settings.redirect_uri,
            "scope": " ".join(self._settings.scope_list),
        }
        try:
            resp = await self._client.post(self._settings.token_endpoint, data=data)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EsiaExchangeError(f"Сбой обмена кода ЕСИА: {exc}") from exc

        access = payload.get("access_token")
        if not access:
            raise EsiaExchangeError("В ответе ЕСИА отсутствует access_token")
        return EsiaTokens(
            access_token=access,
            id_token=payload.get("id_token"),
            expires_in=payload.get("expires_in"),
        )

    async def fetch_identity(self, tokens: EsiaTokens) -> EsiaIdentity:
        """Запрашивает атрибуты гражданина по access-токену."""
        headers = {"Authorization": f"Bearer {tokens.access_token}"}
        try:
            resp = await self._client.get(
                self._settings.userinfo_endpoint, headers=headers
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EsiaExchangeError(f"Сбой получения атрибутов ЕСИА: {exc}") from exc
        return self._map_identity(payload)

    @staticmethod
    def _map_identity(payload: dict[str, Any]) -> EsiaIdentity:
        """Маппит ответ шлюза в доменный :class:`EsiaIdentity`."""
        oid = str(payload.get("oid") or payload.get("sub") or "").strip()
        snils_raw = payload.get("snils") or payload.get("snils_number")
        if not oid or not snils_raw:
            raise EsiaExchangeError("В ответе ЕСИА нет oid/СНИЛС")

        level = str(payload.get("trusted") or payload.get("acr") or "").upper()
        trusted = (
            payload.get("trusted") is True
            or level in _TRUSTED_LEVELS
            or str(payload.get("verifying")).lower() == "true"
        )
        try:
            snils = Snils.parse(str(snils_raw))
        except Exception as exc:  # InvalidSnilsError → проблема обмена
            raise EsiaExchangeError(f"Некорректный СНИЛС из ЕСИА: {exc}") from exc

        return EsiaIdentity(
            oid=oid,
            snils=snils,
            first_name=str(payload.get("firstName") or payload.get("given_name") or ""),
            last_name=str(payload.get("lastName") or payload.get("family_name") or ""),
            middle_name=(payload.get("middleName") or payload.get("patronymic")),
            trusted=trusted,
        )
