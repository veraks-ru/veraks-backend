"""Use-cases домена scoring.

Каждый класс — одна бизнес-операция; зависимости только через порты
(конструктор), поэтому use-cases изолированы от FastAPI/SQLAlchemy и
покрываются юнит-тестами с фейками.

Операции:
  * :class:`ScoreEvent` — пер-прогнозный Brier при разрешении события (фон);
  * :class:`RecomputeRatings` — перестроение материализованных рейтингов по
    областям (global/category/season) с ранжированием (фон, идемпотентно);
  * :class:`GetLeaderboard` — чтение готового лидерборда области;
  * :class:`GetUserCalibration` — калибровка профиля (predicted vs actual);
  * :class:`GetProfileSummary` — сводка профиля (global/категории/сезон).

«На чтении Brier не считается никогда»: чтения берут готовые агрегаты, тяжёлый
пересчёт — здесь, в фоновых use-cases.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field

from app.modules.scoring.application.dto import (
    GradationRecalibration,
    PredictionScore,
    ProfileCategoryRating,
    ProfileSummary,
    SeasonConfigView,
    SeasonStanding,
)
from app.modules.scoring.domain.calibration import CalibrationReport, calibrate
from app.modules.scoring.domain.constants import (
    DEFAULT_GRADATIONS,
    K_SHRINK,
    LEADERBOARD_MIN_RESOLVED_CATEGORY,
    LEADERBOARD_MIN_RESOLVED_GLOBAL,
    MIN_PREDICTORS,
)
from app.modules.scoring.domain.entities import Rating, ScopeType
from app.modules.scoring.domain.errors import (
    EventNotResolvedError,
    ProfileNotFoundError,
    RatingNotFoundError,
    ScoringTargetEventNotFoundError,
)
from app.modules.scoring.domain.formulas import (
    brier,
    crowd_advantage,
    event_weight,
    season_rating_from_contributions,
)
from app.modules.scoring.domain.recalibration import recalibrate
from app.modules.scoring.domain.value_objects import ResolvedEvent, quantize_score
from app.modules.scoring.ports.categories import CategoryDirectory
from app.modules.scoring.ports.clock import Clock
from app.modules.scoring.ports.gateways import (
    EventScoringGateway,
    PredictionScoreWriter,
)
from app.modules.scoring.ports.notifications import Notifier
from app.modules.scoring.ports.repositories import RatingRepository
from app.modules.scoring.ports.season_config import SeasonConfigGateway
from app.modules.scoring.ports.users import UserDirectory
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
        notifier: Notifier | None = None,
    ) -> None:
        self._gateway = gateway
        self._writer = writer
        self._clock = clock
        self._notifier = notifier

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
        saved = await self._writer.save_event_scores(
            event_id, scores, now=self._clock.now()
        )
        if self._notifier is not None:
            for score in scores:
                await self._notifier.emit(
                    user_id=score.user_id,
                    kind="prediction.scored",
                    title="Ваш прогноз засчитан",
                    body=f"Brier {score.brier}",
                    entity_type="event",
                    entity_id=event_id,
                )
        return saved


@dataclass(slots=True)
class _ScopeAccumulator:
    """Накопитель метрик пользователя в одной области за пересчёт.

    ``categories`` нужен только сезонной области — по нему считается число
    категорий с достаточным числом прогнозов (порог разнообразия квалификации).
    """

    weights: list[float] = field(default_factory=list)
    rating_weights: list[float] = field(default_factory=list)
    advantages: list[float] = field(default_factory=list)
    briers: list[float] = field(default_factory=list)
    entries: list[tuple[float, int]] = field(default_factory=list)
    categories: list[uuid.UUID] = field(default_factory=list)

    def add(
        self,
        weight: float,
        rating_weight: float,
        advantage: float,
        brier_score: float,
        prob: float,
        outcome: int,
        category_id: uuid.UUID,
    ) -> None:
        # ``weight`` — «сырая» сложность события (охват/квалификация); в
        # ``rating_weight`` уже вложен тайм-вейтинг (для сезонного рейтинга).
        self.weights.append(weight)
        self.rating_weights.append(rating_weight)
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

    В сезонной области ранг = **призовое место**: сначала идут квалифицированные
    к призам (``qualified is True``), затем все остальные. Порядок внутри группы
    — по ``skill_score``.

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

    @staticmethod
    def touched_scopes(
        *, category_id: uuid.UUID, season_id: uuid.UUID | None
    ) -> set[tuple[ScopeType, uuid.UUID | None]]:
        """Срезы, чьи рейтинги может сдвинуть разрешение одного события.

        Голос участника разносится в глобальный срез, срез его категории и
        (если есть) срез сезона — ровно те же три ключа, что фанит
        :meth:`_accumulate_event`. Используется для инкрементального пересчёта.
        """
        scopes: set[tuple[ScopeType, uuid.UUID | None]] = {
            (ScopeType.GLOBAL, None),
            (ScopeType.CATEGORY, category_id),
        }
        if season_id is not None:
            scopes.add((ScopeType.SEASON, season_id))
        return scopes

    async def execute(
        self,
        *,
        season_id: uuid.UUID | None = None,
        scopes: set[tuple[ScopeType, uuid.UUID | None]] | None = None,
    ) -> int:
        """Пересчёт рейтингов; возвращает число сохранённых строк.

        ``scopes`` (опц.) ограничивает *запись* только указанными срезами
        ``(тип, id)`` — для инкрементального пересчёта после разрешения одного
        события. События по-прежнему читаются полностью, поэтому ранжирование
        внутри каждого сохраняемого среза остаётся корректным (участвуют все
        предсказатели среза, а не только голосовавшие за это событие).
        """
        # Сериализуем конкурентные пересчёты (score_event ↔ ночной full ↔
        # финализация сезона), чтобы не гонять записи одних строк ratings.
        await self._ratings.acquire_recompute_lock()

        events = await self._gateway.list_resolved_events(season_id=season_id)

        # Конфиги сезонов загружаем ДО накопления: из замороженного LeagueConfig
        # берём порог «рейтинговости» события (min_predictors) и константу усадки
        # (k_shrink) для сезонного среза — как обещано опубликованными правилами.
        event_season_ids = {e.season_id for e in events if e.season_id is not None}
        season_views: dict[uuid.UUID, SeasonConfigView | None] = {
            sid: await self._season_config.get_config(sid)
            for sid in event_season_ids
        }

        def _season_min_predictors(sid: uuid.UUID) -> int:
            view = season_views.get(sid)
            cfg = view.config if view is not None else None
            return cfg.min_predictors if cfg is not None else MIN_PREDICTORS

        acc: dict[
            tuple[ScopeType, uuid.UUID | None, uuid.UUID], _ScopeAccumulator
        ] = {}
        for event in events:
            base_ok = event.predictor_count >= MIN_PREDICTORS
            season_ok = (
                event.season_id is not None
                and event.predictor_count >= _season_min_predictors(event.season_id)
            )
            if not base_ok and not season_ok:
                continue
            self._accumulate_event(event, acc, base_ok=base_ok, season_ok=season_ok)

        if scopes is not None:
            acc = {
                key: data
                for key, data in acc.items()
                if (key[0], key[1]) in scopes
            }

        now = self._clock.now()
        by_scope: dict[tuple[ScopeType, uuid.UUID | None], list[Rating]] = {}
        for (scope_type, scope_id, user_id), data in acc.items():
            is_season = scope_type is ScopeType.SEASON and scope_id is not None
            view = (
                season_views.get(scope_id)
                if scope_type is ScopeType.SEASON and scope_id is not None
                else None
            )
            cfg = view.config if view is not None else None
            # k усадки — из конфига сезона (иначе глобальная константа).
            k_shrink = cfg.k_shrink if cfg is not None else K_SHRINK
            qualified = self._evaluate_qualified(scope_id, view, data) if is_season else None
            rating = Rating(
                user_id=user_id,
                scope_type=scope_type,
                scope_id=scope_id,
                mean_brier=quantize_score(data.mean_brier()),
                skill_score=quantize_score(
                    season_rating_from_contributions(
                        data.rating_weights, data.advantages, k=k_shrink
                    )
                ),
                calibration_error=quantize_score(calibrate(data.entries).ece),
                n_resolved=data.n,
                qualified=qualified,
                updated_at=now,
            )
            by_scope.setdefault((scope_type, scope_id), []).append(rating)

        all_ratings: list[Rating] = []
        for ratings in by_scope.values():
            # Сезонная область: сначала квалифицированные к призам, потом
            # остальные. Ранг сезона — призовое место, поэтому неквалифицированный
            # не может стоять выше призёра, даже если его skill_score выше
            # (усадка не спасает от одного удачного выстрела на сюрпризе —
            # см. scoring_system_design.md §3.2). Для global/category
            # ``qualified`` = None, все строки в одной группе — порядок прежний.
            # Внутри группы: «больше skill_score = лучше» (превышение над толпой);
            # тай-брейк — меньший mean_brier, затем больший n_resolved, затем id.
            ratings.sort(
                key=lambda r: (
                    r.qualified is False,
                    -r.skill_score,
                    r.mean_brier,
                    -r.n_resolved,
                    str(r.user_id),
                )
            )
            for position, rating in enumerate(ratings, start=1):
                rating.assign_rank(position, now=now)
            all_ratings.extend(ratings)

        # Устаревшие строки среза (пользователь выбыл из рейтинга — напр. после
        # overturn или падения ниже порога) удаляем, чтобы не оставались «призраки»
        # с прежним рангом. Только для пересчитанных срезов.
        recomputed_scopes = scopes if scopes is not None else set(by_scope.keys())
        await self._ratings.prune_scopes(recomputed_scopes, keep=all_ratings)

        if not all_ratings:
            return 0
        return await self._ratings.upsert_many(all_ratings)

    @staticmethod
    def _accumulate_event(
        event: ResolvedEvent,
        acc: dict[tuple[ScopeType, uuid.UUID | None, uuid.UUID], _ScopeAccumulator],
        *,
        base_ok: bool,
        season_ok: bool,
    ) -> None:
        """Раскладывает вклад каждого голоса по областям (global/category/season).

        ``base_ok`` — событие проходит базовый порог MIN_PREDICTORS (global/category);
        ``season_ok`` — проходит сезонный порог (min_predictors из конфига сезона).
        Событие может считаться для сезона, но не для global/category (или наоборот).
        """
        probabilities = event.probabilities()
        weight = event_weight(probabilities, event.outcome)
        scopes: list[tuple[ScopeType, uuid.UUID | None]] = []
        if base_ok:
            scopes.append((ScopeType.GLOBAL, None))
            scopes.append((ScopeType.CATEGORY, event.category_id))
        if season_ok and event.season_id is not None:
            scopes.append((ScopeType.SEASON, event.season_id))
        if not scopes:
            return

        for vote in event.votes:
            advantage = crowd_advantage(vote.probability, probabilities, event.outcome)
            brier_score = brier(vote.probability, event.outcome)
            # Тайм-вейтинг в рейтинг не вкладывается (см. scoring_gateway):
            # rating_weight = «сырой» вес сложности события.
            rating_weight = weight * vote.time_weight
            for scope_type, scope_id in scopes:
                bucket = acc.setdefault(
                    (scope_type, scope_id, vote.user_id), _ScopeAccumulator()
                )
                bucket.add(
                    weight,
                    rating_weight,
                    advantage,
                    brier_score,
                    vote.probability,
                    event.outcome,
                    event.category_id,
                )

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


async def _visible_ratings(
    users: UserDirectory, ratings: list[Rating]
) -> list[Rating]:
    """Оставляет строки лидерборда, чьи авторы публично видимы (ACTIVE).

    Рейтинги удалённых/заблокированных аккаунтов остаются в таблице (история
    скоринга append-only и нужна пересчётам), но на витрине такая строка
    выглядела бы как «@<uuid>» с мёртвой ссылкой: публичный профиль отдаётся
    только для ACTIVE. Фильтруем на чтении — тем же приёмом, что доска лучших
    прогнозов события (``predictions.GetEventTopPredictions``).

    ``rank`` у оставшихся не пересчитывается: как и с порогом участия, это
    просто фильтрация строк — в последовательности рангов возможны разрывы.
    """
    if not ratings:
        return []
    active = await users.list_active_ids([r.user_id for r in ratings])
    return [r for r in ratings if r.user_id in active]


class GetLeaderboard:
    """Чтение готового лидерборда области — global/category (ничего не считает).

    ``qualified_only`` здесь — порог УЧАСТИЯ (PRD §4.6): по умолчанию скрывает
    предсказателей с числом разрешённых прогнозов ниже
    ``LEADERBOARD_MIN_RESOLVED_GLOBAL``/``_CATEGORY`` (шум и лёгкая манипуляция
    с одним удачным прогнозом). Это отдельное понятие от сезонной квалификации
    к призам (``Rating.qualified`` — многофакторная, см. ``GetSeasonLeaderboard``).
    Как и там, ``rank`` при фильтрации не пересчитывается — отдаётся
    сохранённый (в выдаче возможны разрывы рангов).

    Независимо от порога из выдачи исключаются неактивные аккаунты
    (см. :func:`_visible_ratings`).
    """

    def __init__(self, *, ratings: RatingRepository, users: UserDirectory) -> None:
        self._ratings = ratings
        self._users = users

    async def execute(
        self,
        *,
        scope_type: ScopeType,
        scope_id: uuid.UUID | None,
        limit: int = 50,
        offset: int = 0,
        qualified_only: bool = True,
    ) -> tuple[list[Rating], int | None]:
        """Возвращает ``(рейтинги, применённый порог n_resolved)``.

        Порог — ``None``, если ``qualified_only=False`` (отдаём всех — для
        админки/отладки). Скрытие неактивных аккаунтов флагом не управляется:
        мёртвая строка не нужна и в админке.
        """
        min_resolved = (
            self._min_resolved(scope_type) if qualified_only else None
        )
        ratings = await self._ratings.leaderboard(
            scope_type,
            scope_id,
            limit=limit,
            offset=offset,
            min_resolved=min_resolved,
        )
        return await _visible_ratings(self._users, ratings), min_resolved

    @staticmethod
    def _min_resolved(scope_type: ScopeType) -> int:
        """Порог участия для области (категорийный ниже — меньше событий)."""
        if scope_type is ScopeType.CATEGORY:
            return LEADERBOARD_MIN_RESOLVED_CATEGORY
        return LEADERBOARD_MIN_RESOLVED_GLOBAL


class GetSeasonLeaderboard:
    """Сезонный лидерборд по slug: резолвит сезон и читает готовые рейтинги.

    Резолв slug→id — через ``SeasonConfigGateway`` (направление ``scoring →
    seasons``). Порядок — по сохранённому ``rank``, а он в сезоне уже
    призовой: квалифицированные сверху (см. :class:`RecomputeRatings`). Поэтому
    по умолчанию отдаём всех — неквалифицированный видит себя в таблице ниже
    призовой зоны, а первое место всегда принадлежит призёру. ``qualified_only``
    сжимает выдачу до одной призовой зоны; неактивные аккаунты скрываются всегда
    (см. :func:`_visible_ratings`).
    """

    def __init__(
        self,
        *,
        ratings: RatingRepository,
        season_config: SeasonConfigGateway,
        users: UserDirectory,
    ) -> None:
        self._ratings = ratings
        self._season_config = season_config
        self._users = users

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
        return season_id, await _visible_ratings(self._users, ratings)


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
            # Порог «рейтинговости» — из замороженного конфига сезона (как в
            # RecomputeRatings), а не глобальная константа.
            if event.predictor_count < cfg.min_predictors:
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


class GetSeasonStanding:
    """Своя позиция в сезоне + разбор квалификации (закреплённая строка «вы»).

    Композиция поверх :class:`GetSeasonQualification`: тот считает пороги на
    лету, а здесь к ним добавляется готовая строка рейтинга (место, Brier,
    объём). Нужен, потому что лидерборд страничный — участник ниже 50-й позиции
    иначе не увидел бы ни себя, ни причины, по которой он вне призового зачёта.

    ``rating`` — ``None``, если в сезоне ещё нет рейтинговых прогнозов
    пользователя; это не ошибка (разбор порогов при этом всё равно осмыслен —
    показывает, сколько осталось набрать).
    """

    def __init__(
        self,
        *,
        ratings: RatingRepository,
        season_config: SeasonConfigGateway,
        qualification: GetSeasonQualification,
    ) -> None:
        self._ratings = ratings
        self._season_config = season_config
        self._qualification = qualification

    async def execute(self, *, user_id: uuid.UUID, slug: str) -> SeasonStanding:
        """Возвращает позицию и разбор порогов; поднимает, если сезона нет."""
        season_id = await self._season_config.resolve_slug(slug)
        if season_id is None:
            raise SeasonNotFoundError(f"Сезон не найден: {slug}")
        result = await self._qualification.execute(user_id=user_id, slug=slug)
        rating = await self._ratings.get_for_user(
            user_id, ScopeType.SEASON, season_id
        )
        return SeasonStanding(
            season_id=season_id, rating=rating, qualification=result
        )


class GetUserCalibration:
    """Калибровка публичного профиля по хэндлу (predicted vs actual)."""

    def __init__(
        self, *, gateway: EventScoringGateway, users: UserDirectory
    ) -> None:
        self._gateway = gateway
        self._users = users

    async def execute(
        self, *, username: str
    ) -> tuple[uuid.UUID, CalibrationReport]:
        """Резолвит хэндл и строит отчёт калибровки.

        Возвращает ``(user_id, отчёт)``; неизвестный профиль →
        :class:`ProfileNotFoundError` (маппится в 404).
        """
        user_id = await self._users.resolve_username(username)
        if user_id is None:
            raise ProfileNotFoundError("Профиль не найден")
        entries = await self._gateway.list_user_calibration_entries(user_id)
        return user_id, calibrate(entries)


class GetProfileSummary:
    """Сводка публичного профиля: global / по категориям / активный сезон.

    Только чтение готовых агрегатов — ``RatingRepository.list_for_user``
    достаёт все срезы пользователя одним запросом (вместо ``get_for_user`` по
    каждой области), названия категорий резолвятся вторым запросом одним
    батчем. Отсутствие рейтингов — пустая сводка, не ошибка (новый
    пользователь / пользователь без разрешённых событий).
    """

    def __init__(
        self,
        *,
        ratings: RatingRepository,
        users: UserDirectory,
        categories: CategoryDirectory,
        season_config: SeasonConfigGateway,
    ) -> None:
        self._ratings = ratings
        self._users = users
        self._categories = categories
        self._season_config = season_config

    async def execute(self, *, username: str) -> ProfileSummary:
        """Резолвит хэндл и собирает сводку из материализованных ``ratings``.

        Неизвестный/неактивный профиль → :class:`ProfileNotFoundError`
        (маппится в 404) — тот же контракт, что у калибровки и публичного
        профиля identity (``resolve_username`` отдаёт id только ACTIVE).
        """
        user_id = await self._users.resolve_username(username)
        if user_id is None:
            raise ProfileNotFoundError("Профиль не найден")

        rows = await self._ratings.list_for_user(user_id)

        global_rating = next(
            (r for r in rows if r.scope_type is ScopeType.GLOBAL), None
        )

        category_rows = [r for r in rows if r.scope_type is ScopeType.CATEGORY]
        category_ids = [r.scope_id for r in category_rows if r.scope_id is not None]
        category_refs = await self._categories.list_by_ids(category_ids)
        categories = [
            ProfileCategoryRating(category=category_refs[r.scope_id], rating=r)
            for r in category_rows
            if r.scope_id in category_refs
        ]
        # Лучшая категория первой — как в лидерборде (тот же ключ ранжирования).
        categories.sort(key=lambda c: c.rating.rank)

        active_season_id = await self._season_config.get_active_season_id()
        season_rating = None
        if active_season_id is not None:
            season_rating = next(
                (
                    r
                    for r in rows
                    if r.scope_type is ScopeType.SEASON
                    and r.scope_id == active_season_id
                ),
                None,
            )

        return ProfileSummary(
            user_id=user_id,
            global_rating=global_rating,
            categories=categories,
            active_season_id=active_season_id,
            season_rating=season_rating,
        )


class RecalibrateSeasonGradations:
    """Межсезонная рекалибровка маппинга «градация → вероятность».

    Читает популяционные частоты «ДА» по каждому номиналу за прошедший сезон и
    пересчитывает номиналы изотонической регрессией (монотонность сохраняется).
    Результат — предложение нового маппинга для следующего сезона (заморозка в
    ``LeagueConfig`` — отдельный шаг активации, условия конкурса не меняются по
    ходу сезона). Чистая доменная математика (``recalibrate``) — здесь только
    группировка популяции и оркестрация.
    """

    def __init__(self, *, gateway: EventScoringGateway) -> None:
        self._gateway = gateway

    async def execute(
        self, *, season_id: uuid.UUID
    ) -> list[GradationRecalibration]:
        """Считает предложение нового маппинга по прогнозам сезона.

        Прогнозы снапшотятся дефолтной сеткой, поэтому все слоты — из
        ``DEFAULT_GRADATIONS``. Градацию без наблюдений («дыру») заполняем её
        текущим номиналом с нулевым весом: одна неиспользованная градация больше
        не рушит всю рекалибровку до сетки из &lt;5 значений (M-RECAL1).
        """
        entries = await self._gateway.list_season_calibration_entries(season_id)
        grouped: dict[float, list[int]] = {}
        for nominal, outcome in entries:
            grouped.setdefault(nominal, []).append(outcome)

        nominals = sorted(set(grouped) | set(DEFAULT_GRADATIONS))
        observed: list[tuple[float, float, int]] = []
        for nominal in nominals:
            outcomes = grouped.get(nominal, [])
            if outcomes:
                observed.append(
                    (nominal, math.fsum(outcomes) / len(outcomes), len(outcomes))
                )
            else:
                # Дыра: держим номинал на месте (частота = сам номинал, вес 0→1).
                observed.append((nominal, nominal, 0))

        fitted = recalibrate(
            [(f"{nominal:.2f}", freq, n) for nominal, freq, n in observed]
        )
        return [
            GradationRecalibration(
                nominal=nominal, observed_freq=freq, n=n, fitted=new_nominal
            )
            for (nominal, freq, n), (_, new_nominal) in zip(
                observed, fitted, strict=True
            )
        ]
