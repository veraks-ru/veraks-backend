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
from app.modules.notifications.adapters.emitter import PushingNotificationEmitter
from app.modules.notifications.adapters.goctopus import GoctopusPusher
from app.modules.notifications.adapters.repository import (
    SqlAlchemyNotificationRepository,
)
from app.modules.events.adapters.clock import SystemClock as EventsClock
from app.modules.events.adapters.repository import SqlAlchemyEventRepository
from app.modules.events.application.use_cases import CloseExpiredEvents
from app.modules.predictions.adapters.clock import SystemClock as PredictionsClock
from app.modules.predictions.adapters.repository import (
    SqlAlchemyPredictionRepository,
)
from app.modules.predictions.application.use_cases import LockEventPredictions
from app.modules.billing.adapters.repositories import SqlAlchemyLedgerRepository
from app.modules.billing.application.use_cases import ReconcileLedger
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
    RecalibratingLeagueConfigProvider,
    RollSeasons,
)
from app.modules.scoring.application.use_cases import (
    RecalibrateSeasonGradations,
    RecomputeRatings,
    ScoreEvent,
)
from app.modules.resolutions.adapters.clock import SystemClock as ResolutionsClock
from app.modules.resolutions.adapters.dispute_guard import ResolutionDisputeGuard
from app.modules.resolutions.adapters.event_gateway import (
    SqlAlchemyEventResolutionGateway,
)
from app.modules.resolutions.adapters.repositories import (
    SqlAlchemyDisputeRepository,
    SqlAlchemyResolutionRepository,
    SqlAlchemyScoringDispatchRepository,
)
from app.modules.resolutions.application.use_cases import CloseDisputeWindows
from app.modules.seasons.adapters.season_repository import SqlAlchemySeasonRepository
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail

logger = logging.getLogger(__name__)


class ArqTaskScheduler:
    """Реализация ``TaskScheduler`` поверх пула arq (постановка ``score_event``)."""

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue_score_event(self, event_id: uuid.UUID) -> None:
        await self._pool.enqueue_job("score_event", str(event_id))


# ── Задачи ───────────────────────────────────────────────────────────────────


async def score_event(_ctx: dict[Any, Any], event_id: str) -> int:
    """Пер-прогнозный Brier разрешённого события + инкрементальный пересчёт.

    В одной транзакции: сначала Brier по прогнозам события, затем пересчёт
    рейтингов только для затронутых срезов (global + категория + сезон
    события), а не всех сразу — быстрый апдейт по горячему следу разрешения.
    Ночной ``recompute_ratings`` остаётся полным бэкстопом корректности.
    """
    eid = uuid.UUID(event_id)
    async with session_scope() as session:
        clock = SystemClock()
        gateway = SqlAlchemyEventScoringGateway(session, clock)
        settings = get_settings()
        uc = ScoreEvent(
            gateway=gateway,
            writer=SqlAlchemyPredictionScoreWriter(session),
            clock=clock,
            notifier=PushingNotificationEmitter(
                SqlAlchemyNotificationRepository(session),
                GoctopusPusher(settings.realtime),
            ),
        )
        scored = await uc.execute(event_id=eid)

        event = await gateway.get_resolved_event(eid)
        scopes = RecomputeRatings.touched_scopes(
            category_id=event.category_id, season_id=event.season_id
        )
        recompute = RecomputeRatings(
            gateway=gateway,
            ratings=SqlAlchemyRatingRepository(session),
            clock=clock,
            season_config=SqlAlchemySeasonConfigGateway(session),
        )
        upserted = await recompute.execute(scopes=scopes)
    logger.info(
        "score_event %s: %d predictions scored, %d ratings recomputed",
        event_id,
        scored,
        upserted,
    )
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
    ВКЛ; боевой ``ResolutionDisputeGuard`` блокирует закрытие сезона с открытыми
    спорами, дизайн §6.4/§6.5).
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
            dispute_guard=ResolutionDisputeGuard(SqlAlchemyDisputeRepository(session)),
            recompute=recompute,
            ratings=ratings,
            clock=clock,
        )
        config_provider = RecalibratingLeagueConfigProvider(
            seasons=seasons,
            recalibrate=RecalibrateSeasonGradations(
                gateway=SqlAlchemyEventScoringGateway(session, clock)
            ),
        )
        roll = RollSeasons(
            seasons=seasons,
            finalize=finalize,
            clock=clock,
            auto_finalize=settings.seasons_auto_finalize,
            config_provider=config_provider,
        )
        await roll.execute()


async def close_dispute_windows(ctx: dict[Any, Any]) -> int:
    """Закрывает истёкшие окна оспаривания и ставит скоринг по событиям.

    Для каждого ``resolved``-события с истёкшим окном без открытых споров,
    если скоринг по текущей резолюции ещё не ставился, фиксирует диспатч и
    ставит ``score_event``. Идемпотентно (маркер диспатча) — повторный тик не
    дублирует постановку.
    """
    async with session_scope() as session:
        clock = ResolutionsClock()
        uc = CloseDisputeWindows(
            events=SqlAlchemyEventResolutionGateway(session),
            resolutions=SqlAlchemyResolutionRepository(session),
            disputes=SqlAlchemyDisputeRepository(session),
            dispatches=SqlAlchemyScoringDispatchRepository(session),
            tasks=ArqTaskScheduler(ctx["redis"]),
            audit=SqlAlchemyAuditTrail(session),
            clock=clock,
        )
        dispatched = await uc.execute()
    logger.info("close_dispute_windows: %d events enqueued for scoring", dispatched)
    return dispatched


async def close_expired_events(_ctx: dict[Any, Any]) -> int:
    """Авто-закрытие приёма по истёкшему ``closes_at`` и блокировка прогнозов.

    Композит-рут фона, которому разрешено знать оба домена (events и
    predictions): закрывает просроченные ``open``-события (домен events) и по
    каждому закрытому блокирует прогнозы (домен predictions) — финальный рубеж
    инварианта «после дедлайна правок нет». Идемпотентно на обоих шагах.
    """
    async with session_scope() as session:
        closed = await CloseExpiredEvents(
            events=SqlAlchemyEventRepository(session),
            clock=EventsClock(),
            audit=SqlAlchemyAuditTrail(session),
        ).execute()
        lock = LockEventPredictions(
            predictions=SqlAlchemyPredictionRepository(session),
            clock=PredictionsClock(),
        )
        for event_id in closed:
            await lock.execute(event_id=event_id)
    logger.info("close_expired_events: %d events auto-closed", len(closed))
    return len(closed)


async def reconcile(_ctx: dict[Any, Any]) -> int:
    """Сверка целостности журнала: баланс книг по каждой кассе.

    Двойная запись гарантирует ``debit == credit`` по кассе; расхождение —
    признак повреждения данных в обход триггеров. Тревога логируется как ERROR.
    TODO(billing-infra): добавить сверку с сеттлментами провайдера/банка.
    """
    imbalanced = 0
    async with session_scope() as session:
        reports = await ReconcileLedger(
            ledger=SqlAlchemyLedgerRepository(session)
        ).execute()
    for report in reports:
        if not report.balanced:
            imbalanced += 1
            logger.error(
                "reconcile: ledger %s IMBALANCED — debit=%d credit=%d",
                report.ledger_type.value,
                report.total_debit_kopecks,
                report.total_credit_kopecks,
            )
    logger.info("reconcile: %d/%d ledgers imbalanced", imbalanced, len(reports))
    return imbalanced


class WorkerSettings:
    """Настройки arq-воркера: задачи и расписание."""

    functions = [
        score_event,
        recompute_ratings,
        season_roll,
        close_dispute_windows,
        close_expired_events,
        reconcile,
    ]
    cron_jobs = [
        # Ночной полный пересчёт рейтингов.
        # ``cron`` типизирован под слишком широкий ``WorkerCoroutine``
        # (``*args/**kwargs``); типизированные задачи не подходят структурно —
        # известная особенность arq, поэтому точечный ignore.
        cron(recompute_ratings, hour=3, minute=0),  # type: ignore[arg-type]
        # Roll сезонов каждые 15 минут (активация наступивших; финализация —
        # только если включён ``seasons_auto_finalize``).
        cron(season_roll, minute={0, 15, 30, 45}),  # type: ignore[arg-type]
        # Закрытие окон оспаривания и постановка скоринга — каждые 5 минут.
        cron(close_dispute_windows, minute=set(range(0, 60, 5))),
        # Авто-закрытие приёма по дедлайну (closes_at) — каждую минуту, чтобы
        # приём не оставался открытым заметно дольше серверного дедлайна.
        cron(close_expired_events),  # type: ignore[arg-type]
        # Сверка баланса книг — ежечасно (раннее обнаружение рассогласований).
        cron(reconcile, minute=7),  # type: ignore[arg-type]
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
