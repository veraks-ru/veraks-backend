"""Генерация и хэширование секретов API-ключей."""

from __future__ import annotations

import hashlib
import secrets

_PREFIX = "vk_"  # veraks key
_PREFIX_LEN = 11  # "vk_" + 8 символов для узнавания в списке


class SecretsKeyGenerator:
    """Криптостойкий секрет; хранится SHA-256 (как пароль)."""

    def generate(self) -> str:
        return _PREFIX + secrets.token_urlsafe(32)

    def hash(self, plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    def prefix(self, plaintext: str) -> str:
        return plaintext[:_PREFIX_LEN]
