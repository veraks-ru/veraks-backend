"""DTO прикладного слоя scoring — контракты данных между портами и use-cases.

Чистые dataclass'ы без I/O. ``EventScoringStatus`` отвечает на вопрос «можно
ли уже скорить событие» (найдено / разрешено / прошло окно оспаривания);
``PredictionScore`` — результат пер-прогнозного Brier для записи обратно в
``predictions``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class EventScoringStatus:
    """Готовность события к скорингу (из домена events/resolutions).

    ``is_final`` означает, что исход зафиксирован финально И окно оспаривания
    закрыто — только тогда домен scoring считает Brier (см. поток
    жизненного цикла в задании).
    """

    found: bool
    is_resolved: bool
    is_final: bool
    outcome: int | None

    @property
    def is_scoreable(self) -> bool:
        """Можно ли считать Brier: разрешено, финально и исход известен."""
        return (
            self.found
            and self.is_resolved
            and self.is_final
            and self.outcome is not None
        )


@dataclass(frozen=True, slots=True)
class PredictionScore:
    """Проставляемая оценка прогноза: чей прогноз и его Brier."""

    user_id: uuid.UUID
    brier: Decimal
