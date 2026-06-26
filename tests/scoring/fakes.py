"""In-memory фейки портов scoring для изолированного тестирования use-cases.

Реализуют те же протоколы, что и продакшн-адаптеры, но без Postgres: шлюз
читает заранее заданные разрешённые события, писатель собирает проставленные
Brier-оценки в память, репозиторий рейтингов — простая мапа с эмуляцией
``UNIQUE(user_id, scope_type, scope_id)``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from app.modules.scoring.application.dto import (
    EventScoringStatus,
    PredictionScore,
    SeasonConfigView,
)
from app.modules.scoring.domain.entities import Rating, ScopeType
from app.modules.scoring.domain.value_objects import ResolvedEvent


class FakeClock:
    """Часы с фиксированным временем."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeUserDirectory:
    """Резолв username → user_id в памяти (только «активные»)."""

    def __init__(self, by_username: dict[str, uuid.UUID] | None = None) -> None:
        self._by_username = by_username or {}

    def set(self, username: str, user_id: uuid.UUID) -> None:
        self._by_username[username] = user_id

    async def resolve_username(self, username: str) -> uuid.UUID | None:
        return self._by_username.get(username)


class FakeEventScoringGateway:
    """Шлюз к данным разрешённых событий и калибровочным записям."""

    def __init__(
        self,
        *,
        statuses: dict[uuid.UUID, EventScoringStatus] | None = None,
        events: dict[uuid.UUID, ResolvedEvent] | None = None,
        resolved: Sequence[ResolvedEvent] | None = None,
        user_entries: dict[uuid.UUID, list[tuple[float, int]]] | None = None,
        season_entries: dict[uuid.UUID, list[tuple[float, int]]] | None = None,
    ) -> None:
        self._statuses = statuses or {}
        self._events = events or {}
        self._resolved = list(resolved or [])
        self._user_entries = user_entries or {}
        self._season_entries = season_entries or {}

    async def get_status(self, event_id: uuid.UUID) -> EventScoringStatus:
        return self._statuses.get(
            event_id,
            EventScoringStatus(
                found=False, is_resolved=False, is_final=False, outcome=None
            ),
        )

    async def get_resolved_event(self, event_id: uuid.UUID) -> ResolvedEvent | None:
        return self._events.get(event_id)

    async def list_resolved_events(
        self, *, season_id: uuid.UUID | None = None
    ) -> list[ResolvedEvent]:
        if season_id is None:
            return list(self._resolved)
        return [e for e in self._resolved if e.season_id == season_id]

    async def list_user_calibration_entries(
        self, user_id: uuid.UUID
    ) -> list[tuple[float, int]]:
        return list(self._user_entries.get(user_id, []))

    async def list_season_calibration_entries(
        self, season_id: uuid.UUID
    ) -> list[tuple[float, int]]:
        return list(self._season_entries.get(season_id, []))


class FakeSeasonConfigGateway:
    """Шлюз к конфигурации сезонов: резолв slug и снапшот ``LeagueConfig``."""

    def __init__(
        self,
        *,
        by_slug: dict[str, uuid.UUID] | None = None,
        configs: dict[uuid.UUID, SeasonConfigView] | None = None,
    ) -> None:
        self._by_slug = by_slug or {}
        self._configs = configs or {}

    async def resolve_slug(self, slug: str) -> uuid.UUID | None:
        return self._by_slug.get(slug)

    async def get_config(self, season_id: uuid.UUID) -> SeasonConfigView | None:
        return self._configs.get(season_id)


class FakePredictionScoreWriter:
    """Собирает проставленные Brier-оценки по событиям (без БД)."""

    def __init__(self) -> None:
        self.saved: dict[uuid.UUID, list[PredictionScore]] = {}

    async def save_event_scores(
        self,
        event_id: uuid.UUID,
        scores: Sequence[PredictionScore],
        *,
        now: datetime,
    ) -> int:
        self.saved[event_id] = list(scores)
        return len(scores)


class InMemoryRatingRepository:
    """Хранилище рейтингов в памяти (ключ — ``user_id × scope_type × scope_id``)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[uuid.UUID, ScopeType, uuid.UUID | None], Rating] = {}

    async def upsert_many(self, ratings: Sequence[Rating]) -> int:
        for rating in ratings:
            key = (rating.user_id, rating.scope_type, rating.scope_id)
            self._by_key[key] = rating
        return len(ratings)

    async def leaderboard(
        self,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
        *,
        limit: int = 50,
        offset: int = 0,
        qualified_only: bool = False,
    ) -> list[Rating]:
        rows = [
            r
            for r in self._by_key.values()
            if r.scope_type == scope_type and r.scope_id == scope_id
        ]
        if qualified_only:
            rows = [r for r in rows if r.qualified is True]
        rows.sort(key=lambda r: r.rank)
        return rows[offset : offset + limit]

    async def get_for_user(
        self,
        user_id: uuid.UUID,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
    ) -> Rating | None:
        return self._by_key.get((user_id, scope_type, scope_id))
