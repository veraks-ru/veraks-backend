"""Порт шифрования полей с ПДн (телефон, ФИО реквизитов выплат).

Структурно совпадает с ``FernetFieldEncryptor`` из identity — композит-рут
подставляет его же (один ключ ``SECURITY_FIELD_ENCRYPTION_KEY``), billing
зависит только от этого протокола.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class FieldEncryptor(Protocol):
    """Симметричное шифрование строкового поля перед записью в БД."""

    def encrypt(self, plaintext: str) -> bytes:
        """Зашифровать значение поля."""
        ...

    def decrypt(self, ciphertext: bytes) -> str:
        """Расшифровать значение поля."""
        ...
