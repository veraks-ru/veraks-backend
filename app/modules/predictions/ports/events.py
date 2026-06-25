"""Порт доступа к состоянию события (исходящая зависимость к домену events).

Домен прогнозов не знает внутренних типов events: ему нужен лишь снимок окна
приёма (:class:`EventSnapshot`), чтобы решить, принимается ли прогноз сейчас.
Реализация-адаптер (``adapters/event_gateway.py``) переводит сущность события
в снимок.

TODO(events-integration): при выносе events в отдельный сервис заменить
адаптер на HTTP/событийный контракт — порт при этом не меняется.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.modules.predictions.domain.value_objects import EventSnapshot


@runtime_checkable
class EventGateway(Protocol):
    """Чтение снимка события для валидации приёма прогноза."""

    async def get_snapshot(self, event_id: uuid.UUID) -> EventSnapshot | None:
        """Снимок окна события по id или ``None``, если события нет."""
        ...
