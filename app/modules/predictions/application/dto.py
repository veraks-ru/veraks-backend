"""DTO прикладного слоя predictions — нейтральные контракты (без pydantic).

API-схемы (``api/schemas.py``) транслируются в эти структуры и обратно из
доменных сущностей; HTTP-детали внутрь домена не протекают.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.modules.predictions.domain.entities import ConfidenceGrade


@dataclass(frozen=True, slots=True)
class PredictionSummary:
    """Агрегат прогнозов по событию — «сигнал толпы».

    ``distribution`` — число прогнозов на каждую градацию (все пять ключей,
    отсутствующие = 0). ``mean_probability`` — консенсус толпы ``c_e`` (среднее
    внутренних вероятностей), ``None`` при отсутствии прогнозов.
    """

    event_id: uuid.UUID
    total_count: int
    distribution: dict[ConfidenceGrade, int]
    mean_probability: Decimal | None


@dataclass(frozen=True, slots=True)
class PredictionAuditEntry:
    """Запись истории изменения прогноза для порта аудита.

    Фиксирует переход градации ``before → after`` (значения enum в виде строк).
    ``before is None`` — первичная постановка прогноза. Предназначена для
    append-only ``audit_log`` (см. модель данных §2.6).
    """

    action: str
    actor_id: uuid.UUID
    event_id: uuid.UUID
    prediction_id: uuid.UUID
    before: str | None
    after: str
    occurred_at: datetime
