"""Юнит-тесты use-cases scoring через порты-фейки.

Покрывают: пер-прогнозный Brier при разрешении события (идемпотентно, с
запретом скоринга неразрешённого/несуществующего события); пересчёт рейтингов
по областям с ранжированием по превышению над толпой; чтение лидерборда и
калибровки профиля.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.modules.scoring.application.dto import EventScoringStatus
from app.modules.scoring.application.use_cases import (
    GetLeaderboard,
    GetUserCalibration,
    RecomputeRatings,
    ScoreEvent,
)
from app.modules.scoring.domain.entities import ScopeType
from app.modules.scoring.domain.errors import (
    EventNotResolvedError,
    ScoringTargetEventNotFoundError,
)
from app.modules.scoring.domain.formulas import (
    event_contribution,
    season_rating_from_contributions,
)
from app.modules.scoring.domain.value_objects import quantize_score
from tests.scoring.conftest import FIXED_NOW, make_event
from tests.scoring.fakes import (
    FakeClock,
    FakeEventScoringGateway,
    FakePredictionScoreWriter,
    InMemoryRatingRepository,
)


def _final_status(outcome: int) -> EventScoringStatus:
    return EventScoringStatus(
        found=True, is_resolved=True, is_final=True, outcome=outcome
    )


# ── ScoreEvent ──────────────────────────────────────────────────────────────


async def test_score_event_writes_brier_per_prediction() -> None:
    event, ids = make_event(outcome=1, probabilities=[0.9, 0.7, 0.5])
    gateway = FakeEventScoringGateway(
        statuses={event.event_id: _final_status(1)},
        events={event.event_id: event},
    )
    writer = FakePredictionScoreWriter()
    uc = ScoreEvent(gateway=gateway, writer=writer, clock=FakeClock(FIXED_NOW))

    count = await uc.execute(event_id=event.event_id)

    assert count == 3
    scores = {s.user_id: s.brier for s in writer.saved[event.event_id]}
    assert scores[ids[0]] == Decimal("0.01000")  # (0.9-1)^2
    assert scores[ids[1]] == Decimal("0.09000")  # (0.7-1)^2
    assert scores[ids[2]] == Decimal("0.25000")  # (0.5-1)^2


async def test_score_event_missing_event_raises() -> None:
    gateway = FakeEventScoringGateway()
    uc = ScoreEvent(
        gateway=gateway,
        writer=FakePredictionScoreWriter(),
        clock=FakeClock(FIXED_NOW),
    )
    with pytest.raises(ScoringTargetEventNotFoundError):
        await uc.execute(event_id=uuid.uuid4())


async def test_score_event_unresolved_raises() -> None:
    event_id = uuid.uuid4()
    gateway = FakeEventScoringGateway(
        statuses={
            event_id: EventScoringStatus(
                found=True, is_resolved=False, is_final=False, outcome=None
            )
        }
    )
    uc = ScoreEvent(
        gateway=gateway,
        writer=FakePredictionScoreWriter(),
        clock=FakeClock(FIXED_NOW),
    )
    with pytest.raises(EventNotResolvedError):
        await uc.execute(event_id=event_id)


async def test_score_event_not_final_raises() -> None:
    # Исход зафиксирован, но окно оспаривания не закрыто → ещё не скорим.
    event, _ = make_event(outcome=1, probabilities=[0.9, 0.7])
    gateway = FakeEventScoringGateway(
        statuses={
            event.event_id: EventScoringStatus(
                found=True, is_resolved=True, is_final=False, outcome=1
            )
        },
        events={event.event_id: event},
    )
    uc = ScoreEvent(
        gateway=gateway,
        writer=FakePredictionScoreWriter(),
        clock=FakeClock(FIXED_NOW),
    )
    with pytest.raises(EventNotResolvedError):
        await uc.execute(event_id=event.event_id)


async def test_score_event_is_idempotent() -> None:
    event, ids = make_event(outcome=0, probabilities=[0.1, 0.3])
    gateway = FakeEventScoringGateway(
        statuses={event.event_id: _final_status(0)},
        events={event.event_id: event},
    )
    writer = FakePredictionScoreWriter()
    uc = ScoreEvent(gateway=gateway, writer=writer, clock=FakeClock(FIXED_NOW))

    first = await uc.execute(event_id=event.event_id)
    first_scores = {s.user_id: s.brier for s in writer.saved[event.event_id]}
    second = await uc.execute(event_id=event.event_id)
    second_scores = {s.user_id: s.brier for s in writer.saved[event.event_id]}

    assert first == second == 2
    assert first_scores == second_scores


# ── RecomputeRatings ────────────────────────────────────────────────────────


async def test_recompute_ratings_ranks_by_crowd_advantage() -> None:
    """Игрок, обыгравший уверенную толпу в провале, — №1; следующие за толпой ниже."""
    category_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    # Толпа [0.9×4, 0.3], исход НЕТ: владелец 0.3 обыграл толпу.
    event, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        category_id=category_id,
        user_ids=ids,
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    repo = InMemoryRatingRepository()
    uc = RecomputeRatings(gateway=gateway, ratings=repo, clock=FakeClock(FIXED_NOW))

    await uc.execute()

    board = await repo.leaderboard(ScopeType.GLOBAL, None)
    assert len(board) == 5
    assert board[0].user_id == ids[4]  # игрок с 0.3
    assert board[0].rank == 1

    # Ожидаемый skill_score = R по тем же доменным формулам.
    weight, contribution = event_contribution(0.3, event.probabilities(), 0)
    expected_r = season_rating_from_contributions([weight], [contribution / weight])
    assert board[0].skill_score == quantize_score(expected_r)
    assert board[0].n_resolved == 1
    # Категорийная область строится отдельно от глобальной.
    cat_board = await repo.leaderboard(ScopeType.CATEGORY, category_id)
    assert len(cat_board) == 5


async def test_recompute_ratings_skips_low_predictor_events() -> None:
    # Событие с < MIN_PREDICTORS не рейтингуется → рейтингов нет.
    event, _ = make_event(outcome=1, probabilities=[0.9, 0.7])  # 2 < 5
    gateway = FakeEventScoringGateway(resolved=[event])
    repo = InMemoryRatingRepository()
    uc = RecomputeRatings(gateway=gateway, ratings=repo, clock=FakeClock(FIXED_NOW))

    await uc.execute()

    assert await repo.leaderboard(ScopeType.GLOBAL, None) == []


async def test_recompute_ratings_builds_season_scope() -> None:
    season_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    event, _ = make_event(
        outcome=1,
        probabilities=[0.5, 0.5, 0.5, 0.7, 0.9],
        season_id=season_id,
        user_ids=ids,
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    repo = InMemoryRatingRepository()
    uc = RecomputeRatings(gateway=gateway, ratings=repo, clock=FakeClock(FIXED_NOW))

    await uc.execute()

    season_board = await repo.leaderboard(ScopeType.SEASON, season_id)
    assert len(season_board) == 5
    assert {r.rank for r in season_board} == {1, 2, 3, 4, 5}


# ── GetLeaderboard / GetUserCalibration ─────────────────────────────────────


async def test_get_leaderboard_returns_ranked_scope() -> None:
    category_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    event, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        category_id=category_id,
        user_ids=ids,
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    repo = InMemoryRatingRepository()
    await RecomputeRatings(
        gateway=gateway, ratings=repo, clock=FakeClock(FIXED_NOW)
    ).execute()

    uc = GetLeaderboard(ratings=repo)
    board = await uc.execute(scope_type=ScopeType.CATEGORY, scope_id=category_id, limit=3)
    assert len(board) == 3
    assert board[0].rank == 1


async def test_get_user_calibration_delegates_to_domain() -> None:
    user_id = uuid.uuid4()
    entries = [(0.70, 1)] * 31 + [(0.70, 0)] * 9
    gateway = FakeEventScoringGateway(user_entries={user_id: entries})
    uc = GetUserCalibration(gateway=gateway)

    report = await uc.execute(user_id=user_id)

    assert report.n_total == 40
    assert report.bins[0].frequency == pytest.approx(0.775, abs=1e-4)
