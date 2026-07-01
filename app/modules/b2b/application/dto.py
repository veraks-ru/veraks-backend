"""Read-модели сигналов B2B (проекции консенсуса, рейтингов, событий)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ConsensusSignal:
    """Сигнал консенсуса толпы по событию."""

    event_id: uuid.UUID
    total_count: int
    mean_probability: float | None
    distribution: dict[str, int]  # градация(value) → число голосов


@dataclass(frozen=True, slots=True)
class LeaderboardSignalRow:
    """Строка сигнала рейтинга предсказателя."""

    rank: int
    user_id: uuid.UUID
    username: str
    skill_score: Decimal
    mean_brier: Decimal
    n_resolved: int


@dataclass(frozen=True, slots=True)
class EventSignal:
    """Проекция события для B2B-ленты."""

    id: uuid.UUID
    title: str
    category_id: uuid.UUID
    season_id: uuid.UUID | None
    status: str
    opens_at: datetime
    closes_at: datetime
    resolves_at: datetime
    outcome: bool | None
