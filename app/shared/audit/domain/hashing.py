"""Чистая хеш-цепочка аудита (детерминированная, без I/O).

Канонизация payload'а гарантирует, что один и тот же логический набор полей
даёт один и тот же хеш независимо от порядка ключей. Звено цепочки:
``hash = sha256(prev_hash ‖ RS ‖ canonical_json(payload))``, где ``RS`` —
разделитель записей (0x1e), исключающий склейку соседних полей.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

_RECORD_SEPARATOR = b"\x1e"


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Канонический JSON: сортированные ключи, без пробелов, не-ASCII как есть.

    ``default=str`` сериализует UUID/datetime детерминированно (вызывающая
    сторона передаёт уже приведённые к строкам значения, это лишь страховка).
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def chain_hash(prev_hash: str | None, payload: Mapping[str, Any]) -> str:
    """Считает хеш звена поверх предыдущего ``hash`` и канонического payload'а."""
    digest = hashlib.sha256()
    digest.update((prev_hash or "").encode("utf-8"))
    digest.update(_RECORD_SEPARATOR)
    digest.update(canonical_json(payload).encode("utf-8"))
    return digest.hexdigest()
