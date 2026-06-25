"""Адаптер шлюза к домену events.

Реализует порт :class:`~app.modules.predictions.ports.events.EventGateway`
поверх публичного репозитория-порта events (``EventRepository``). В монолите
с единой БД это прямое чтение события; снимок строится из его окна и статуса.

TODO(events-integration): при выносе events в отдельный сервис заменить на
HTTP/событийный контракт — порт и use-cases при этом не меняются.
"""

from __future__ import annotations

import uuid

from app.modules.events.domain.entities import EventStatus
from app.modules.events.ports.repositories import EventRepository
from app.modules.predictions.domain.value_objects import EventSnapshot


class EventRepositoryGateway:
    """``EventGateway`` поверх репозитория событий домена events."""

    def __init__(self, events: EventRepository) -> None:
        self._events = events

    async def get_snapshot(self, event_id: uuid.UUID) -> EventSnapshot | None:
        """Читает событие и переводит его в снимок окна приёма."""
        event = await self._events.get_by_id(event_id)
        if event is None:
            return None
        return EventSnapshot(
            event_id=event.id,
            is_open=event.status is EventStatus.OPEN,
            opens_at=event.window.opens_at,
            closes_at=event.window.closes_at,
        )
