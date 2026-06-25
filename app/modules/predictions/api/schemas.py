"""Pydantic-схемы запросов/ответов эндпоинтов predictions.

Контракт HTTP-слоя, отделённый от доменных сущностей: пользователь шлёт лишь
градацию уверенности, а вероятность — производная и отдаётся в ответе.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction


class PlacePredictionRequest(BaseModel):
    """Тело запроса постановки/изменения прогноза.

    Пользователь передаёт только градацию уверенности; внутренняя вероятность
    выводится сервером (неизменяемость смысла прогноза).
    """

    confidence_grade: ConfidenceGrade


class PredictionResponse(BaseModel):
    """Проекция прогноза для клиента."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    event_id: uuid.UUID
    confidence_grade: ConfidenceGrade
    probability: Decimal
    is_locked: bool
    brier_score: Decimal | None
    scored_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, prediction: Prediction) -> PredictionResponse:
        """Маппинг доменной сущности в ответ."""
        return cls(
            id=prediction.id,
            user_id=prediction.user_id,
            event_id=prediction.event_id,
            confidence_grade=prediction.confidence_grade,
            probability=prediction.probability,
            is_locked=prediction.is_locked,
            brier_score=prediction.brier_score,
            scored_at=prediction.scored_at,
            created_at=prediction.created_at,
            updated_at=prediction.updated_at,
        )
