"""Value-objects домена predictions.

Чистый код без I/O. Здесь живёт ``EventSnapshot`` — минимальная проекция
состояния события, нужная домену прогнозов, чтобы решить «принимаем ли мы
сейчас прогноз». Полную сущность события (домен events) сюда не тянем —
зависимость к events идёт через порт :class:`~app.modules.predictions.ports.events.EventGateway`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    """Снимок окна приёма события для домена прогнозов.

    ``is_open`` — событие в статусе приёма (``open``); ``opens_at``/``closes_at``
    — серверное временное окно. Зеркалит ``Event.can_accept_predictions`` из
    домена events, но без утечки его внутренних типов сюда.
    """

    event_id: uuid.UUID
    is_open: bool
    opens_at: datetime
    closes_at: datetime

    def is_accepting_at(self, moment: datetime) -> bool:
        """Открыт ли приём прогнозов в указанный момент (источник времени — сервер)."""
        return self.is_open and self.opens_at <= moment < self.closes_at
