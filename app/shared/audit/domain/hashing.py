"""Чистая хеш-цепочка аудита (детерминированная, без I/O).

Канонизация payload'а гарантирует, что один и тот же логический набор полей
даёт один и тот же хеш независимо от порядка ключей. Звено цепочки:
``hash = sha256(prev_hash ‖ RS ‖ canonical_json(payload))``, где ``RS`` —
разделитель записей (0x1e), исключающий склейку соседних полей.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.shared.audit.domain.entities import AuditActorType

_RECORD_SEPARATOR = b"\x1e"


def entry_payload(
    *,
    occurred_at: datetime,
    actor_id: uuid.UUID | None,
    actor_type: AuditActorType,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Собирает payload звена — единая формула для записи И верификации.

    Используется и адаптером записи (:mod:`adapters.trail`) при вычислении
    хеша нового звена, и use-case верификации цепочки при пересчёте хеша уже
    сохранённой записи — так формула payload'а живёт в одном месте.
    """
    return {
        "occurred_at": occurred_at.isoformat(),
        "actor_id": str(actor_id) if actor_id else None,
        "actor_type": actor_type.value,
        "action": action,
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id else None,
        "before": dict(before) if before is not None else None,
        "after": dict(after) if after is not None else None,
        "metadata": dict(metadata),
    }


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
