"""Порт хранилища сезонов и журнала финализаций.

Прикладной слой зависит от этого протокола, а не от SQLAlchemy. Метод
``lock_for_finalize`` в Postgres-адаптере выполняет ``SELECT … FOR UPDATE``
строки сезона — это сериализует параллельные финализации (дизайн §6.1).
``append_finalization`` пишет неизменяемую запись (родитель + строки-на-
участника) в той же транзакции, что и финальный пересчёт (§6.2–6.3).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.value_objects import (
    SeasonFinalization,
    SeasonFinalizationEntry,
)


@runtime_checkable
class SeasonRepository(Protocol):
    """Хранилище сезонов (ключ уникальности — ``UNIQUE(slug)``)."""

    async def add(self, season: Season) -> None:
        """Сохраняет новый сезон."""
        ...

    async def get_by_id(self, season_id: uuid.UUID) -> Season | None:
        """Сезон по id или ``None``."""
        ...

    async def get_by_slug(self, slug: str) -> Season | None:
        """Сезон по slug (citext, без учёта регистра) или ``None``."""
        ...

    async def list(self, *, status: SeasonStatus | None = None) -> list[Season]:
        """Сезоны, опционально отфильтрованные по статусу."""
        ...

    async def update(self, season: Season) -> None:
        """Сохраняет изменения существующего сезона."""
        ...

    async def lock_for_finalize(self, season_id: uuid.UUID) -> Season | None:
        """Читает сезон с блокировкой строки (``FOR UPDATE``) или ``None``.

        Блокировка держится до конца транзакции вызывающего и сериализует
        параллельные финализации одного сезона.
        """
        ...

    async def append_finalization(
        self,
        finalization: SeasonFinalization,
        entries: Sequence[SeasonFinalizationEntry],
    ) -> None:
        """Пишет неизменяемую запись финализации (родитель + строки участников)."""
        ...
