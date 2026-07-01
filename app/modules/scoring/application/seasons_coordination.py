"""Кросс-доменные координаторы scoring ↔ seasons (финализация и roll сезонов).

Живут в scoring, потому что финализация требует финального пересчёта рейтингов
(scoring), а seasons по правилу зависимостей знать о scoring не может. Здесь —
единственное место, где обе стороны сводятся вместе; направление зависимостей
``scoring → seasons`` сохранено.

Надёжность финализации (дизайн §6) обеспечивается совместно с адаптером и
вызывающим (воркер/эндпоинт):
  * блокировка строки сезона ``FOR UPDATE`` (``lock_for_finalize``) сериализует
    параллельные финализации (§6.1);
  * идемпотентность: финализация уже завершённого сезона — no-op (§6.1);
  * атомарность: пересчёт, неизменяемая запись и флаг ``finished`` — в одной
    транзакции вызывающего (он коммитит) (§6.2–6.3);
  * запрет финализации при открытых спорах через ``DisputeGuard`` (§6.4).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import Protocol

from app.modules.scoring.application.dto import FinalizeResult
from app.modules.scoring.application.use_cases import (
    RecalibrateSeasonGradations,
    RecomputeRatings,
)
from app.modules.scoring.domain.constants import DEFAULT_GRADATIONS
from app.modules.scoring.domain.entities import ScopeType
from app.modules.scoring.ports.clock import Clock
from app.modules.scoring.ports.repositories import RatingRepository
from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.errors import (
    InvalidSeasonDataError,
    InvalidSeasonTransitionError,
    SeasonFinalizationBlockedError,
    SeasonNotFoundError,
)
from app.modules.seasons.domain.value_objects import (
    LeagueConfig,
    SeasonFinalization,
    SeasonFinalizationEntry,
)
from app.modules.seasons.ports.gateways import DisputeGuard
from app.modules.seasons.ports.repositories import SeasonRepository

logger = logging.getLogger(__name__)

# Снимок финализации читает все сезонные рейтинги; верхняя граница — backstop.
_SNAPSHOT_LIMIT = 1_000_000


class FinalizeSeason:
    """Финализация сезона: ``active → finished`` с пересчётом и снапшотом призёров."""

    def __init__(
        self,
        *,
        seasons: SeasonRepository,
        dispute_guard: DisputeGuard,
        recompute: RecomputeRatings,
        ratings: RatingRepository,
        clock: Clock,
    ) -> None:
        self._seasons = seasons
        self._dispute_guard = dispute_guard
        self._recompute = recompute
        self._ratings = ratings
        self._clock = clock

    async def execute(self, *, season_id: uuid.UUID) -> FinalizeResult:
        """Финализирует сезон идемпотентно; пишет неизменяемую запись.

        Выполняется в транзакции вызывающего (он коммитит) — пересчёт, запись
        финализации и перевод статуса либо применяются целиком, либо
        откатываются вместе (атомарность, §6.2).
        """
        season = await self._seasons.lock_for_finalize(season_id)
        if season is None:
            raise SeasonNotFoundError("Сезон не найден")
        if season.status is SeasonStatus.FINISHED:
            # Идемпотентность: уже завершён — no-op, без повторного пересчёта.
            return FinalizeResult(
                finalized=False, qualified_count=0, total_participants=0
            )
        if season.status is not SeasonStatus.ACTIVE:
            raise InvalidSeasonTransitionError(
                "Финализировать можно только активный сезон"
            )
        if season.league_config is None:  # pragma: no cover - инвариант активации
            raise InvalidSeasonTransitionError(
                "У активного сезона нет замороженного league_config"
            )
        if await self._dispute_guard.has_open_disputes(season_id):
            raise SeasonFinalizationBlockedError(
                "Нельзя финализировать сезон с открытыми спорами по его событиям"
            )

        # Финальный пересчёт рейтингов сезона (та же транзакция).
        await self._recompute.execute(season_id=season_id)
        standings = await self._ratings.leaderboard(
            ScopeType.SEASON, season_id, limit=_SNAPSHOT_LIMIT
        )
        qualified = [r for r in standings if r.qualified is True]

        now = self._clock.now()
        season.finalize(now=now)  # active → finished (инвариант перехода)
        finalization = SeasonFinalization(
            season_id=season_id,
            league_config=season.league_config,
            qualified_count=len(qualified),
            total_participants=len(standings),
            finalized_at=now,
        )
        entries = [
            SeasonFinalizationEntry(
                user_id=r.user_id,
                rank=r.rank,
                skill_score=r.skill_score,
                mean_brier=r.mean_brier,
                calibration_error=r.calibration_error,
                n_resolved=r.n_resolved,
            )
            for r in qualified
        ]
        await self._seasons.append_finalization(finalization, entries)
        await self._seasons.update(season)
        logger.info(
            "Season %s finalized: %d/%d qualified",
            season_id,
            len(qualified),
            len(standings),
        )
        return FinalizeResult(
            finalized=True,
            qualified_count=len(qualified),
            total_participants=len(standings),
        )


class LeagueConfigProvider(Protocol):
    """Источник замороженного ``LeagueConfig`` для активируемого сезона."""

    async def config_for(self, season: Season) -> LeagueConfig:
        ...


class DefaultLeagueConfigProvider:
    """Нейтральный провайдер: всегда боевые дефолты (``LeagueConfig.default``).

    Обратная совместимость: активация без рекалибровки (как было раньше).
    """

    async def config_for(self, season: Season) -> LeagueConfig:  # noqa: ARG002
        return LeagueConfig.default()


class RecalibratingLeagueConfigProvider:
    """Провайдер конфига активации с межсезонной рекалибровкой сетки градаций.

    При активации сезона берёт последний ЗАВЕРШЁННЫЙ сезон, пересчитывает по
    его популяции сетку «градация → вероятность» изотонической регрессией
    (``RecalibrateSeasonGradations``) и морозит новую сетку в ``LeagueConfig``
    нового сезона (пороги/усадка — боевые дефолты). Так рекалибровка применяется
    строго между сезонами и публикуется на весь сезон вперёд.

    Безопасный фолбэк на ``LeagueConfig.default()``: нет завершённых сезонов,
    сетка получилась не той длины / неубывающая (ничьи PAV) / вне ``(0, 1)`` —
    любой из этих случаев отдаёт нейтральную дефолтную сетку без падения.
    """

    def __init__(
        self,
        *,
        seasons: SeasonRepository,
        recalibrate: RecalibrateSeasonGradations,
    ) -> None:
        self._seasons = seasons
        self._recalibrate = recalibrate

    async def config_for(self, season: Season) -> LeagueConfig:
        base = LeagueConfig.default()
        prev = await self._latest_finished(before=season)
        if prev is None:
            return base
        proposal = await self._recalibrate.execute(season_id=prev.id)
        grid = tuple(item.fitted for item in proposal)
        if len(grid) != len(DEFAULT_GRADATIONS):
            logger.info(
                "Recalibration for season %s produced %d grades (need %d) — "
                "using default grid",
                season.id,
                len(grid),
                len(DEFAULT_GRADATIONS),
            )
            return base
        try:
            config = replace(base, gradation_map=grid)
        except InvalidSeasonDataError:
            # Ничьи PAV (нестрогий рост) или значения на границе 0/1 — фолбэк.
            logger.info(
                "Recalibrated grid %s for season %s is not a valid grid — "
                "using default",
                grid,
                season.id,
            )
            return base
        logger.info(
            "Season %s activated with recalibrated grid %s (from season %s)",
            season.id,
            grid,
            prev.id,
        )
        return config

    async def _latest_finished(self, *, before: Season) -> Season | None:
        """Последний завершённый сезон (по ``ends_at``), кроме самого ``before``."""
        finished = [
            s
            for s in await self._seasons.list(status=SeasonStatus.FINISHED)
            if s.id != before.id
        ]
        if not finished:
            return None
        return max(finished, key=lambda s: s.ends_at)


class RollSeasons:
    """Таймерный переход сезонов: активация наступивших, финализация истёкших.

    Активация наступивших ``upcoming`` сезонов безопасна и включена всегда.
    Авто-финализация истёкших ``active`` сезонов управляется флагом
    ``auto_finalize`` (по умолчанию ВКЛ): боевой ``DisputeGuard``
    (``ResolutionDisputeGuard``) блокирует финализацию поверх открытых споров,
    поэтому таймерное авто-закрытие безопасно (дизайн §6.4/§6.5). Флаг оставлен
    для возможности временно перевести закрытие сезонов в ручной режим.

    ``config_provider`` формирует замороженный ``LeagueConfig`` активируемого
    сезона (по умолчанию — боевые дефолты; боевой провайдер добавляет
    межсезонную рекалибровку сетки градаций).
    """

    def __init__(
        self,
        *,
        seasons: SeasonRepository,
        finalize: FinalizeSeason,
        clock: Clock,
        auto_finalize: bool = True,
        config_provider: LeagueConfigProvider | None = None,
    ) -> None:
        self._seasons = seasons
        self._finalize = finalize
        self._clock = clock
        self._auto_finalize = auto_finalize
        self._config_provider = config_provider or DefaultLeagueConfigProvider()

    async def execute(self) -> list[uuid.UUID]:
        """Прокатывает переходы; возвращает id активированных сезонов.

        Список активированных нужен композит-руту (воркеру), чтобы разнести
        дивизионы нового сезона по итогам предыдущего — без обратной зависимости
        scoring→leagues (её делает воркер).
        """
        now = self._clock.now()
        activated: list[uuid.UUID] = []

        for season in await self._seasons.list(status=SeasonStatus.UPCOMING):
            if season.starts_at > now:
                continue
            config = await self._config_provider.config_for(season)
            if season.activate(config, now=now):
                await self._seasons.update(season)
                activated.append(season.id)
                logger.info("Season %s auto-activated", season.id)

        if not self._auto_finalize:
            logger.info(
                "Auto-finalization disabled by config — skipping timer-based "
                "season finalization (manual admin finalization only)."
            )
            return activated

        for season in await self._seasons.list(status=SeasonStatus.ACTIVE):
            if season.ends_at <= now:
                await self._finalize.execute(season_id=season.id)
        return activated
