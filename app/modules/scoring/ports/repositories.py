"""Порт репозитория рейтингов (материализованные агрегаты лидербордов).

Прикладной слой зависит от этого протокола; реализация — в
``adapters/rating_repository.py``, в тестах — in-memory фейк. На чтении
лидерборда ничего не считается: всё перестроено фоном (``RecomputeRatings``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from app.modules.scoring.domain.entities import Rating, ScopeType


@runtime_checkable
class RatingRepository(Protocol):
    """Хранилище рейтингов (ключ — ``UNIQUE(user_id, scope_type, scope_id)``)."""

    async def acquire_recompute_lock(self) -> None:
        """Сериализует конкурентные пересчёты рейтингов (advisory-лок транзакции)."""
        ...

    async def upsert_many(self, ratings: Sequence[Rating]) -> int:
        """Идемпотентно сохраняет/обновляет рейтинги; возвращает их число."""
        ...

    async def prune_scopes(
        self,
        scopes: Iterable[tuple[ScopeType, uuid.UUID | None]],
        *,
        keep: Sequence[Rating],
    ) -> int:
        """Удаляет из пересчитанных срезов строки пользователей вне ``keep``."""
        ...

    async def leaderboard(
        self,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
        *,
        limit: int = 50,
        offset: int = 0,
        qualified_only: bool = False,
        min_resolved: int | None = None,
    ) -> list[Rating]:
        """Топ области по предрасчитанному ``rank``.

        ``qualified_only`` — фильтр по сохранённому флагу приза (сезон, см.
        ``Rating.qualified``). ``min_resolved`` — фильтр по числу разрешённых
        прогнозов пользователя (порог участия глобального/категорийного
        лидерборда, см. ``domain.constants.LEADERBOARD_MIN_RESOLVED_*``). В
        обоих случаях ``rank`` отдаётся сохранённым, без пересчёта — после
        фильтрации в последовательности рангов возможны разрывы.
        """
        ...

    async def get_for_user(
        self,
        user_id: uuid.UUID,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
    ) -> Rating | None:
        """Рейтинг пользователя в области (для профиля) или ``None``."""
        ...

    async def list_for_user(self, user_id: uuid.UUID) -> list[Rating]:
        """Все срезы пользователя (global + все категории + все сезоны) одним запросом.

        Для сводки профиля (``GetProfileSummary``) — вместо N обращений
        ``get_for_user`` по каждой области. Пустой список — не ошибка (у
        пользователя без разрешённых событий рейтингов ещё нет).
        """
        ...
