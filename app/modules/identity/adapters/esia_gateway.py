"""Адаптер шлюза ЕСИА (OIDC authorization code flow).

Интеграция идёт через сертифицированного интегратора/шлюз, который берёт на
себя ГОСТ-криптографию (подпись ``client_secret`` по ГОСТ Р 34.10-2012) и
аттестацию СКЗИ КС3/ФСБ. Наш код общается со шлюзом по обычному HTTPS+JSON.

Поток защищён двумя механизмами сверх одноразового ``state``:

* **PKCE (S256, RFC 7636)** — в запрос авторизации уходит ``code_challenge``,
  в запрос маркеров — ``code_verifier``. Перехваченный authorization code
  (логи прокси, история браузера, вредоносное приложение на redirect_uri)
  без ``code_verifier`` бесполезен.
* **nonce + проверка ``id_token``** — маркер проверяется по JWKS шлюза
  (см. ``adapters/id_token.py``), в т.ч. на совпадение ``nonce``: чужой или
  повторно предъявленный маркер не проходит.

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
from app.modules.identity.adapters.id_token import (
    EsiaIdTokenVerifier,
    pkce_code_challenge,
)
from app.modules.identity.domain.errors import EsiaExchangeError
from app.modules.identity.domain.value_objects import EsiaIdentity, EsiaTokens, Snils

# Уровни учётной записи ЕСИА, считающиеся «подтверждёнными».
_TRUSTED_LEVELS = {"CONFIRMED", "AAL2", "AAL3", "P3", "P2"}


class EsiaOidcGateway:
    """HTTP-клиент к шлюзу ЕСИА."""

    def __init__(
        self,
        settings: EsiaSettings,
        client: httpx.AsyncClient,
        id_token_verifier: EsiaIdTokenVerifier,
    ) -> None:
        self._settings = settings
        self._client = client
        self._id_token_verifier = id_token_verifier

    def build_authorization_url(
        self, *, state: str, code_verifier: str, nonce: str
    ) -> str:
        """Собирает URL страницы авторизации ЕСИА.

        Подпись ``client_secret`` по ГОСТ выполняет шлюз; здесь передаём
        исходные параметры запроса. Наружу уходит не сам ``code_verifier``,
        а его S256-хеш (``code_challenge``).
        """
        params = {
            "client_id": self._settings.client_id,
            "redirect_uri": self._settings.redirect_uri,
            "scope": " ".join(self._settings.scope_list),
            "response_type": "code",
            "state": state,
            "access_type": "online",
            "code_challenge": pkce_code_challenge(code_verifier),
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
        return f"{self._settings.authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> EsiaTokens:
        """Меняет authorization code на маркеры и проверяет ``id_token``."""
        data = {
            "client_id": self._settings.client_id,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self._settings.redirect_uri,
            "scope": " ".join(self._settings.scope_list),
            "code_verifier": code_verifier,
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
        id_token = payload.get("id_token")
        # Проверяем ДО обращения к /userinfo: невалидный маркер = не наш вход.
        await self._id_token_verifier.verify(
            id_token, nonce=nonce, client=self._client
        )
        return EsiaTokens(
            access_token=access,
            id_token=id_token,
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
