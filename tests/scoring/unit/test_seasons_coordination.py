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
    RollSeasons,
)
from app.modules.scoring.application.use_cases import RecomputeRatings
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
        auto_finalize=False,  # авто-финализация выключена (дизайн §6.4/§6.5)
    )
    await roll.execute()

    season = await season_repo.get_by_id(season_id)
    assert season is not None and season.status is SeasonStatus.ACTIVE  # не тронут
    assert season_repo.finalizations == []
