"""DTO прикладного слоя scoring — контракты данных между портами и use-cases.

Чистые dataclass'ы без I/O. ``EventScoringStatus`` отвечает на вопрос «можно
ли уже скорить событие» (найдено / разрешено / прошло окно оспаривания);
``PredictionScore`` — результат пер-прогнозного Brier для записи обратно в
``predictions``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.modules.scoring.domain.entities import Rating
from app.modules.seasons.domain.entities import SeasonStatus
from app.modules.seasons.domain.value_objects import LeagueConfig, QualificationResult


@dataclass(frozen=True, slots=True)
class EventScoringStatus:
    """Готовность события к скорингу (из домена events/resolutions).

    ``is_final`` означает, что исход зафиксирован финально И окно оспаривания
    закрыто — только тогда домен scoring считает Brier (см. поток
    жизненного цикла в задании).
    """

    found: bool
    is_resolved: bool
    is_final: bool
    outcome: int | None

    @property
    def is_scoreable(self) -> bool:
        """Можно ли считать Brier: разрешено, финально и исход известен."""
        return (
            self.found
            and self.is_resolved
            and self.is_final
            and self.outcome is not None
        )


@dataclass(frozen=True, slots=True)
class PredictionScore:
    """Проставляемая оценка прогноза: чей прогноз и его Brier."""

    user_id: uuid.UUID
    brier: Decimal


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    """Итог финализации сезона (для воркера/админ-эндпоинта).

    ``finalized=False`` — идемпотентный no-op (сезон уже был завершён).
    """

    finalized: bool
    qualified_count: int
    total_participants: int


@dataclass(frozen=True, slots=True)
class GradationRecalibration:
    """Результат межсезонной рекалибровки одной градации.

    ``nominal`` — старый номинал градации (зафиксированный в прошлом сезоне),
    ``observed_freq`` — фактическая частота «ДА» среди прогнозов этой градации,
    ``n`` — объём выборки, ``fitted`` — пересчитанный номинал (изотонически
    монотонный) для следующего сезона.
    """

    nominal: float
    observed_freq: float
    n: int
    fitted: float


@dataclass(frozen=True, slots=True)
class SeasonConfigView:
    """Проекция сезона из домена seasons для нужд квалификации в scoring.

    Несёт статус (чтобы отличить «сезон ещё не активирован — нормально» от
    «активен, но конфиг недоступен — ошибка инварианта», см. дизайн §4) и
    замороженный ``LeagueConfig`` (``None`` до активации).
    """

    status: SeasonStatus
    config: LeagueConfig | None


@dataclass(frozen=True, slots=True)
class CategoryRef:
    """Название категории (для сводки профиля — рейтинги хранят только id)."""

    category_id: uuid.UUID
    slug: str
    title: str


@dataclass(frozen=True, slots=True)
class ProfileCategoryRating:
    """Один срез сводки профиля по категории: название + готовый рейтинг."""

    category: CategoryRef
    rating: Rating


@dataclass(frozen=True, slots=True)
class SeasonStanding:
    """Позиция пользователя в сезоне + разбор его квалификации к призам.

    Для закреплённой строки «вы» под сезонной таблицей: участник может быть за
    пределами страницы лидерборда, но обязан видеть своё место и то, каких
    порогов ему не хватает. ``rating`` — ``None``, если разрешённых прогнозов в
    сезоне ещё нет (тогда в ``qualification`` — нули и ``qualified=False``).
    """

    season_id: uuid.UUID
    rating: Rating | None
    qualification: QualificationResult


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    """Сводка публичного профиля: готовые срезы global/категории/активный сезон.

    Ничего не пересчитывает — все поля читаются из материализованных
    ``ratings``. Отсутствие среза (пользователь не набрал рейтинга в области,
    активного сезона нет) — ``None``/пустой список, а не ошибка.
    """

    user_id: uuid.UUID
    global_rating: Rating | None
    categories: list[ProfileCategoryRating]
    active_season_id: uuid.UUID | None
    season_rating: Rating | None
