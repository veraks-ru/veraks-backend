"""Глобальная настройка тестов.

Выставляет тестовое окружение ДО импорта приложения (чтобы Settings
сконструировались) и даёт общие билдеры для криптопортов и личности ЕСИА.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

# ── Тестовое окружение (до импорта app.*) ─────────────────────────────────
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("SECURITY_SNILS_HMAC_KEY", "test-snils-hmac-key-0123456789abcdef")
os.environ.setdefault("SECURITY_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SECURITY_JWT_SECRET", "test-jwt-secret-0123456789abcdef-pad")
os.environ.setdefault("SECURITY_ACCESS_TOKEN_TTL_SECONDS", "900")
os.environ.setdefault("SECURITY_REFRESH_TOKEN_TTL_SECONDS", "3600")
os.environ.setdefault("SECURITY_COOKIE_SECURE", "false")
os.environ.setdefault("ESIA_CLIENT_ID", "test-client")
os.environ.setdefault("ESIA_REDIRECT_URI", "https://orakul.test/auth/esia/callback")
os.environ.setdefault("ESIA_AUTHORIZATION_ENDPOINT", "https://esia.test/authorize")
os.environ.setdefault("ESIA_TOKEN_ENDPOINT", "https://esia.test/token")
os.environ.setdefault("ESIA_USERINFO_ENDPOINT", "https://esia.test/userinfo")
os.environ.setdefault("ESIA_REQUIRE_CONFIRMED", "true")

import pytest  # noqa: E402

from app.modules.identity.adapters.security import (  # noqa: E402
    FernetFieldEncryptor,
    HmacSnilsHasher,
    JwtTokenIssuer,
)
from app.modules.identity.domain.value_objects import EsiaIdentity, Snils  # noqa: E402

# Валидный СНИЛС (контрольная сумма корректна): 112-233-445 95.
VALID_SNILS = "11223344595"


@pytest.fixture
def snils_hasher() -> HmacSnilsHasher:
    return HmacSnilsHasher("test-snils-hmac-key-0123456789abcdef")


@pytest.fixture
def encryptor() -> FernetFieldEncryptor:
    return FernetFieldEncryptor(os.environ["SECURITY_FIELD_ENCRYPTION_KEY"])


@pytest.fixture
def token_issuer() -> JwtTokenIssuer:
    return JwtTokenIssuer(
        secret="test-jwt-secret-0123456789abcdef-pad",
        algorithm="HS256",
        access_ttl_seconds=900,
        refresh_ttl_seconds=3600,
    )


@pytest.fixture
def confirmed_identity() -> EsiaIdentity:
    return EsiaIdentity(
        oid="esia-oid-1",
        snils=Snils.parse(VALID_SNILS),
        first_name="Иван",
        last_name="Петров",
        middle_name="Сергеевич",
        trusted=True,
    )
