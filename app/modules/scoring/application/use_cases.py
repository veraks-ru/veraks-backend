"""Use-cases домена scoring.

Каждый класс — одна бизнес-операция; зависимости только через порты
(конструктор), поэтому use-cases изолированы от FastAPI/SQLAlchemy и
покрываются юнит-тестами с фейками.

Операции:
  * :class:`ScoreEvent` — пер-прогнозный Brier при разрешении события (фон);
  * :class:`RecomputeRatings` — перестроение материализованных рейтингов по
    областям (global/category/season) с ранжированием (фон, идемпотентно);
  * :class:`GetLeaderboard` — чтение готового лидерборда области;
  * :class:`GetUserCalibration` — калибровка профиля (predicted vs actual).

«На чтении Brier не считается никогда»: чтения берут готовые агрегаты, тяжёлый
пересчёт — здесь, в фоновых use-cases.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field

from app.modules.scoring.application.dto import PredictionScore, SeasonConfigView
from app.modules.scoring.domain.calibration import CalibrationReport, calibrate
from app.modules.scoring.domain.constants import MIN_PREDICTORS
from app.modules.scoring.domain.entities import Rating, ScopeType
from app.modules.scoring.domain.errors import (
    EventNotResolvedError,
    RatingNotFoundError,
    ScoringTargetEventNotFoundError,
)
from app.modules.scoring.domain.formulas import (
    brier,
    crowd_advantage,
    event_weight,
    season_rating_from_contributions,
)
from app.modules.scoring.domain.value_objects import ResolvedEvent, quantize_score
from app.modules.scoring.ports.clock import Clock
from app.modules.scoring.ports.gateways import (
    EventScoringGateway,
    PredictionScoreWriter,
)
from app.modules.scoring.ports.repositories import RatingRepository
from app.modules.scoring.ports.season_config import SeasonConfigGateway
from app.modules.seasons.domain.entities import SeasonStatus
from app.modules.seasons.domain.errors import SeasonNotFoundError
from app.modules.seasons.domain.qualification import evaluate_qualification
from app.modules.seasons.domain.value_objects import QualificationResult

logger = logging.getLogger(__name__)


class ScoreEvent:
    """Скоринг события при разрешении: пер-прогнозный Brier (повторно-безопасно).

    Триггерится фоном после фиксации исхода И закрытия окна оспаривания
    (см. ``EventScoringStatus.is_scoreable``). Эндпоинт разрешения остаётся
    быстрым — тяжёлый проход по тысячам прогнозов уходит в воркер.

    Дедупликация *постановки* в очередь — на стороне resolutions
    (``ScoringDispatch`` по ``resolution_id`` + ``on_conflict_do_nothing``);
    сам ``execute`` повторно-безопасен в слабом смысле «latest-wins»:
    ``save_event_scores`` перезаписывает ``brier_score``/``scored_at`` теми же
    значениями, поэтому повторный прогон (ретрай воркера) безвреден. Это же
    свойство обеспечивает корректный ре-скоринг при overturn (новая резолюция →
    новый диспатч → перезапись оценок).
    """

    def __init__(
        self,
        *,
        gateway: EventScoringGateway,
        writer: PredictionScoreWriter,
        clock: Clock,
    ) -> None:
        self._gateway = gateway
        self._writer = writer
        self._clock = clock

    async def execute(self, *, event_id: uuid.UUID) -> int:
        """Считает и записывает Brier по всем прогнозам события.

        Возвращает число оценённых прогнозов. Поднимает
        :class:`ScoringTargetEventNotFoundError` (нет события) или
        :class:`EventNotResolvedError` (исход не финален).
        """
        status = await self._gateway.get_status(event_id)
        if not status.found:
            raise ScoringTargetEventNotFoundError("Событие для скоринга не найдено")
        if not status.is_scoreable:
            raise EventNotResolvedError(
                "Событие не разрешено финально — скоринг невозможен"
            )

        event = await self._gateway.get_resolved_event(event_id)
        if event is None:  # pragma: no cover — статус гарантирует наличие
            raise ScoringTargetEventNotFoundError("Событие для скоринга не найдено")

        outcome = event.outcome
        scores = [
            PredictionScore(
                user_id=vote.user_id,
                brier=quantize_score(brier(vote.probability, outcome)),
            )
            for vote in event.votes
        ]
        return await self._writer.save_event_scores(
            event_id, scores, now=self._clock.now()
        )


@dataclass(slots=True)
class _ScopeAccumulator:
    """Накопитель метрик пользователя в одной области за пересчёт.

    ``categories`` нужен только сезонной области — по нему считается число
    категорий с достаточным числом прогнозов (порог разнообразия квалификации).
    """

    weights: list[float] = field(default_factory=list)
    advantages: list[float] = field(default_factory=list)
    briers: list[float] = field(default_factory=list)
    entries: list[tuple[float, int]] = field(default_factory=list)
    categories: list[uuid.UUID] = field(default_factory=list)

    def add(
        self,
        weight: float,
        advantage: float,
        brier_score: float,
        prob: float,
        outcome: int,
        category_id: uuid.UUID,
    ) -> None:
        self.weights.append(weight)
        self.advantages.append(advantage)
        self.briers.append(brier_score)
        self.entries.append((prob, outcome))
        self.categories.append(category_id)

    @property
    def n(self) -> int:
        return len(self.briers)

    def mean_brier(self) -> float:
        return sum(self.briers) / len(self.briers)

    def category_count(self, m_per_category: int) -> int:
        """Число категорий, где у пользователя ≥ ``m_per_category`` прогнозов."""
        counts = Counter(self.categories)
        return sum(1 for k in counts.values() if k >= m_per_category)

    def total_weight(self) -> float:
        """Суммарный вес сложности (охват) — для порога ``W_MIN``."""
        return math.fsum(self.weights)


class RecomputeRatings:
    """Перестроение материализованных рейтингов из разрешённых событий.

    Для каждой области (global / по категории / по сезону) и каждого
    пользователя считает: ``mean_brier``, ранжирующий ``skill_score`` (усаженное
    превышение над толпой ``R``), ``calibration_error`` (ECE), ``n_resolved`` —
    затем проставляет ранги внутри области и идемпотентно сохраняет.

    Учитываются только «рейтинговые» события (``predictor_count >=
    MIN_PREDICTORS``): на неполной толпе консенсус-бенчмарк ненадёжен.

    TODO(scoring-infra): инкрементальный режим + ночной full recompute; здесь —
    полный пересчёт (фон, идемпотентно).
    """

    def __init__(
        self,
        *,
        gateway: EventScoringGateway,
        ratings: RatingRepository,
        clock: Clock,
        season_config: SeasonConfigGateway,
    ) -> None:
        self._gateway = gateway
        self._ratings = ratings
        self._clock = clock
        self._season_config = season_config

    async def execute(self, *, season_id: uuid.UUID | None = None) -> int:
        """Полный пересчёт рейтингов; возвращает число сохранённых строк."""
        events = await self._gateway.list_resolved_events(season_id=season_id)
        acc: dict[
            tuple[ScopeType, uuid.UUID | None, uuid.UUID], _ScopeAccumulator
        ] = {}

        for event in events:
            if event.predictor_count < MIN_PREDICTORS:
                continue
            self._accumulate_event(event, acc)

        season_views = await self._load_season_configs(acc)
        now = self._clock.now()
        by_scope: dict[tuple[ScopeType, uuid.UUID | None], list[Rating]] = {}
        for (scope_type, scope_id, user_id), data in acc.items():
            qualified = (
                self._evaluate_qualified(scope_id, season_views.get(scope_id), data)
                if scope_type is ScopeType.SEASON and scope_id is not None
                else None
            )
            rating = Rating(
                user_id=user_id,
                scope_type=scope_type,
                scope_id=scope_id,
                mean_brier=quantize_score(data.mean_brier()),
                skill_score=quantize_score(
                    season_rating_from_contributions(data.weights, data.advantages)
                ),
                calibration_error=quantize_score(calibrate(data.entries).ece),
                n_resolved=data.n,
                qualified=qualified,
                updated_at=now,
            )
            by_scope.setdefault((scope_type, scope_id), []).append(rating)

        all_ratings: list[Rating] = []
        for ratings in by_scope.values():
            # Ранжирование «больше skill_score = лучше» (превышение над толпой).
            ratings.sort(key=lambda r: r.skill_score, reverse=True)
            for position, rating in enumerate(ratings, start=1):
                rating.assign_rank(position, now=now)
            all_ratings.extend(ratings)

        if not all_ratings:
            return 0
        return await self._ratings.upsert_many(all_ratings)

    @staticmethod
    def _accumulate_event(
        event: ResolvedEvent,
        acc: dict[tuple[ScopeType, uuid.UUID | None, uuid.UUID], _ScopeAccumulator],
    ) -> None:
        """Раскладывает вклад каждого голоса по областям (global/category/season)."""
        probabilities = event.probabilities()
        weight = event_weight(probabilities, event.outcome)
        scopes: list[tuple[ScopeType, uuid.UUID | None]] = [
            (ScopeType.GLOBAL, None),
            (ScopeType.CATEGORY, event.category_id),
        ]
        if event.season_id is not None:
            scopes.append((ScopeType.SEASON, event.season_id))

        for vote in event.votes:
            advantage = crowd_advantage(vote.probability, probabilities, event.outcome)
            brier_score = brier(vote.probability, event.outcome)
            for scope_type, scope_id in scopes:
                bucket = acc.setdefault(
                    (scope_type, scope_id, vote.user_id), _ScopeAccumulator()
                )
                bucket.add(
                    weight,
                    advantage,
                    brier_score,
                    vote.probability,
                    event.outcome,
                    event.category_id,
                )

    async def _load_season_configs(
        self,
        acc: dict[tuple[ScopeType, uuid.UUID | None, uuid.UUID], _ScopeAccumulator],
    ) -> dict[uuid.UUID, SeasonConfigView | None]:
        """Подгружает конфиги всех сезонов, встретившихся в пересчёте (по одному разу)."""
        season_ids = {
            scope_id
            for (scope_type, scope_id, _user) in acc
            if scope_type is ScopeType.SEASON and scope_id is not None
        }
        views: dict[uuid.UUID, SeasonConfigView | None] = {}
        for sid in season_ids:
            views[sid] = await self._season_config.get_config(sid)
        return views

    @staticmethod
    def _evaluate_qualified(
        season_id: uuid.UUID | None,
        view: SeasonConfigView | None,
        data: _ScopeAccumulator,
    ) -> bool | None:
        """Считает флаг квалификации для сезонной области (или ``None``).

        Различает два случая недоступного конфига (дизайн §4): сезон ещё не
        активирован — нормальный пропуск; активный/завершённый без конфига —
        нарушение инварианта (громкий error-лог, не тихий пропуск).
        """
        if view is None:
            logger.info(
                "Season %s not found while recomputing — qualification skipped",
                season_id,
            )
            return None
        if view.config is None:
            if view.status is SeasonStatus.UPCOMING:
                logger.info(
                    "Season %s is upcoming (no frozen config yet) — "
                    "qualification skipped",
                    season_id,
                )
            else:
                logger.error(
                    "INVARIANT BREACH: season %s is %s but has no frozen "
                    "LeagueConfig — qualification cannot be computed; season "
                    "ratings stop reflecting eligibility until fixed",
                    season_id,
                    view.status.value,
                )
            return None
        cfg = view.config
        result = evaluate_qualification(
            n_resolved=data.n,
            category_count=data.category_count(cfg.m_per_category),
            total_weight=data.total_weight(),
            cfg=cfg,
        )
        return result.qualified


class GetLeaderboard:
    """Чтение готового лидерборда области (ничего не считает на чтении)."""

    def __init__(self, *, ratings: RatingRepository) -> None:
        self._ratings = ratings

    async def execute(
        self,
        *,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
        limit: int = 50,
        offset: int = 0,
        qualified_only: bool = False,
    ) -> list[Rating]:
        """Топ области по предрасчитанному рангу (опц. только квалифицированные)."""
        return await self._ratings.leaderboard(
            scope_type,
            scope_id,
            limit=limit,
            offset=offset,
            qualified_only=qualified_only,
        )


class GetSeasonLeaderboard:
    """Сезонный лидерборд по slug: резолвит сезон и читает готовые рейтинги.

    Резолв slug→id — через ``SeasonConfigGateway`` (направление ``scoring →
    seasons``). ``qualified_only`` оставляет только квалифицированных к призам.
    """

    def __init__(
        self, *, ratings: RatingRepository, season_config: SeasonConfigGateway
    ) -> None:
        self._ratings = ratings
        self._season_config = season_config

    async def execute(
        self,
        *,
        slug: str,
        limit: int = 50,
        offset: int = 0,
        qualified_only: bool = False,
    ) -> tuple[uuid.UUID, list[Rating]]:
        """Возвращает ``(season_id, рейтинги)``; поднимает, если сезон не найден."""
        season_id = await self._season_config.resolve_slug(slug)
        if season_id is None:
            raise SeasonNotFoundError(f"Сезон не найден: {slug}")
        ratings = await self._ratings.leaderboard(
            ScopeType.SEASON,
            season_id,
            limit=limit,
            offset=offset,
            qualified_only=qualified_only,
        )
        return season_id, ratings


class GetSeasonQualification:
    """Разбор квалификации пользователя в сезоне (для UX профиля «почему не»).

    Считает на лету по разрешённым событиям сезона (это редкое профильное
    чтение). Требует активированного сезона с замороженным ``LeagueConfig``;
    иначе — :class:`RatingNotFoundError` (правил ещё нет / сезон не активирован).
    """

    def __init__(
        self,
        *,
        gateway: EventScoringGateway,
        season_config: SeasonConfigGateway,
    ) -> None:
        self._gateway = gateway
        self._season_config = season_config

    async def execute(
        self, *, user_id: uuid.UUID, slug: str
    ) -> QualificationResult:
        season_id = await self._season_config.resolve_slug(slug)
        if season_id is None:
            raise SeasonNotFoundError(f"Сезон не найден: {slug}")
        view = await self._season_config.get_config(season_id)
        if view is None or view.config is None:
            raise RatingNotFoundError(
                "У сезона нет опубликованных правил (не активирован) — "
                "квалификация недоступна"
            )
        cfg = view.config

        events = await self._gateway.list_resolved_events(season_id=season_id)
        weights: list[float] = []
        categories: list[uuid.UUID] = []
        for event in events:
            if event.predictor_count < MIN_PREDICTORS:
                continue
            if not any(vote.user_id == user_id for vote in event.votes):
                continue
            weights.append(event_weight(event.probabilities(), event.outcome))
            categories.append(event.category_id)

        counts = Counter(categories)
        category_count = sum(
            1 for k in counts.values() if k >= cfg.m_per_category
        )
        return evaluate_qualification(
            n_resolved=len(weights),
            category_count=category_count,
            total_weight=math.fsum(weights),
            cfg=cfg,
        )


class GetUserCalibration:
    """Калибровка профиля пользователя (predicted vs actual по градациям)."""

    def __init__(self, *, gateway: EventScoringGateway) -> None:
        self._gateway = gateway

    async def execute(self, *, user_id: uuid.UUID) -> CalibrationReport:
        """Строит отчёт калибровки по засчитанным прогнозам пользователя."""
        entries = await self._gateway.list_user_calibration_entries(user_id)
        return calibrate(entries)
