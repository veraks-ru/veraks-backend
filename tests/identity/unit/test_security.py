"""Юнит-тесты криптоадаптеров: HMAC СНИЛС, шифрование ФИО, JWT."""

from __future__ import annotations

import uuid

import pytest

from app.modules.identity.adapters.security import (
    FernetFieldEncryptor,
    HmacSnilsHasher,
    JwtTokenIssuer,
)
from app.modules.identity.application.dto import SessionClaims
from app.modules.identity.domain.entities import UserRole
from app.modules.identity.domain.errors import InvalidTokenError
from app.modules.identity.domain.value_objects import Snils


def test_snils_hash_is_deterministic_and_keyed() -> None:
    snils = Snils.parse("11223344595")
    h1 = HmacSnilsHasher("k1").hash(snils)
    h2 = HmacSnilsHasher("k1").hash(snils)
    h3 = HmacSnilsHasher("k2").hash(snils)
    assert h1 == h2  # детерминированность → годится для UNIQUE
    assert h1 != h3  # зависит от ключа
    assert "11223344595" not in h1  # сырой СНИЛС не утекает


def test_field_encryptor_roundtrip(encryptor: FernetFieldEncryptor) -> None:
    ciphertext = encryptor.encrypt("Петров Иван")
    assert encryptor.decrypt(ciphertext) == "Петров Иван"
    assert b"Petrov" not in ciphertext


def test_access_token_roundtrip(token_issuer: JwtTokenIssuer) -> None:
    claims = SessionClaims(user_id=uuid.uuid4(), role=UserRole.EDITOR)
    token = token_issuer.issue_access(claims)
    decoded = token_issuer.verify_access(token)
    assert decoded == claims


def test_refresh_token_carries_jti(token_issuer: JwtTokenIssuer) -> None:
    claims = SessionClaims(user_id=uuid.uuid4(), role=UserRole.USER)
    token, jti = token_issuer.issue_refresh(claims)
    decoded, decoded_jti = token_issuer.verify_refresh(token)
    assert decoded == claims
    assert decoded_jti == jti


def test_access_token_rejected_as_refresh(token_issuer: JwtTokenIssuer) -> None:
    claims = SessionClaims(user_id=uuid.uuid4(), role=UserRole.USER)
    access = token_issuer.issue_access(claims)
    with pytest.raises(InvalidTokenError):
        token_issuer.verify_refresh(access)


def test_tampered_token_rejected(token_issuer: JwtTokenIssuer) -> None:
    with pytest.raises(InvalidTokenError):
        token_issuer.verify_access("not-a-jwt")
