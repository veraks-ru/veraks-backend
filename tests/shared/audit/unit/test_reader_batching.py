"""Юнит-тест пагинации ``SqlAlchemyAuditLogReader.stream_ordered`` через границу пачки.

Реальную БД не поднимаем — вместо этого подсовываем ридеру session-дубль,
отдающий заранее нарезанные пачки по вызовам ``execute``. Так тестируется
именно цикл пагинации (``while True`` в ``reader.py``: продолжать, пока
пачка не пуста, копить курсор по последнему ``id``), а не SQL сам по себе
(SQL-часть — предмет e2e-теста над реальным Postgres).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.shared.audit.adapters.reader import SqlAlchemyAuditLogReader
from app.shared.audit.domain.entities import AuditActorType, AuditEntry


class _FakeRow:
    """Ряд-заглушка: как ``AuditLogORM`` — есть ``.id`` и ``.to_domain()``."""

    def __init__(self, row_id: int) -> None:
        self.id = row_id

    def to_domain(self) -> AuditEntry:
        return AuditEntry(
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            actor_id=None,
            actor_type=AuditActorType.SYSTEM,
            action=f"test.action.{self.id}",
            entity_type="test",
            entity_id=None,
            hash=f"hash-{self.id}",
            id=self.id,
        )


class _FakeScalars:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def all(self) -> list[_FakeRow]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeSession:
    """Отдаёт пачки из заранее заданного списка, по одной за вызов ``execute``.

    Не парсит переданный ``stmt`` (это SQL, ей и место в e2e) — только считает
    вызовы и возвращает следующую пачку, эмулируя keyset-пагинацию по числу
    round-trip'ов, а не по фактическому ``WHERE id > :last_id``.
    """

    def __init__(self, batches: list[list[int]]) -> None:
        self._batches = list(batches)
        self.execute_calls = 0

    async def execute(self, _stmt: Any) -> _FakeResult:
        self.execute_calls += 1
        ids = self._batches.pop(0) if self._batches else []
        return _FakeResult([_FakeRow(i) for i in ids])


async def test_stream_ordered_walks_multiple_batches_in_order() -> None:
    """5 записей при размере пачки 2 → 3 непустых round-trip'а + 1 пустой (стоп)."""
    # [1,2] [3,4] [5] [] — последний пустой обрывает цикл (см. reader.py).
    session = _FakeSession(batches=[[1, 2], [3, 4], [5], []])
    reader = SqlAlchemyAuditLogReader(session, batch_size=2)  # type: ignore[arg-type]

    entries = [entry async for entry in reader.stream_ordered()]

    assert [e.id for e in entries] == [1, 2, 3, 4, 5]
    assert session.execute_calls == 4  # 3 пачки с данными + финальная пустая


async def test_stream_ordered_stops_immediately_when_empty() -> None:
    session = _FakeSession(batches=[[]])
    reader = SqlAlchemyAuditLogReader(session, batch_size=500)  # type: ignore[arg-type]

    entries = [entry async for entry in reader.stream_ordered()]

    assert entries == []
    assert session.execute_calls == 1


async def test_stream_ordered_single_batch_no_trailing_roundtrip_needed_beyond_empty() -> None:
    """Пачка меньше ``batch_size`` — цикл всё равно делает ещё один (пустой) запрос."""
    session = _FakeSession(batches=[[1, 2, 3], []])
    reader = SqlAlchemyAuditLogReader(session, batch_size=500)  # type: ignore[arg-type]

    entries = [entry async for entry in reader.stream_ordered()]

    assert [e.id for e in entries] == [1, 2, 3]
    assert session.execute_calls == 2
