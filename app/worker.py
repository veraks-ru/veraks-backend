"""ARQ-воркер: фоновый скоринг событий, пересчёт рейтингов и roll сезонов.

Запуск: ``arq app.worker.WorkerSettings``.

Каждая задача открывает собственную сессию через :func:`session_scope` (одна
задача = одна транзакция, атомарность — дизайн §6.2) и собирает use-cases из
адаптеров (воркер — это композит-рут для фона, как ``api/dependencies.py`` для
HTTP). Тяжёлая бизнес-логика живёт в тестируемых use-cases/координаторах
(``RecomputeRatings``, ``FinalizeSeason``, ``RollSeasons``); здесь — только
тонкая обвязка, расписание и постановка задач.

Это композит-рут, которому разрешено знать оба домена (scoring и seasons), —
именно здесь формируется снапшот «боевых» правил лиги при авто-активации.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from arq import cron
from arq.connections import ArqRedis, RedisSettings

from app.config import get_settings
from app.db.session import session_scope
from app.modules.scoring.adapters.clock import SystemClock
from app.modules.scoring.adapters.rating_repository import SqlAlchemyRatingRepository
from app.modules.scoring.adapters.scoring_gateway import (
    SqlAlchemyEventScoringGateway,
    SqlAlchemyPredictionScoreWriter,
)
from app.modules.scoring.adapters.season_config_gateway import (
    SqlAlchemySeasonConfigGateway,
)
from app.modules.scoring.application.seasons_coordination import (
    FinalizeSeason,
    RollSeasons,
)
from app.modules.scoring.application.use_cases import RecomputeRatings, ScoreEvent
from app.modules.seasons.adapters.dispute_guard import AlwaysAllowsDisputeGuard
from app.modules.seasons.adapters.season_repository import SqlAlchemySeasonRepository

logger = logging.getLogger(__name__)


class ArqTaskScheduler:
    """Реализация ``TaskScheduler`` поверх пула arq (постановка ``score_event``)."""

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue_score_event(self, event_id: uuid.UUID) -> None:
        await self._pool.enqueue_job("score_event", str(event_id))


# ── Задачи ───────────────────────────────────────────────────────────────────


async def score_event(_ctx: dict[Any, Any], event_id: str) -> int:
    """Пер-прогнозный Brier разрешённого события (идемпотентно)."""
    async with session_scope() as session:
        clock = SystemClock()
        uc = ScoreEvent(
            gateway=SqlAlchemyEventScoringGateway(session, clock),
            writer=SqlAlchemyPredictionScoreWriter(session),
            clock=clock,
        )
        scored = await uc.execute(event_id=uuid.UUID(event_id))
    logger.info("score_event %s: %d predictions scored", event_id, scored)
    return scored


async def recompute_ratings(_ctx: dict[Any, Any], season_id: str | None = None) -> int:
    """Полный пересчёт материализованных рейтингов (ночной/по запросу)."""
    async with session_scope() as session:
        clock = SystemClock()
        uc = RecomputeRatings(
            gateway=SqlAlchemyEventScoringGateway(session, clock),
            ratings=SqlAlchemyRatingRepository(session),
            clock=clock,
            season_config=SqlAlchemySeasonConfigGateway(session),
        )
        upserted = await uc.execute(
            season_id=uuid.UUID(season_id) if season_id else None
        )
    logger.info("recompute_ratings: %d ratings upserted", upserted)
    return upserted


async def season_roll(_ctx: dict[Any, Any]) -> None:
    """Таймерный переход сезонов: активация наступивших, финализация истёкших.

    Авто-финализация управляется флагом ``seasons_auto_finalize`` (по умолчанию
    ВЫКЛ, пока ``DisputeGuard`` — заглушка; дизайн §6.4/§6.5).
    """
    settings = get_settings()
    async with session_scope() as session:
        clock = SystemClock()
        seasons = SqlAlchemySeasonRepository(session)
        ratings = SqlAlchemyRatingRepository(session)
        recompute = RecomputeRatings(
            gateway=SqlAlchemyEventScoringGateway(session, clock),
            ratings=ratings,
            clock=clock,
            season_config=SqlAlchemySeasonConfigGateway(session),
        )
        finalize = FinalizeSeason(
            seasons=seasons,
            dispute_guard=AlwaysAllowsDisputeGuard(),
            recompute=recompute,
            ratings=ratings,
            clock=clock,
        )
        roll = RollSeasons(
            seasons=seasons,
            finalize=finalize,
            clock=clock,
            auto_finalize=settings.seasons_auto_finalize,
        )
        await roll.execute()


class WorkerSettings:
    """Настройки arq-воркера: задачи и расписание."""

    functions = [score_event, recompute_ratings, season_roll]
    cron_jobs = [
        # Ночной полный пересчёт рейтингов.
        # ``cron`` типизирован под слишком широкий ``WorkerCoroutine``
        # (``*args/**kwargs``); типизированные задачи не подходят структурно —
        # известная особенность arq, поэтому точечный ignore.
        cron(recompute_ratings, hour=3, minute=0),  # type: ignore[arg-type]
        # Roll сезонов каждые 15 минут (активация наступивших; финализация —
        # только если включён ``seasons_auto_finalize``).
        cron(season_roll, minute={0, 15, 30, 45}),  # type: ignore[arg-type]
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
