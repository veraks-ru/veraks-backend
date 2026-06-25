"""Доменные сущности scoring: ``Rating`` и ``ScopeType``.

``Rating`` — материализованный агрегат для лидербордов и профилей: на чтении
ничего не считается, всё пересчитывается фоном (см. use-case
``RecomputeRatings``). Это обычный dataclass без знания о SQLAlchemy; ORM
(``adapters/orm.py``) маппится на него через ``to_domain``/``from_domain``.

Метрики хранятся как ``Decimal`` (под ``numeric(6,5)``), чтобы зеркалить
точность БД; вычисляются они во float-домене формул и квантуются на границе
(``value_objects.quantize_score``).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal


class ScopeType(str, enum.Enum):
    """Область рейтинга: глобально / по категории / по сезону."""

    GLOBAL = "global"
    CATEGORY = "category"
    SEASON = "season"


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Rating:
    """Агрегат точности пользователя в одной области (``scope``).

    ``skill_score`` — ранжирующая метрика «больше = лучше»: усаженное
    средневзвешенное превышение над толпой ``R`` (см.
    ``formulas.season_rating_from_contributions``). ``mean_brier`` —
    средний Brier (меньше = лучше), ``calibration_error`` — ECE.
    ``rank`` проставляется при перестроении лидерборда области.
    """

    user_id: uuid.UUID
    scope_type: ScopeType
    scope_id: uuid.UUID | None  # category_id / season_id; ``None`` для global
    mean_brier: Decimal
    skill_score: Decimal
    calibration_error: Decimal
    n_resolved: int
    rank: int = 0
    updated_at: datetime = field(default_factory=_utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def assign_rank(self, rank: int, *, now: datetime | None = None) -> None:
        """Проставляет предрасчитанный ранг в области (1 = лучший)."""
        self.rank = rank
        self.updated_at = now or _utcnow()
