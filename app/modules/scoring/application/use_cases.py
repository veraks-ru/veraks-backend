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

import uuid
from dataclasses import dataclass, field

from app.modules.scoring.application.dto import PredictionScore
from app.modules.scoring.domain.calibration import CalibrationReport, calibrate
from app.modules.scoring.domain.constants import MIN_PREDICTORS
from app.modules.scoring.domain.entities import Rating, ScopeType
from app.modules.scoring.domain.errors import (
    EventNotResolvedError,
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


class ScoreEvent:
    """Скоринг события при разрешении: пер-прогнозный Brier (идемпотентно).

    Триггерится фоном после фиксации исхода И закрытия окна оспаривания
    (см. ``EventScoringStatus.is_scoreable``). Эндпоинт разрешения остаётся
    быстрым — тяжёлый проход по тысячам прогнозов уходит в воркер.

    TODO(scoring-infra): вызывается ARQ-воркером ``score_event`` по доменному
    событию resolutions; идемпотентность по ``(event_id, resolution_id)``.
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
    """Накопитель метрик пользователя в одной области за пересчёт."""

    weights: list[float] = field(default_factory=list)
    advantages: list[float] = field(default_factory=list)
    briers: list[float] = field(default_factory=list)
    entries: list[tuple[float, int]] = field(default_factory=list)

    def add(
        self, weight: float, advantage: float, brier_score: float, prob: float, outcome: int
    ) -> None:
        self.weights.append(weight)
        self.advantages.append(advantage)
        self.briers.append(brier_score)
        self.entries.append((prob, outcome))

    @property
    def n(self) -> int:
        return len(self.briers)

    def mean_brier(self) -> float:
        return sum(self.briers) / len(self.briers)


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
    ) -> None:
        self._gateway = gateway
        self._ratings = ratings
        self._clock = clock

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

        now = self._clock.now()
        by_scope: dict[tuple[ScopeType, uuid.UUID | None], list[Rating]] = {}
        for (scope_type, scope_id, user_id), data in acc.items():
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
                    weight, advantage, brier_score, vote.probability, event.outcome
                )


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
    ) -> list[Rating]:
        """Топ области по предрасчитанному рангу."""
        return await self._ratings.leaderboard(
            scope_type, scope_id, limit=limit, offset=offset
        )


class GetUserCalibration:
    """Калибровка профиля пользователя (predicted vs actual по градациям)."""

    def __init__(self, *, gateway: EventScoringGateway) -> None:
        self._gateway = gateway

    async def execute(self, *, user_id: uuid.UUID) -> CalibrationReport:
        """Строит отчёт калибровки по засчитанным прогнозам пользователя."""
        entries = await self._gateway.list_user_calibration_entries(user_id)
        return calibrate(entries)
