"""Верификация целостности хеш-цепочки ``audit_log``.

ARCHITECTURE.md §2.6/§6 обещают tamper-evidence: возможность доказать, что
журнал не правили в обход приложения (напрямую в БД, в обход append-only
триггера роли приложения — например с правами суперпользователя). Формула
звена — ``domain/hashing.py``; здесь она применяется по всей цепочке.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.shared.audit.domain.hashing import chain_hash, entry_payload
from app.shared.audit.ports.audit_reader import AuditLogReader


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    """Итог прохода по цепочке: где (если есть) она сломана."""

    ok: bool
    checked: int
    first_broken_id: int | None = None


class VerifyAuditChain:
    """Проходит ``audit_log`` по возрастанию ``id``, пересчитывая звенья.

    На каждой записи проверяется два инварианта: (1) её ``prev_hash`` — это
    реально ``hash`` предыдущей записи (иначе цепочка разорвана — подмена
    порядка/удаление записи), (2) её собственный ``hash`` — это
    ``chain_hash(prev_hash, payload)`` от её текущего содержимого (иначе
    содержимое подменено без пересчёта хеша). Первое несовпадение — и есть
    первая испорченная запись; дальше не идём (нет смысла — цепочка после
    порчи не восстановима без внешнего свидетеля).

    **Честно про то, чего эта проверка НЕ обнаруживает**: обрезание хвоста.
    Если у атакующего есть доступ в обход роли приложения (например,
    суперпользователь БД) и он удаляет последние N записей журнала целиком
    (не подменяя, а просто стирая), цепочка, что осталась, по-прежнему
    внутренне консистентна — ``ok=True``, потому что верификация видит только
    то, что реально есть в таблице, и не знает, сколько записей «должно
    было» быть. Обнаружить такое усечение можно только внешним якорем —
    периодически публиковать последний ``hash`` куда-то вне досягаемости той
    же БД (см. заметку в ARCHITECTURE.md §2.6); эта задача якорь не реализует.
    """

    def __init__(self, *, reader: AuditLogReader) -> None:
        self._reader = reader

    async def execute(self) -> ChainVerificationResult:
        checked = 0
        expected_prev: str | None = None
        async for entry in self._reader.stream_ordered():
            checked += 1
            if entry.prev_hash != expected_prev:
                return ChainVerificationResult(
                    ok=False, checked=checked, first_broken_id=entry.id
                )
            payload = entry_payload(
                occurred_at=entry.occurred_at,
                actor_id=entry.actor_id,
                actor_type=entry.actor_type,
                action=entry.action,
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                before=entry.before,
                after=entry.after,
                metadata=entry.metadata,
            )
            if chain_hash(expected_prev, payload) != entry.hash:
                return ChainVerificationResult(
                    ok=False, checked=checked, first_broken_id=entry.id
                )
            expected_prev = entry.hash
        return ChainVerificationResult(ok=True, checked=checked)
