"""Криптография OIDC-потока ЕСИА: PKCE и проверка ``id_token``.

Здесь живёт вся крипта шага аутентификации — домен и прикладной слой о JWT,
JWKS и хешах не знают (они лишь генерируют случайные секреты и передают их
через порт ``EsiaGateway``).

Что проверяется в ``id_token``:

* подпись — публичным ключом из JWKS шлюза (``ESIA_JWKS_URL``), ключ ищется
  по ``kid`` заголовка;
* ``iss`` — совпадает с ``ESIA_ISSUER``;
* ``aud`` — совпадает с нашим ``client_id`` (маркер выписан именно нам);
* ``exp`` — не истёк (с допуском на расхождение часов);
* ``nonce`` — совпадает с тем, что мы положили в запрос авторизации
  (маркер относится к ЭТОМУ входу, а не воспроизведён из другого).

Если ``ESIA_JWKS_URL`` не задан, работает режим «доверие каналу»: маркер
принимается без проверки. Это допустимо только локально с моком — вне
``app_env=local`` пустой ``ESIA_JWKS_URL`` не даёт приложению стартовать
(см. ``Settings._require_esia_id_token_verification``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWK, PyJWKSet
from jwt.exceptions import (
    InvalidSignatureError,
    PyJWKError,
    PyJWKSetError,
    PyJWTError,
)

from app.config import EsiaSettings
from app.modules.identity.domain.errors import EsiaExchangeError, InvalidIdTokenError

_LOG = logging.getLogger(__name__)

# Допуск на расхождение часов при проверке ``exp``/``iat`` (секунды).
_CLOCK_SKEW_SECONDS = 60


def pkce_code_challenge(code_verifier: str) -> str:
    """Считает ``code_challenge`` по методу S256 (RFC 7636 §4.2).

    ``BASE64URL(SHA256(ASCII(code_verifier)))`` без выравнивающих ``=``.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class EsiaIdTokenVerifier:
    """Проверка ``id_token`` с кэшем ключей JWKS в памяти процесса.

    Кэш живёт ``jwks_cache_ttl_seconds`` (по умолчанию час), чтобы не ходить
    к шлюзу на каждый вход. Ротацию ключа «вне расписания» ловим двумя
    путями, до того как маркер будет отвергнут:

    * неизвестный ``kid`` — принудительное обновление JWKS;
    * ``InvalidSignatureError`` при знакомом ``kid`` — шлюз мог подменить
      ключ, не меняя идентификатор: одно обновление и ОДИН повтор проверки
      (не цикл, иначе подделанная подпись гоняла бы нас к шлюзу).

    Если обновление не удалось, а прежние ключи ещё есть — работаем на них
    (сбой JWKS-эндпоинта не должен ронять вход), но пишем предупреждение.

    Экземпляр общий на процесс (см. ``identity.api.dependencies``), поэтому
    гонку параллельных обновлений закрываем ``asyncio.Lock``.
    """

    def __init__(self, settings: EsiaSettings) -> None:
        self._settings = settings
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def verify(
        self, id_token: str | None, *, nonce: str, client: httpx.AsyncClient
    ) -> dict[str, Any]:
        """Проверяет маркер и возвращает его claims (пустой dict без JWKS)."""
        if not self._settings.verify_id_token:
            # Режим «доверие каналу» (только local): проверять нечем.
            return {}
        if not id_token:
            raise InvalidIdTokenError("Шлюз ЕСИА не вернул id_token")

        try:
            header = jwt.get_unverified_header(id_token)
        except PyJWTError as exc:
            raise InvalidIdTokenError(f"Некорректный заголовок id_token: {exc}") from exc

        kid = header.get("kid")
        key = await self._resolve_key(kid, client)
        try:
            claims = self._decode(id_token, key)
        except InvalidSignatureError:
            # Ключ мог быть заменён БЕЗ смены kid (путь «незнакомый kid» такую
            # ротацию не ловит). Один принудительный перечит JWKS и повтор —
            # ровно один, чтобы подделка не гоняла нас к шлюзу по кругу.
            await self._refresh(client, force=True)
            rotated = self._lookup(kid)
            if rotated is None:
                raise InvalidIdTokenError(
                    f"В JWKS ЕСИА нет ключа подписи id_token (kid={kid!r})"
                ) from None
            try:
                claims = self._decode(id_token, rotated)
            except PyJWTError as exc:
                raise InvalidIdTokenError(
                    f"id_token не прошёл проверку: {exc}"
                ) from exc
        except PyJWTError as exc:
            raise InvalidIdTokenError(f"id_token не прошёл проверку: {exc}") from exc

        if claims.get("nonce") != nonce:
            # Маркер валиден сам по себе, но относится к другому входу —
            # признак воспроизведения (replay).
            raise InvalidIdTokenError("nonce в id_token не совпадает с запросом")
        return claims

    def _decode(self, id_token: str, key: PyJWK) -> dict[str, Any]:
        """Проверяет подпись и обязательные claims (кроме ``nonce``)."""
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key=key,
            algorithms=self._settings.id_token_algorithm_list,
            audience=self._settings.client_id,
            issuer=self._settings.issuer,
            leeway=_CLOCK_SKEW_SECONDS,
            # "sub" обязателен по OIDC Core (§2): без него esia_gateway.py
            # молча пропускает сверку субъекта с ответом /userinfo, и
            # привязка личности к проверенному id_token отключается тихо,
            # без ошибки (фикс-раунд ревью T12).
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
        return claims

    async def _resolve_key(self, kid: str | None, client: httpx.AsyncClient) -> PyJWK:
        """Находит ключ подписи по ``kid``, при необходимости обновив JWKS."""
        if self._is_stale():
            await self._refresh(client, force=False)
        key = self._lookup(kid)
        if key is None:
            # Возможна ротация ключей шлюзом — пробуем обновиться вне расписания.
            await self._refresh(client, force=True)
            key = self._lookup(kid)
        if key is None:
            raise InvalidIdTokenError(
                f"В JWKS ЕСИА нет ключа подписи id_token (kid={kid!r})"
            )
        return key

    def _lookup(self, kid: str | None) -> PyJWK | None:
        """Ключ по ``kid``; при единственном ключе допускаем маркер без ``kid``."""
        if kid is not None:
            return self._keys.get(kid)
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    def _is_stale(self) -> bool:
        """Пора ли перечитывать JWKS (кэш пуст или протух)."""
        if not self._keys:
            return True
        age = time.monotonic() - self._fetched_at
        return age >= self._settings.jwks_cache_ttl_seconds

    async def _refresh(self, client: httpx.AsyncClient, *, force: bool) -> None:
        """Перечитывает JWKS (под общей блокировкой, без «стада» запросов)."""
        async with self._lock:
            # Пока ждали блокировку, соседняя корутина могла уже обновить кэш.
            if not force and not self._is_stale():
                return
            try:
                resp = await client.get(self._settings.jwks_url)
                resp.raise_for_status()
                keys = self._parse(resp.json())
            except (httpx.HTTPError, ValueError, PyJWKSetError, PyJWKError) as exc:
                if self._keys:
                    _LOG.warning(
                        "Не удалось обновить JWKS ЕСИА (%s); используем прежние ключи",
                        exc,
                    )
                    return
                raise EsiaExchangeError(f"Сбой получения JWKS ЕСИА: {exc}") from exc
            self._keys = keys
            self._fetched_at = time.monotonic()

    @staticmethod
    def _parse(payload: Any) -> dict[str, PyJWK]:
        """Разбирает ответ JWKS в карту ``kid -> ключ``."""
        jwk_set = PyJWKSet.from_dict(payload)
        keys = {key.key_id: key for key in jwk_set.keys if key.key_id}
        if not keys:
            # Единственный ключ без ``kid`` — допустимо (см. ``_lookup``).
            only = jwk_set.keys[:1]
            keys = {"": only[0]} if only else {}
        return keys
