"""Порты-шлюзы seasons к данным других доменов.

``DisputeGuard`` — проверка открытых споров по событиям сезона перед
финализацией. Боевой адаптер — ``ResolutionDisputeGuard`` (домен resolutions);
он связывается с seasons в composition root scoring/воркера, где направление
зависимостей ``scoring → seasons`` сохранено (дизайн §6.4).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class DisputeGuard(Protocol):
    """Есть ли открытые споры по событиям сезона (блокируют финализацию)."""

    async def has_open_disputes(self, season_id: uuid.UUID) -> bool:
        """``True``, если по событиям сезона есть незакрытые споры."""
        ...


@runtime_checkable
class PredictionGuard(Protocol):
    """Есть ли по сезону хоть один прогноз (интеграционный шов к predictions).

    Нужен исправлению правил активного сезона: пока прогнозов нет, полагаться
    на объявленные условия некому, и пороги ещё можно поправить.
    """

    async def has_predictions(self, season_id: uuid.UUID) -> bool: ...
