"""Порт scoring к данным сезонов (резолв slug + замороженный конфиг лиги).

Реализуется адаптером на стороне scoring, читающим таблицу ``seasons`` напрямую
(монолит, единая БД) — так сохраняется направление зависимостей ``scoring →
seasons`` без обратной связи. Используется ``RecomputeRatings`` (квалификация),
сезонным лидербордом (резолв slug) и чтением деталей квалификации.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.modules.scoring.application.dto import SeasonConfigView


@runtime_checkable
class SeasonConfigGateway(Protocol):
    """Чтение сезона: резолв slug→id и замороженная конфигурация лиги."""

    async def resolve_slug(self, slug: str) -> uuid.UUID | None:
        """id сезона по slug или ``None``, если сезона нет."""
        ...

    async def get_config(self, season_id: uuid.UUID) -> SeasonConfigView | None:
        """Статус + снапшот ``LeagueConfig`` сезона или ``None``, если нет."""
        ...
