"""Доменные политики predictions — чистые правила без I/O.

Главное правило честности: прогноз принимается/правится только пока событие
открыто и серверное время не вышло за ``closes_at``. Источник времени —
сервер (см. конвенции модели данных), поэтому момент передаётся явно.
"""

from __future__ import annotations

from datetime import datetime

from app.modules.predictions.domain.errors import PredictionsClosedError
from app.modules.predictions.domain.value_objects import EventSnapshot


def ensure_event_accepts_predictions(
    snapshot: EventSnapshot, *, now: datetime
) -> None:
    """Требует, чтобы событие принимало прогнозы в момент ``now``.

    Поднимает :class:`PredictionsClosedError`, если событие не в статусе
    ``open`` либо окно приёма уже закрыто (дедлайн прошёл).
    """
    if not snapshot.is_accepting_at(now):
        raise PredictionsClosedError(
            "Приём прогнозов по событию закрыт: событие не открыто или дедлайн прошёл"
        )
