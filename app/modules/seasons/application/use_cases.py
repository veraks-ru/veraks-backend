"""Use-cases домена seasons.

Каждый класс — одна бизнес-операция; зависимости только через порты
(конструктор), поэтому use-cases изолированы от FastAPI/SQLAlchemy и
покрываются юнит-тестами с фейками.

Операции жизненного цикла, не требующие данных рейтингов:
  * :class:`CreateSeason` / :class:`UpdateSeason` — заведение и правка
    сезона (правка — только пока ``upcoming``);
  * :class:`ActivateSeason` — ``upcoming → active`` с заморозкой
    :class:`LeagueConfig` (снапшот передаётся извне — ацикличность);
  * :class:`ListSeasons` / :class:`GetSeason` — чтения.

Финализация (``active → finished``) требует финального пересчёта рейтингов и
поэтому координируется в домене scoring (он вправе зависеть от seasons, не
наоборот) — см. ``scoring.application.use_cases.FinalizeSeason``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.modules.identity.domain.entities import UserRole
from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.errors import (
    InvalidSeasonDataError,
    InvalidSeasonTransitionError,
    SeasonNotFoundError,
    SeasonSlugTakenError,
)
from app.modules.seasons.domain.policies import (
    ensure_can_manage_seasons,
    ensure_can_transition,
)
from app.modules.seasons.domain.value_objects import LeagueConfig
from app.modules.seasons.ports.clock import Clock
from app.modules.seasons.ports.repositories import SeasonRepository


def _ensure_window(starts_at: datetime, ends_at: datetime) -> None:
    """Проверяет корректность окна сезона (начало строго раньше конца)."""
    if starts_at >= ends_at:
        raise InvalidSeasonDataError("Начало сезона должно быть раньше конца")


class CreateSeason:
    """Заводит новый сезон в статусе ``upcoming`` (editor/admin)."""

    def __init__(self, *, repo: SeasonRepository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    async def execute(
        self,
        *,
        slug: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        actor_role: UserRole,
    ) -> Season:
        """Создаёт сезон; поднимает при отсутствии прав/занятом slug/окне."""
        ensure_can_manage_seasons(actor_role)
        _ensure_window(starts_at, ends_at)
        if await self._repo.get_by_slug(slug) is not None:
            raise SeasonSlugTakenError(f"Slug сезона уже занят: {slug}")
        now = self._clock.now()
        season = Season(
            slug=slug,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            status=SeasonStatus.UPCOMING,
            created_at=now,
            updated_at=now,
        )
        await self._repo.add(season)
        return season


class UpdateSeason:
    """Правит метаданные сезона, пока он ещё не активирован (editor/admin)."""

    def __init__(self, *, repo: SeasonRepository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    async def execute(
        self,
        *,
        season_id: uuid.UUID,
        actor_role: UserRole,
        title: str | None = None,
        slug: str | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> Season:
        """Обновляет поля сезона в статусе ``upcoming``."""
        ensure_can_manage_seasons(actor_role)
        season = await self._repo.get_by_id(season_id)
        if season is None:
            raise SeasonNotFoundError("Сезон не найден")
        if season.status is not SeasonStatus.UPCOMING:
            raise InvalidSeasonTransitionError(
                "Правка сезона доступна только до активации (upcoming)"
            )
        if slug is not None and slug != season.slug:
            if await self._repo.get_by_slug(slug) is not None:
                raise SeasonSlugTakenError(f"Slug сезона уже занят: {slug}")
            season.slug = slug
        if title is not None:
            season.title = title
        new_starts = starts_at if starts_at is not None else season.starts_at
        new_ends = ends_at if ends_at is not None else season.ends_at
        _ensure_window(new_starts, new_ends)
        season.starts_at = new_starts
        season.ends_at = new_ends
        season.updated_at = self._clock.now()
        await self._repo.update(season)
        return season


class ActivateSeason:
    """Активирует сезон (``upcoming → active``), замораживая ``config`` (admin).

    ``config`` — снапшот правил лиги, сформированный вызывающим слоем
    (composition root знает дефолты scoring); домен seasons его не вычисляет.
    Идемпотентна: повторная активация активного сезона ничего не меняет.
    """

    def __init__(self, *, repo: SeasonRepository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    async def execute(
        self,
        *,
        season_id: uuid.UUID,
        config: LeagueConfig,
        actor_role: UserRole,
    ) -> Season:
        ensure_can_transition(actor_role)
        season = await self._repo.get_by_id(season_id)
        if season is None:
            raise SeasonNotFoundError("Сезон не найден")
        if season.activate(config, now=self._clock.now()):
            await self._repo.update(season)
        return season


class ListSeasons:
    """Список сезонов (опц. фильтр по статусу) — публичное чтение."""

    def __init__(self, *, repo: SeasonRepository) -> None:
        self._repo = repo

    async def execute(self, *, status: SeasonStatus | None = None) -> list[Season]:
        return await self._repo.list(status=status)


class GetSeason:
    """Сезон по slug — публичное чтение."""

    def __init__(self, *, repo: SeasonRepository) -> None:
        self._repo = repo

    async def execute(self, *, slug: str) -> Season:
        season = await self._repo.get_by_slug(slug)
        if season is None:
            raise SeasonNotFoundError(f"Сезон не найден: {slug}")
        return season
