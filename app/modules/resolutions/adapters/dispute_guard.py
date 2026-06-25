"""Реальная реализация ``DisputeGuard`` для домена seasons.

Заменяет ``AlwaysAllowsDisputeGuard``-заглушку: финализация сезона блокируется,
если по любому его событию есть незакрытый спор. Реализует протокол
``app.modules.seasons.ports.gateways.DisputeGuard`` поверх ``DisputeRepository``
домена resolutions.
"""

from __future__ import annotations

import uuid

from app.modules.resolutions.ports.repositories import DisputeRepository


class ResolutionDisputeGuard:
    """Проверка открытых споров по событиям сезона (через ``disputes``)."""

    def __init__(self, disputes: DisputeRepository) -> None:
        self._disputes = disputes

    async def has_open_disputes(self, season_id: uuid.UUID) -> bool:
        """``True``, если по событиям сезона есть незакрытые споры."""
        return await self._disputes.has_open_in_season(season_id)
