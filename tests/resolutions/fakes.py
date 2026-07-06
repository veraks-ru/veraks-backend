"""In-memory фейки портов resolutions для изолированного тестирования.

Реализуют те же протоколы, что и продакшн-адаптеры, но без Postgres. Фейковый
``EventResolutionGateway`` ведёт состояние событий в памяти, эмулируя переходы
автомата; репозитории клонируют сущности на входе/выходе, чтобы внешние
мутации не протекали в хранилище.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from app.modules.events.domain.entities import EventStatus
from app.modules.resolutions.application.dto import EventLifecycle
from app.modules.resolutions.domain.entities import Dispute, Resolution
from app.shared.audit.domain.entities import AuditActorType, AuditEntry


class FakeClock:
    """Часы с фиксированным временем."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _EventState:
    """Внутреннее состояние события в фейковом шлюзе."""

    def __init__(
        self,
        *,
        status: EventStatus,
        outcome: bool | None,
        dispute_window_ends_at: datetime | None,
        season_id: uuid.UUID | None,
    ) -> None:
        self.status = status
        self.outcome = outcome
        self.dispute_window_ends_at = dispute_window_ends_at
        self.season_id = season_id


class FakeEventResolutionGateway:
    """Шлюз статуса события в памяти (эмулирует переходs автомата events)."""

    def __init__(self) -> None:
        self._events: dict[uuid.UUID, _EventState] = {}
        # Событие, прочитанные с блокировкой строки (for_update=True) — для
        # ассертов в тестах гонок (M-RESRACE).
        self.locked_reads: list[uuid.UUID] = []

    def seed(
        self,
        event_id: uuid.UUID,
        *,
        status: EventStatus,
        outcome: bool | None = None,
        dispute_window_ends_at: datetime | None = None,
        season_id: uuid.UUID | None = None,
    ) -> None:
        """Заводит событие с заданным состоянием жизненного цикла."""
        self._events[event_id] = _EventState(
            status=status,
            outcome=outcome,
            dispute_window_ends_at=dispute_window_ends_at,
            season_id=season_id,
        )

    def status_of(self, event_id: uuid.UUID) -> EventStatus:
        """Текущий статус события (для ассертов)."""
        return self._events[event_id].status

    async def get_lifecycle(
        self, event_id: uuid.UUID, *, for_update: bool = False
    ) -> EventLifecycle | None:
        if for_update:
            self.locked_reads.append(event_id)
        state = self._events.get(event_id)
        if state is None:
            return None
        return EventLifecycle(
            event_id=event_id,
            status=state.status,
            outcome=state.outcome,
            dispute_window_ends_at=state.dispute_window_ends_at,
            season_id=state.season_id,
        )

    async def fix_outcome(
        self,
        event_id: uuid.UUID,
        *,
        outcome: bool,
        dispute_window_ends_at: datetime,
        now: datetime,
    ) -> None:
        state = self._events[event_id]
        state.status = EventStatus.RESOLVED
        state.outcome = outcome
        state.dispute_window_ends_at = dispute_window_ends_at

    async def open_dispute(self, event_id: uuid.UUID, *, now: datetime) -> None:
        self._events[event_id].status = EventStatus.DISPUTED

    async def dismiss_dispute(self, event_id: uuid.UUID, *, now: datetime) -> None:
        self._events[event_id].status = EventStatus.RESOLVED

    async def overturn_outcome(
        self,
        event_id: uuid.UUID,
        *,
        outcome: bool,
        dispute_window_ends_at: datetime,
        now: datetime,
    ) -> None:
        state = self._events[event_id]
        state.status = EventStatus.RESOLVED
        state.outcome = outcome
        state.dispute_window_ends_at = dispute_window_ends_at

    async def find_resolved_past_window(self, *, now: datetime) -> list[uuid.UUID]:
        return [
            event_id
            for event_id, state in self._events.items()
            if state.status is EventStatus.RESOLVED
            and state.dispute_window_ends_at is not None
            and state.dispute_window_ends_at <= now
        ]


class InMemoryResolutionRepository:
    """Append-only журнал решений в памяти."""

    def __init__(self) -> None:
        self._items: list[Resolution] = []

    async def add(self, resolution: Resolution) -> Resolution:
        self._items.append(replace(resolution))
        return replace(resolution)

    async def current_final(self, event_id: uuid.UUID) -> Resolution | None:
        superseded = {r.supersedes_id for r in self._items if r.supersedes_id}
        finals = [
            r
            for r in self._items
            if r.event_id == event_id
            and r.status.value == "final"
            and r.id not in superseded
        ]
        if not finals:
            return None
        latest = max(finals, key=lambda r: r.resolved_at)
        return replace(latest)

    async def list_for_event(self, event_id: uuid.UUID) -> list[Resolution]:
        rows = [r for r in self._items if r.event_id == event_id]
        rows.sort(key=lambda r: r.resolved_at, reverse=True)
        return [replace(r) for r in rows]


class InMemoryDisputeRepository:
    """Хранилище споров в памяти; ``has_open_in_season`` — через карту сезонов."""

    def __init__(
        self, *, event_seasons: Mapping[uuid.UUID, uuid.UUID] | None = None
    ) -> None:
        self._by_id: dict[uuid.UUID, Dispute] = {}
        self._event_seasons = dict(event_seasons or {})

    async def add(self, dispute: Dispute) -> Dispute:
        self._by_id[dispute.id] = replace(dispute)
        return replace(dispute)

    async def get_by_id(self, dispute_id: uuid.UUID) -> Dispute | None:
        found = self._by_id.get(dispute_id)
        return replace(found) if found else None

    async def update(self, dispute: Dispute) -> Dispute:
        self._by_id[dispute.id] = replace(dispute)
        return replace(dispute)

    async def list_for_event(self, event_id: uuid.UUID) -> list[Dispute]:
        rows = [d for d in self._by_id.values() if d.event_id == event_id]
        rows.sort(key=lambda d: d.created_at, reverse=True)
        return [replace(d) for d in rows]

    async def has_open_for_event(self, event_id: uuid.UUID) -> bool:
        return any(
            d.event_id == event_id and d.is_open() for d in self._by_id.values()
        )

    async def has_open_in_season(self, season_id: uuid.UUID) -> bool:
        return any(
            d.is_open() and self._event_seasons.get(d.event_id) == season_id
            for d in self._by_id.values()
        )


class InMemoryScoringDispatchRepository:
    """Маркеры поставленного скоринга в памяти."""

    def __init__(self) -> None:
        self._seen: set[uuid.UUID] = set()
        self.added: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def exists(self, resolution_id: uuid.UUID) -> bool:
        return resolution_id in self._seen

    async def add(
        self, *, resolution_id: uuid.UUID, event_id: uuid.UUID, now: datetime
    ) -> bool:
        if resolution_id in self._seen:
            return False
        self._seen.add(resolution_id)
        self.added.append((resolution_id, event_id))
        return True


class FakeParticipationGateway:
    """Проверка участия: множество пар ``(user_id, event_id)``."""

    def __init__(self) -> None:
        self._participants: set[tuple[uuid.UUID, uuid.UUID]] = set()

    def allow(self, user_id: uuid.UUID, event_id: uuid.UUID) -> None:
        """Помечает пользователя участником события."""
        self._participants.add((user_id, event_id))

    async def has_prediction(
        self, *, user_id: uuid.UUID, event_id: uuid.UUID
    ) -> bool:
        return (user_id, event_id) in self._participants


class FakeTaskScheduler:
    """Собирает поставленные в очередь события скоринга."""

    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def enqueue_score_event(self, event_id: uuid.UUID) -> None:
        self.enqueued.append(event_id)


class FakeAuditTrail:
    """Запоминает записанные действия (без хеш-цепочки)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_type: AuditActorType,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        self.records.append(
            {
                "actor_id": actor_id,
                "actor_type": actor_type,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
            }
        )
        return AuditEntry(
            occurred_at=datetime.now(),  # noqa: DTZ005 — фейк, время не важно
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            hash="fake",
        )

    def actions(self) -> list[str]:
        """Список зафиксированных action'ов (для ассертов)."""
        return [r["action"] for r in self.records]
