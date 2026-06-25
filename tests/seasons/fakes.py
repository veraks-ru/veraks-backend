"""In-memory фейки портов seasons для изолированного тестирования use-cases.

Реализуют те же протоколы, что и продакшн-адаптеры, но без Postgres: репозиторий
— мапа по id с эмуляцией ``UNIQUE(slug)`` и журналом финализаций; ``DisputeGuard``
— управляемый флаг открытых споров.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.value_objects import (
    SeasonFinalization,
    SeasonFinalizationEntry,
)


class FakeClock:
    """Часы с фиксированным временем."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class InMemorySeasonRepository:
    """Хранилище сезонов в памяти + журнал финализаций (append-only)."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, Season] = {}
        self.finalizations: list[
            tuple[SeasonFinalization, list[SeasonFinalizationEntry]]
        ] = []

    async def add(self, season: Season) -> None:
        self._by_id[season.id] = season

    async def get_by_id(self, season_id: uuid.UUID) -> Season | None:
        return self._by_id.get(season_id)

    async def get_by_slug(self, slug: str) -> Season | None:
        for season in self._by_id.values():
            if season.slug.lower() == slug.lower():  # citext — без учёта регистра
                return season
        return None

    async def list(self, *, status: SeasonStatus | None = None) -> list[Season]:
        seasons = list(self._by_id.values())
        if status is not None:
            seasons = [s for s in seasons if s.status is status]
        return sorted(seasons, key=lambda s: s.starts_at)

    async def update(self, season: Season) -> None:
        self._by_id[season.id] = season

    async def lock_for_finalize(self, season_id: uuid.UUID) -> Season | None:
        # В Postgres-адаптере здесь SELECT ... FOR UPDATE; в памяти — обычное чтение.
        return self._by_id.get(season_id)

    async def append_finalization(
        self,
        finalization: SeasonFinalization,
        entries: Sequence[SeasonFinalizationEntry],
    ) -> None:
        self.finalizations.append((finalization, list(entries)))


class FakeDisputeGuard:
    """Управляемая заглушка проверки открытых споров."""

    def __init__(self, *, has_open: bool = False) -> None:
        self._has_open = has_open
        self.calls = 0

    async def has_open_disputes(self, season_id: uuid.UUID) -> bool:
        self.calls += 1
        return self._has_open
