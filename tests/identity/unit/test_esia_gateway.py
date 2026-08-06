"""Юнит-тесты адаптера шлюза ЕСИА: PKCE и проверка id_token в обмене кода.

Сеть подменяется ``httpx.MockTransport``: проверяем ровно то, что адаптер
кладёт в запросы и как реагирует на ответы шлюза.
"""

from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import EsiaSettings
from app.modules.identity.adapters.esia_gateway import EsiaOidcGateway
from app.modules.identity.adapters.id_token import (
    EsiaIdTokenVerifier,
    pkce_code_challenge,
)
from app.modules.identity.domain.errors import InvalidIdTokenError

_ISSUER = "https://esia-gateway.test"
_CLIENT_ID = "orakul-test"
_KID = "key-1"
_VERIFIER = "verifier-0123456789-0123456789-0123456789"
_NONCE = "nonce-1"


def _settings(**overrides: Any) -> EsiaSettings:
    base: dict[str, Any] = {
        "client_id": _CLIENT_ID,
        "redirect_uri": "https://orakul.test/auth/esia/callback",
        "authorization_endpoint": f"{_ISSUER}/authorize",
        "token_endpoint": f"{_ISSUER}/token",
        "userinfo_endpoint": f"{_ISSUER}/userinfo",
        "issuer": _ISSUER,
        "jwks_url": f"{_ISSUER}/jwks",
    }
    base.update(overrides)
    return EsiaSettings(**base)


@pytest.fixture
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key: rsa.RSAPrivateKey) -> dict[str, Any]:
    """JWKS с публичной частью ключа (как его отдаёт шлюз)."""
    numbers = key.public_key().public_numbers()

    def b64u(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": _KID,
                "n": b64u(numbers.n),
                "e": b64u(numbers.e),
            }
        ]
    }


def _id_token(key: rsa.RSAPrivateKey, *, nonce: str = _NONCE) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": _ISSUER,
            "sub": "esia-oid-1",
            "aud": _CLIENT_ID,
            "iat": now - 1,
            "exp": now + 300,
            "nonce": nonce,
        },
        key,
        algorithm="RS256",
        headers={"kid": _KID},
    )


class _Gateway:
    """Фейковый шлюз: /token отдаёт заданный id_token, /jwks — ключи."""

    def __init__(
        self,
        key: rsa.RSAPrivateKey,
        *,
        id_token: str | None,
        userinfo_oid: str = "esia-oid-1",
    ) -> None:
        self.key = key
        self.id_token = id_token
        self.userinfo_oid = userinfo_oid
        self.token_form: dict[str, list[str]] = {}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jwks":
            return httpx.Response(200, json=_jwks(self.key))
        if request.url.path == "/userinfo":
            return httpx.Response(
                200,
                json={
                    "oid": self.userinfo_oid,
                    "snils": "112-233-445 95",
                    "firstName": "Иван",
                    "lastName": "Петров",
                    "trusted": True,
                },
            )
        self.token_form = parse_qs(request.content.decode())
        body: dict[str, Any] = {"access_token": "access-1", "expires_in": 3600}
        if self.id_token is not None:
            body["id_token"] = self.id_token
        return httpx.Response(200, json=body)


def test_authorization_url_carries_s256_challenge_not_verifier() -> None:
    """В URL уходит только хеш секрета: перехват ссылки не даёт code_verifier."""
    gateway = EsiaOidcGateway(
        _settings(), httpx.AsyncClient(), EsiaIdTokenVerifier(_settings())
    )

    url = gateway.build_authorization_url(
        state="state-1", code_verifier=_VERIFIER, nonce=_NONCE
    )

    query = parse_qs(urlparse(url).query)
    assert query["code_challenge"] == [pkce_code_challenge(_VERIFIER)]
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"] == [_NONCE]
    assert query["state"] == ["state-1"]
    assert _VERIFIER not in url


async def test_exchange_sends_code_verifier_and_accepts_valid_id_token(
    signing_key,
) -> None:
    server = _Gateway(signing_key, id_token=_id_token(signing_key))
    settings = _settings()

    async with server.client() as client:
        gateway = EsiaOidcGateway(settings, client, EsiaIdTokenVerifier(settings))
        tokens = await gateway.exchange_code(
            code="code-1", code_verifier=_VERIFIER, nonce=_NONCE
        )

    assert tokens.access_token == "access-1"
    assert server.token_form["code_verifier"] == [_VERIFIER]
    assert server.token_form["code"] == ["code-1"]


async def test_identity_is_bound_to_verified_subject(signing_key) -> None:
    """Атрибуты /userinfo принадлежат тому же субъекту, что и проверенный маркер."""
    server = _Gateway(signing_key, id_token=_id_token(signing_key))
    settings = _settings()

    async with server.client() as client:
        gateway = EsiaOidcGateway(settings, client, EsiaIdTokenVerifier(settings))
        tokens = await gateway.exchange_code(
            code="code-1", code_verifier=_VERIFIER, nonce=_NONCE
        )
        identity = await gateway.fetch_identity(tokens)

    assert tokens.subject == "esia-oid-1"
    assert identity.oid == "esia-oid-1"


async def test_identity_of_another_subject_rejected(signing_key) -> None:
    """Подменённый ответ /userinfo (другой гражданин) — отказ, а не чужой аккаунт."""
    server = _Gateway(
        signing_key, id_token=_id_token(signing_key), userinfo_oid="esia-oid-999"
    )
    settings = _settings()

    async with server.client() as client:
        gateway = EsiaOidcGateway(settings, client, EsiaIdTokenVerifier(settings))
        tokens = await gateway.exchange_code(
            code="code-1", code_verifier=_VERIFIER, nonce=_NONCE
        )
        with pytest.raises(InvalidIdTokenError, match="субъект"):
            await gateway.fetch_identity(tokens)


async def test_identity_not_bound_in_trust_channel_mode(signing_key) -> None:
    """Без проверки маркера сверять не с чем — поведение как раньше."""
    server = _Gateway(
        signing_key, id_token="совсем-не-jwt", userinfo_oid="esia-oid-999"
    )
    settings = _settings(jwks_url="", issuer="")

    async with server.client() as client:
        gateway = EsiaOidcGateway(settings, client, EsiaIdTokenVerifier(settings))
        tokens = await gateway.exchange_code(
            code="code-1", code_verifier=_VERIFIER, nonce=_NONCE
        )
        identity = await gateway.fetch_identity(tokens)

    assert tokens.subject is None
    assert identity.oid == "esia-oid-999"


async def test_exchange_rejects_id_token_from_another_flow(signing_key) -> None:
    """id_token с чужим nonce не пропускается дальше обмена (replay)."""
    server = _Gateway(signing_key, id_token=_id_token(signing_key, nonce="другой"))
    settings = _settings()

    async with server.client() as client:
        gateway = EsiaOidcGateway(settings, client, EsiaIdTokenVerifier(settings))
        with pytest.raises(InvalidIdTokenError):
            await gateway.exchange_code(
                code="code-1", code_verifier=_VERIFIER, nonce=_NONCE
            )


async def test_exchange_requires_id_token_when_verification_enabled(
    signing_key,
) -> None:
    """Шлюз не вернул id_token, а JWKS настроен — обмен не считается успешным."""
    server = _Gateway(signing_key, id_token=None)
    settings = _settings()

    async with server.client() as client:
        gateway = EsiaOidcGateway(settings, client, EsiaIdTokenVerifier(settings))
        with pytest.raises(InvalidIdTokenError):
            await gateway.exchange_code(
                code="code-1", code_verifier=_VERIFIER, nonce=_NONCE
            )


async def test_exchange_in_trust_channel_mode_skips_id_token(signing_key) -> None:
    """Без ESIA_JWKS_URL (только local) обмен работает как раньше."""
    server = _Gateway(signing_key, id_token="совсем-не-jwt")
    settings = _settings(jwks_url="", issuer="")

    async with server.client() as client:
        gateway = EsiaOidcGateway(settings, client, EsiaIdTokenVerifier(settings))
        tokens = await gateway.exchange_code(
            code="code-1", code_verifier=_VERIFIER, nonce=_NONCE
        )

    assert tokens.access_token == "access-1"
    # PKCE предъявляется в любом режиме.
    assert server.token_form["code_verifier"] == [_VERIFIER]
