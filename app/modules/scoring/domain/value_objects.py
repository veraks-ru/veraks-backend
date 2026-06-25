"""Value-objects домена scoring — входные проекции и квантование результата.

Чистый код без I/O. ``ResolvedEvent`` — минимальная проекция разрешённого
события с прогнозами участников, нужная и для пер-прогнозного Brier, и для
пересчёта рейтингов. Полные сущности predictions/events сюда не тянем —
данные приходят через порты-шлюзы.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Точность хранения метрик скоринга — ``numeric(6,5)`` в БД.
_SCORE_QUANT = Decimal("0.00001")


def quantize_score(value: float) -> Decimal:
    """Округляет вещественную метрику до 5 знаков (под колонку ``numeric(6,5)``).

    Границу «float-домен → Decimal-хранилище» держим в одном месте, чтобы
    нигде не протекали «грязные» хвосты двоичного представления.
    """
    return Decimal(str(value)).quantize(_SCORE_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PredictionVote:
    """Голос участника по событию: чей и с какой внутренней вероятностью."""

    user_id: uuid.UUID
    probability: float


@dataclass(frozen=True, slots=True)
class ResolvedEvent:
    """Разрешённое событие с заблокированными прогнозами и финальным исходом.

    ``outcome`` — ``int ∈ {0, 1}`` (1 = «ДА» наступило). ``votes`` — все
    заблокированные прогнозы события (для консенсуса/LOO/веса).
    """

    event_id: uuid.UUID
    category_id: uuid.UUID
    season_id: uuid.UUID | None
    outcome: int
    votes: tuple[PredictionVote, ...]

    @property
    def predictor_count(self) -> int:
        """Число предсказателей события (для порога ``MIN_PREDICTORS``)."""
        return len(self.votes)

    def probabilities(self) -> list[float]:
        """Список вероятностей всех голосов (для консенсуса толпы)."""
        return [vote.probability for vote in self.votes]
