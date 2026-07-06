"""Юнит-тесты кросс-доменных координаторов scoring↔seasons: финализация и roll.

Финализация — reliability-критичная операция (дизайн §6): идемпотентность,
блок при открытых спорах, неизменяемый снапшот победителей, атомарный пересчёт.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.modules.scoring.application.dto import SeasonConfigView
from app.modules.scoring.application.seasons_coordination import (
    FinalizeSeason,
    RecalibratingLeagueConfigProvider,
    RollSeasons,
)
from app.modules.scoring.application.use_cases import (
    RecalibrateSeasonGradations,
    RecomputeRatings,
)
from app.modules.scoring.domain.constants import DEFAULT_GRADATIONS
from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.errors import (
    SeasonFinalizationBlockedError,
    SeasonNotFoundError,
)
from app.modules.seasons.domain.value_objects import LeagueConfig
from tests.scoring.conftest import FIXED_NOW, make_event
from tests.scoring.fakes import (
    FakeClock,
    FakeEventScoringGateway,
    FakeSeasonConfigGateway,
    InMemoryRatingRepository,
)
from tests.seasons.fakes import FakeDisputeGuard, InMemorySeasonRepository

EASY_CFG = LeagueConfig(
    gradation_map=(0.1, 0.3, 0.5, 0.7, 0.9),
    n_min=1,
    c_min=1,
    w_min=0.0,
    m_per_category=1,
    k_shrink=6.0,
    min_predictors=5,
)
LATER = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _active_season(season_id: uuid.UUID, *, ends_at: datetime | None = None) -> Season:
    return Season(
        slug="2026q3",
        title="Сезон III",
        starts_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ends_at=ends_at or datetime(2026, 9, 30, tzinfo=timezone.utc),
        status=SeasonStatus.ACTIVE,
        league_config=EASY_CFG,
        id=season_id,
    )


def _finalize_uc(
    *,
    season_repo: InMemorySeasonRepository,
    ratings: InMemoryRatingRepository,
    season_id: uuid.UUID,
    dispute_guard: FakeDisputeGuard,
) -> FinalizeSeason:
    event, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        season_id=season_id,
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    season_config = FakeSeasonConfigGateway(
        configs={
            season_id: SeasonConfigView(status=SeasonStatus.ACTIVE, config=EASY_CFG)
        }
    )
    recompute = RecomputeRatings(
        gateway=gateway,
        ratings=ratings,
        clock=FakeClock(FIXED_NOW),
        season_config=season_config,
    )
    return FinalizeSeason(
        seasons=season_repo,
        dispute_guard=dispute_guard,
        recompute=recompute,
        ratings=ratings,
        clock=FakeClock(LATER),
    )


async def test_finalize_moves_to_finished_and_writes_immutable_snapshot() -> None:
    season_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(_active_season(season_id))
    ratings = InMemoryRatingRepository()
    guard = FakeDisputeGuard(has_open=False)

    uc = _finalize_uc(
        season_repo=season_repo, ratings=ratings, season_id=season_id, dispute_guard=guard
    )
    result = await uc.execute(season_id=season_id)

    assert result.finalized is True
    assert result.qualified_count == 5  # все 5 квалифицированы мягким конфигом
    season = await season_repo.get_by_id(season_id)
    assert season is not None and season.status is SeasonStatus.FINISHED
    # Неизменяемая запись финализации с разложенными по строкам участниками.
    assert len(season_repo.finalizations) == 1
    finalization, entries = season_repo.finalizations[0]
    assert finalization.league_config == EASY_CFG
    assert len(entries) == 5
    assert {e.rank for e in entries} == {1, 2, 3, 4, 5}


async def test_finalize_is_idempotent_noop_when_already_finished() -> None:
    season_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(_active_season(season_id))
    ratings = InMemoryRatingRepository()
    guard = FakeDisputeGuard(has_open=False)
    uc = _finalize_uc(
        season_repo=season_repo, ratings=ratings, season_id=season_id, dispute_guard=guard
    )

    await uc.execute(season_id=season_id)
    second = await uc.execute(season_id=season_id)

    # Повтор — no-op: не пересчитывает и не пишет вторую запись финализации.
    assert second.finalized is False
    assert len(season_repo.finalizations) == 1


async def test_finalize_blocked_by_open_disputes() -> None:
    season_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(_active_season(season_id))
    ratings = InMemoryRatingRepository()
    guard = FakeDisputeGuard(has_open=True)
    uc = _finalize_uc(
        season_repo=season_repo, ratings=ratings, season_id=season_id, dispute_guard=guard
    )

    with pytest.raises(SeasonFinalizationBlockedError):
        await uc.execute(season_id=season_id)

    # Сезон НЕ завершён, снапшот НЕ записан.
    season = await season_repo.get_by_id(season_id)
    assert season is not None and season.status is SeasonStatus.ACTIVE
    assert season_repo.finalizations == []


async def test_finalize_missing_season_raises() -> None:
    season_repo = InMemorySeasonRepository()
    ratings = InMemoryRatingRepository()
    uc = _finalize_uc(
        season_repo=season_repo,
        ratings=ratings,
        season_id=uuid.uuid4(),
        dispute_guard=FakeDisputeGuard(),
    )
    with pytest.raises(SeasonNotFoundError):
        await uc.execute(season_id=uuid.uuid4())


# ── RollSeasons ──────────────────────────────────────────────────────────────


async def test_roll_activates_due_upcoming_seasons() -> None:
    season_repo = InMemorySeasonRepository()
    due = Season(
        slug="due",
        title="Пора",
        starts_at=datetime(2026, 6, 1, tzinfo=timezone.utc),  # <= LATER
        ends_at=datetime(2026, 12, 1, tzinfo=timezone.utc),
        status=SeasonStatus.UPCOMING,
    )
    future = Season(
        slug="future",
        title="Рано",
        starts_at=datetime(2026, 12, 1, tzinfo=timezone.utc),  # > LATER
        ends_at=datetime(2027, 3, 1, tzinfo=timezone.utc),
        status=SeasonStatus.UPCOMING,
    )
    await season_repo.add(due)
    await season_repo.add(future)

    roll = RollSeasons(
        seasons=season_repo,
        finalize=_finalize_uc(
            season_repo=season_repo,
            ratings=InMemoryRatingRepository(),
            season_id=uuid.uuid4(),
            dispute_guard=FakeDisputeGuard(),
        ),
        clock=FakeClock(LATER),
        auto_finalize=False,
    )
    await roll.execute()

    assert (await season_repo.get_by_id(due.id)).status is SeasonStatus.ACTIVE  # type: ignore[union-attr]
    assert (await season_repo.get_by_id(future.id)).status is SeasonStatus.UPCOMING  # type: ignore[union-attr]


async def test_roll_does_not_auto_finalize_when_gated() -> None:
    season_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(
        _active_season(season_id, ends_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    )  # ends в прошлом относительно LATER
    ratings = InMemoryRatingRepository()
    roll = RollSeasons(
        seasons=season_repo,
        finalize=_finalize_uc(
            season_repo=season_repo,
            ratings=ratings,
            season_id=season_id,
            dispute_guard=FakeDisputeGuard(),
        ),
        clock=FakeClock(LATER),
        auto_finalize=False,  # явно выключено — ручной режим
    )
    await roll.execute()

    season = await season_repo.get_by_id(season_id)
    assert season is not None and season.status is SeasonStatus.ACTIVE  # не тронут
    assert season_repo.finalizations == []


async def test_roll_auto_finalizes_expired_active_season_by_default() -> None:
    # Дефолт ``auto_finalize=True``: истёкший активный сезон без открытых споров
    # закрывается таймером (боевой DisputeGuard блокирует закрытие при спорах).
    season_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(
        _active_season(season_id, ends_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    )  # ends в прошлом относительно LATER
    ratings = InMemoryRatingRepository()
    roll = RollSeasons(
        seasons=season_repo,
        finalize=_finalize_uc(
            season_repo=season_repo,
            ratings=ratings,
            season_id=season_id,
            dispute_guard=FakeDisputeGuard(has_open=False),
        ),
        clock=FakeClock(LATER),
        # auto_finalize не передан — берётся дефолт (True)
    )
    await roll.execute()

    season = await season_repo.get_by_id(season_id)
    assert season is not None and season.status is SeasonStatus.FINISHED
    assert len(season_repo.finalizations) == 1


# ── RecalibratingLeagueConfigProvider ────────────────────────────────────────


def _finished_season(season_id: uuid.UUID, *, ends_at: datetime) -> Season:
    return Season(
        slug=f"fin-{season_id.hex[:6]}",
        title="Завершён",
        starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ends_at=ends_at,
        status=SeasonStatus.FINISHED,
        league_config=EASY_CFG,
        id=season_id,
    )


def _upcoming(season_id: uuid.UUID) -> Season:
    return Season(
        slug="new",
        title="Новый",
        starts_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 12, 1, tzinfo=timezone.utc),
        status=SeasonStatus.UPCOMING,
        id=season_id,
    )


def _entries(*by_nominal: tuple[float, int, int]) -> list[tuple[float, int]]:
    """Строит калибровочные записи: (номинал, число «да», число «нет»)."""
    out: list[tuple[float, int]] = []
    for nominal, yes, no in by_nominal:
        out.extend([(nominal, 1)] * yes + [(nominal, 0)] * no)
    return out


def _provider(
    *, season_repo: InMemorySeasonRepository, season_entries: dict
) -> RecalibratingLeagueConfigProvider:
    return RecalibratingLeagueConfigProvider(
        seasons=season_repo,
        recalibrate=RecalibrateSeasonGradations(
            gateway=FakeEventScoringGateway(season_entries=season_entries)
        ),
    )


async def test_provider_defaults_without_finished_season() -> None:
    season_repo = InMemorySeasonRepository()
    provider = _provider(season_repo=season_repo, season_entries={})
    config = await provider.config_for(_upcoming(uuid.uuid4()))
    assert config.gradation_map == DEFAULT_GRADATIONS


async def test_provider_freezes_recalibrated_grid() -> None:
    prev_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(
        _finished_season(prev_id, ends_at=datetime(2026, 5, 31, tzinfo=timezone.utc))
    )
    # Наблюдаемые частоты строго возрастают и лежат в (0,1) → PAV оставляет их:
    # сетка (0.2, 0.4, 0.5, 0.6, 0.8) ≠ дефолт.
    entries = _entries(
        (0.1, 1, 4),  # freq 0.2
        (0.3, 2, 3),  # freq 0.4
        (0.5, 1, 1),  # freq 0.5
        (0.7, 3, 2),  # freq 0.6
        (0.9, 4, 1),  # freq 0.8
    )
    provider = _provider(season_repo=season_repo, season_entries={prev_id: entries})

    config = await provider.config_for(_upcoming(uuid.uuid4()))

    assert config.gradation_map != DEFAULT_GRADATIONS
    assert len(config.gradation_map) == 5
    # Строго возрастает и в (0,1) (иначе LeagueConfig бы упал).
    assert all(0.0 < p < 1.0 for p in config.gradation_map)
    assert all(
        a < b for a, b in zip(config.gradation_map, config.gradation_map[1:])
    )
    assert config.gradation_map[0] == pytest.approx(0.2)
    assert config.gradation_map[-1] == pytest.approx(0.8)


async def test_provider_fills_holes_for_unused_gradations() -> None:
    prev_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(
        _finished_season(prev_id, ends_at=datetime(2026, 5, 31, tzinfo=timezone.utc))
    )
    # Использованы только 2 градации: пропущенные заполняются их номиналом
    # (M-RECAL1) → сетка длины 5, рекалибровка применяется, а не откатывается.
    entries = _entries((0.3, 2, 3), (0.7, 3, 2))
    provider = _provider(season_repo=season_repo, season_entries={prev_id: entries})

    config = await provider.config_for(_upcoming(uuid.uuid4()))
    grid = config.gradation_map
    assert len(grid) == len(DEFAULT_GRADATIONS)
    assert grid != DEFAULT_GRADATIONS  # рекалибровка применилась
    assert all(0.0 < x < 1.0 for x in grid)
    assert all(grid[i] < grid[i + 1] for i in range(len(grid) - 1))  # строго растёт


async def test_provider_clamps_boundary_frequency() -> None:
    prev_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(
        _finished_season(prev_id, ends_at=datetime(2026, 5, 31, tzinfo=timezone.utc))
    )
    # Верхняя градация с частотой 1.0 клампится в (0,1) (M-RECAL2) → рекалибровка
    # применяется, а не откатывается на дефолт из-за граничного значения.
    entries = _entries(
        (0.1, 1, 4),
        (0.3, 2, 3),
        (0.5, 1, 1),
        (0.7, 3, 2),
        (0.9, 5, 0),  # freq 1.0
    )
    provider = _provider(season_repo=season_repo, season_entries={prev_id: entries})

    config = await provider.config_for(_upcoming(uuid.uuid4()))
    grid = config.gradation_map
    assert len(grid) == len(DEFAULT_GRADATIONS)
    assert all(0.0 < x < 1.0 for x in grid)  # ничего на границе 0/1
    assert all(grid[i] < grid[i + 1] for i in range(len(grid) - 1))
    assert grid[-1] < 1.0  # частота 1.0 клампнута внутрь интервала


async def test_provider_picks_most_recent_finished_season() -> None:
    older_id, newer_id = uuid.uuid4(), uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(
        _finished_season(older_id, ends_at=datetime(2025, 12, 31, tzinfo=timezone.utc))
    )
    await season_repo.add(
        _finished_season(newer_id, ends_at=datetime(2026, 5, 31, tzinfo=timezone.utc))
    )
    # Свежий сезон даёт валидную сетку, старый — нет (короткую). Провайдер должен
    # взять свежий → получить рекалиброванную сетку, а не фолбэк.
    valid = _entries(
        (0.1, 1, 4), (0.3, 2, 3), (0.5, 1, 1), (0.7, 3, 2), (0.9, 4, 1)
    )
    provider = _provider(
        season_repo=season_repo,
        season_entries={newer_id: valid, older_id: _entries((0.3, 1, 1))},
    )

    config = await provider.config_for(_upcoming(uuid.uuid4()))
    assert config.gradation_map != DEFAULT_GRADATIONS


async def test_roll_activates_with_provider_config() -> None:
    prev_id = uuid.uuid4()
    season_repo = InMemorySeasonRepository()
    await season_repo.add(
        _finished_season(prev_id, ends_at=datetime(2026, 5, 31, tzinfo=timezone.utc))
    )
    due = _upcoming(uuid.uuid4())
    await season_repo.add(due)
    entries = _entries(
        (0.1, 1, 4), (0.3, 2, 3), (0.5, 1, 1), (0.7, 3, 2), (0.9, 4, 1)
    )
    roll = RollSeasons(
        seasons=season_repo,
        finalize=_finalize_uc(
            season_repo=season_repo,
            ratings=InMemoryRatingRepository(),
            season_id=uuid.uuid4(),
            dispute_guard=FakeDisputeGuard(),
        ),
        clock=FakeClock(LATER),
        auto_finalize=False,
        config_provider=_provider(
            season_repo=season_repo, season_entries={prev_id: entries}
        ),
    )
    await roll.execute()

    activated = await season_repo.get_by_id(due.id)
    assert activated is not None and activated.status is SeasonStatus.ACTIVE
    assert activated.league_config is not None
    assert activated.league_config.gradation_map != DEFAULT_GRADATIONS
