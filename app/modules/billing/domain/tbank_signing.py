"""Подпись запросов и проверка уведомлений ТБанк (Token, SHA-256).

Алгоритм (developer.tbank.ru/eacq/intro/developer/token): берём только скалярные
поля корневого объекта (вложенные объекты/массивы — Receipt, DATA, Shops —
исключаются), добавляем пару Password, сортируем по ключу, конкатенируем значения
без разделителей, SHA-256 (UTF-8) → hex.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

_EXCLUDED = frozenset({"Token", "Receipt", "DATA", "Shops", "Receipts"})


def _stringify(value: object) -> str:
    """Значение поля в строку для конкатенации (bool → 'true'/'false')."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _digest(params: Mapping[str, object], password: str) -> str:
    scalar: dict[str, object] = {
        key: value
        for key, value in params.items()
        if key not in _EXCLUDED and not isinstance(value, (dict, list, tuple))
    }
    scalar["Password"] = password
    concatenated = "".join(_stringify(scalar[key]) for key in sorted(scalar))
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()


def make_token(params: Mapping[str, object], password: str) -> str:
    """Подпись исходящего запроса (Init/Cancel и т.п.)."""
    return _digest(params, password)


def verify_token(payload: Mapping[str, object], password: str) -> bool:
    """Проверка Token входящего уведомления (constant-time)."""
    provided = payload.get("Token")
    if not isinstance(provided, str) or not provided:
        return False
    expected = _digest(payload, password)
    return hmac.compare_digest(expected, provided)
