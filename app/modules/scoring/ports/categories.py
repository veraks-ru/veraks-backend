"""Порт резолва категорий по id (зависимость к events, для сводки профиля).

Рейтинги хранят только ``category_id``; сводка профиля показывает
человекочитаемые slug/название — берёт их через этот порт, а не пересчитывает
и не хранит копию у себя. Домен scoring об устройстве events не знает.

TODO(scoring-integration): прямое чтение соседней таблицы в монолите; заменить
сетевым контрактом при выделении events в отдельный сервис.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.modules.scoring.application.dto import CategoryRef


@runtime_checkable
class CategoryDirectory(Protocol):
    """Резолв набора ``category_id`` в название/slug одним запросом."""

    async def list_by_ids(
        self, category_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, CategoryRef]:
        """Категории из ``category_ids``, найденные в справочнике (по id)."""
        ...
