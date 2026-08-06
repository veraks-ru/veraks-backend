"""Юнит-тесты криптографии OIDC-потока: PKCE и проверка ``id_token``.

Ключи RSA генерируются прямо здесь, JWKS отдаётся фейковым HTTP-транспортом —
это позволяет проверить именно нашу логику (какие claims обязательны, что
происходит при ротации ключа, работает ли кэш), не завися от сети.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import EsiaSettings
from app.modules.identity.adapters.id_token import (
    EsiaIdTokenVerifier,
    pkce_code_challenge,
)
from app.modules.identity.domain.errors import EsiaExchangeError, InvalidIdTokenError

_ISSUER = "https://esia-gateway.test"
_CLIENT_ID = "orakul-test"
_JWKS_URL = "https://esia-gateway.test/jwks"
_KID = "key-1"


def _settings(**overrides: Any) -> EsiaSettings:
    """Настройки ЕСИА с включённой проверкой id_token."""
    base: dict[str, Any] = {
        "client_id": _CLIENT_ID,
        "redirect_uri": "https://orakul.test/auth/esia/callback",
        "authorization_endpoint": "https://esia-gateway.test/authorize",
        "token_endpoint": "https://esia-gateway.test/token",
        "userinfo_endpoint": "https://esia-gateway.test/userinfo",
        "issuer": _ISSUER,
        "jwks_url": _JWKS_URL,
    }
    base.update(overrides)
    return EsiaSettings(**base)


def _b64u_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwks(key: rsa.RSAPrivateKey, *, kid: str = _KID) -> dict[str, Any]:
    """JWKS с публичной частью ключа (как его отдаёт шлюз)."""
    numbers = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64u_uint(numbers.n),
                "e": _b64u_uint(numbers.e),
            }
        ]
    }


def _id_token(
    key: rsa.RSAPrivateKey,
    *,
    kid: str = _KID,
    iss: str = _ISSUER,
    aud: str = _CLIENT_ID,
    nonce: str | None = "nonce-1",
    lifetime: int = 300,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "sub": "esia-oid-1",
        "aud": aud,
        "iat": now - 1,
        "exp": now + lifetime,
    }
    if nonce is not None:
        claims["nonce"] = nonce
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class _JwksServer:
    """Фейковый JWKS-эндпоинт: считает запросы, умеет менять ответ и падать."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0
        self.fail = False

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=self.payload)


@pytest.fixture
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# ── PKCE ──────────────────────────────────────────────────────────────────


def test_pkce_challenge_matches_rfc7636_vector() -> None:
    """Контрольный вектор RFC 7636 (Appendix B) — наш S256 совпадает с эталоном."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_challenge_is_url_safe_and_unpadded() -> None:
    """Challenge — base64url без '=' (иначе ломается в query-string)."""
    challenge = pkce_code_challenge("a" * 43)
    assert "=" not in challenge
    assert "+" not in challenge and "/" not in challenge


def test_pkce_challenge_differs_per_verifier() -> None:
    assert pkce_code_challenge("verifier-a") != pkce_code_challenge("verifier-b")


# ── Проверка id_token ─────────────────────────────────────────────────────


async def test_valid_id_token_passes(signing_key) -> None:
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        claims = await verifier.verify(
            _id_token(signing_key), nonce="nonce-1", client=client
        )

    assert claims["sub"] == "esia-oid-1"
    assert claims["nonce"] == "nonce-1"


async def test_forged_signature_rejected(signing_key) -> None:
    """Маркер подписан ЧУЖИМ ключом (тот же kid) — не проходит."""
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError):
            await verifier.verify(
                _id_token(attacker_key), nonce="nonce-1", client=client
            )


async def test_foreign_issuer_rejected(signing_key) -> None:
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError):
            await verifier.verify(
                _id_token(signing_key, iss="https://evil.example"),
                nonce="nonce-1",
                client=client,
            )


async def test_foreign_audience_rejected(signing_key) -> None:
    """Маркер выписан другому клиенту — не наш вход."""
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError):
            await verifier.verify(
                _id_token(signing_key, aud="another-client"),
                nonce="nonce-1",
                client=client,
            )


async def test_expired_token_rejected(signing_key) -> None:
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError):
            # Заведомо больше допуска на расхождение часов (60 секунд).
            await verifier.verify(
                _id_token(signing_key, lifetime=-600), nonce="nonce-1", client=client
            )


async def test_foreign_nonce_rejected(signing_key) -> None:
    """Валидный сам по себе маркер из ДРУГОГО входа (replay) — отказ."""
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError, match="nonce"):
            await verifier.verify(
                _id_token(signing_key, nonce="nonce-from-another-flow"),
                nonce="nonce-1",
                client=client,
            )


async def test_missing_nonce_claim_rejected(signing_key) -> None:
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError, match="nonce"):
            await verifier.verify(
                _id_token(signing_key, nonce=None), nonce="nonce-1", client=client
            )


async def test_missing_id_token_rejected() -> None:
    """Шлюз не вернул id_token, а проверка включена — вход не продолжается."""
    server = _JwksServer({"keys": []})
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError):
            await verifier.verify(None, nonce="nonce-1", client=client)


async def test_garbage_token_rejected() -> None:
    server = _JwksServer({"keys": []})
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError):
            await verifier.verify("не-jwt-вовсе", nonce="nonce-1", client=client)


async def test_trust_channel_mode_skips_verification() -> None:
    """Пустой ESIA_JWKS_URL (только local) — маркер не проверяется."""
    verifier = EsiaIdTokenVerifier(_settings(jwks_url="", issuer=""))
    server = _JwksServer({"keys": []})

    async with server.client() as client:
        assert await verifier.verify("что угодно", nonce="n", client=client) == {}
        assert await verifier.verify(None, nonce="n", client=client) == {}

    assert server.calls == 0  # за ключами даже не ходили


async def test_jwks_cached_between_verifications(signing_key) -> None:
    """Ключи читаются один раз на TTL — на каждый вход к шлюзу не ходим."""
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        await verifier.verify(_id_token(signing_key), nonce="nonce-1", client=client)
        await verifier.verify(_id_token(signing_key), nonce="nonce-1", client=client)

    assert server.calls == 1


async def test_unknown_kid_triggers_refresh(signing_key) -> None:
    """Ротация ключа шлюзом: незнакомый kid обновляет кэш, вход не ломается."""
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async with server.client() as client:
        await verifier.verify(_id_token(signing_key), nonce="nonce-1", client=client)
        server.payload = _jwks(rotated_key, kid="key-2")
        claims = await verifier.verify(
            _id_token(rotated_key, kid="key-2"), nonce="nonce-1", client=client
        )

    assert claims["sub"] == "esia-oid-1"
    assert server.calls == 2  # второй поход — из-за неизвестного kid


async def test_rotated_key_with_same_kid_triggers_one_retry(signing_key) -> None:
    """Шлюз подменил ключ, НЕ сменив kid: обновляем JWKS и повторяем проверку.

    Путь «незнакомый kid» такую ротацию не ловит — маркер отвергался бы с
    Signature verification failed до истечения кэша.
    """
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async with server.client() as client:
        await verifier.verify(_id_token(signing_key), nonce="nonce-1", client=client)
        server.payload = _jwks(rotated_key)  # тот же kid, другой ключ
        claims = await verifier.verify(
            _id_token(rotated_key), nonce="nonce-1", client=client
        )

    assert claims["sub"] == "esia-oid-1"
    assert server.calls == 2


async def test_forged_signature_retries_refresh_exactly_once(signing_key) -> None:
    """Подделка подписи не гоняет нас к JWKS по кругу: ровно один повтор."""
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async with server.client() as client:
        await verifier.verify(_id_token(signing_key), nonce="nonce-1", client=client)
        with pytest.raises(InvalidIdTokenError):
            await verifier.verify(
                _id_token(attacker_key), nonce="nonce-1", client=client
            )

    assert server.calls == 2  # первичное чтение + один принудительный refresh


async def test_unknown_kid_after_refresh_rejected(signing_key) -> None:
    server = _JwksServer(_jwks(signing_key))
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(InvalidIdTokenError, match="kid"):
            await verifier.verify(
                _id_token(signing_key, kid="unknown-kid"),
                nonce="nonce-1",
                client=client,
            )


async def test_unavailable_jwks_without_cache_is_gateway_error() -> None:
    """Первый вход при недоступном JWKS — 502-ошибка обмена, а не «плохой токен»."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = _JwksServer(_jwks(key))
    server.fail = True
    verifier = EsiaIdTokenVerifier(_settings())

    async with server.client() as client:
        with pytest.raises(EsiaExchangeError):
            await verifier.verify(_id_token(key), nonce="nonce-1", client=client)


async def test_stale_cache_survives_jwks_outage(signing_key) -> None:
    """Разовый сбой JWKS-эндпоинта не должен ронять вход: работаем на кэше."""
    server = _JwksServer(_jwks(signing_key))
    # TTL=1с, чтобы кэш успел протухнуть между проверками.
    verifier = EsiaIdTokenVerifier(_settings(jwks_cache_ttl_seconds=1))

    async with server.client() as client:
        await verifier.verify(_id_token(signing_key), nonce="nonce-1", client=client)
        verifier._fetched_at -= 10  # эмулируем протухший кэш, не ожидая TTL
        server.fail = True
        claims = await verifier.verify(
            _id_token(signing_key), nonce="nonce-1", client=client
        )

    assert claims["sub"] == "esia-oid-1"
