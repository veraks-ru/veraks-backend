"""Проверка подписи входящих вебхуков провайдеров (чистый домен, без I/O).

HMAC-SHA256 тела запроса по разделяемому секрету; сравнение — в постоянное
время (защита от timing-атак). Пустой секрет означает «верификация выключена»
(локальная разработка/тесты) — в проде секрет обязателен, иначе подделанный
вебхук мог бы провести платёж или выплату.
"""

from __future__ import annotations

import hashlib
import hmac


def compute_signature(secret: str, payload: bytes) -> str:
    """HMAC-SHA256 тела в hex по секрету."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(secret: str, payload: bytes, provided: str | None) -> bool:
    """Совпадает ли присланная подпись с ожидаемой.

    Пустой ``secret`` → ``True`` (верификация выключена). Отсутствующая подпись
    при заданном секрете → ``False``. Сравнение — constant-time.
    """
    if not secret:
        return True
    if not provided:
        return False
    expected = compute_signature(secret, payload)
    return hmac.compare_digest(expected, provided)
