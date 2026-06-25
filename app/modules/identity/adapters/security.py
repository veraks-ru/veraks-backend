"""Криптографические адаптеры и JWT-выпуск токенов.

- ``HmacSnilsHasher`` — детерминированный HMAC-SHA256 от СНИЛС (UNIQUE-ключ);
- ``FernetFieldEncryptor`` — симметричное шифрование ФИО (Fernet/AES-128-CBC+HMAC);
- ``JwtTokenIssuer`` — выпуск/верификация access/refresh JWT.

TODO(identity-infra): в проде HMAC/Fernet-ключи и подпись JWT держать в
secret manager; для JWT рассмотреть RS256 (асимметрия) вместо HS256.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.modules.identity.application.dto import SessionClaims
from app.modules.identity.domain.entities import UserRole
from app.modules.identity.domain.errors import InvalidTokenError
from app.modules.identity.domain.value_objects import Snils

_ACCESS_TOKEN_TYPE = "access"
_REFRESH_TOKEN_TYPE = "refresh"


class HmacSnilsHasher:
    """HMAC-SHA256 хеширование нормализованного СНИЛС."""

    def __init__(self, key: str) -> None:
        self._key = key.encode("utf-8")

    def hash(self, snils: Snils) -> str:
        """Возвращает hex-дайджест HMAC от 11 цифр СНИЛС."""
        return hmac.new(self._key, snils.digits.encode("utf-8"), hashlib.sha256).hexdigest()


class FernetFieldEncryptor:
    """Шифрование чувствительных полей через Fernet."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("utf-8"))

    def encrypt(self, plaintext: str) -> bytes:
        """Шифрует строку в байты."""
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Расшифровывает; поднимает ``InvalidTokenError`` при подмене ключа."""
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise InvalidTokenError("Невозможно расшифровать поле") from exc


class JwtTokenIssuer:
    """Выпуск и верификация JWT-сессий (HS256 по умолчанию)."""

    def __init__(
        self,
        *,
        secret: str,
        algorithm: str,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._access_ttl = access_ttl_seconds
        self._refresh_ttl = refresh_ttl_seconds

    def issue_access(self, claims: SessionClaims) -> str:
        """Короткоживущий access-токен."""
        token, _ = self._encode(claims, _ACCESS_TOKEN_TYPE, self._access_ttl)
        return token

    def issue_refresh(self, claims: SessionClaims) -> tuple[str, str]:
        """Refresh-токен и его ``jti`` (для отзыва/ротации)."""
        return self._encode(claims, _REFRESH_TOKEN_TYPE, self._refresh_ttl)

    def verify_access(self, token: str) -> SessionClaims:
        """Проверяет access-токен."""
        return self._decode(token, _ACCESS_TOKEN_TYPE)[0]

    def verify_refresh(self, token: str) -> tuple[SessionClaims, str]:
        """Проверяет refresh-токен, возвращает claims и jti."""
        return self._decode(token, _REFRESH_TOKEN_TYPE)

    def _encode(
        self, claims: SessionClaims, token_type: str, ttl: int
    ) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        jti = secrets.token_urlsafe(16)
        payload = {
            "sub": str(claims.user_id),
            "role": claims.role.value,
            "type": token_type,
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        }
        token = jwt.encode(payload, self._secret, algorithm=self._algorithm)
        return token, jti

    def _decode(self, token: str, expected_type: str) -> tuple[SessionClaims, str]:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("Недействительный токен") from exc
        if payload.get("type") != expected_type:
            raise InvalidTokenError("Неверный тип токена")
        try:
            user_id = uuid.UUID(payload["sub"])
            role = UserRole(payload["role"])
            jti = str(payload["jti"])
        except (KeyError, ValueError) as exc:
            raise InvalidTokenError("Повреждённые claims токена") from exc
        return SessionClaims(user_id=user_id, role=role), jti
