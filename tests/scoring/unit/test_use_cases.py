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
    GetProfileSummary,
    GetSeasonLeaderboard,
    GetUserCalibration,
    RecalibrateSeasonGradations,
    RecomputeRatings,
    ScoreEvent,
)
from app.modules.scoring.domain.constants import (
    LEADERBOARD_MIN_RESOLVED_CATEGORY,
    LEADERBOARD_MIN_RESOLVED_GLOBAL,
)
from app.modules.scoring.domain.entities import Rating, ScopeType
from app.modules.scoring.domain.errors import (
    EventNotResolvedError,
    ProfileNotFoundError,
    ScoringTargetEventNotFoundError,
)
from app.modules.scoring.domain.formulas import (
    event_contribution,
    season_rating_from_contributions,
    time_weight_from_earliness,
)
from app.modules.scoring.domain.value_objects import (
    PredictionVote,
    ResolvedEvent,
    quantize_score,
)
from tests.scoring.conftest import FIXED_NOW, make_event
from tests.scoring.fakes import (
    FakeCategoryDirectory,
    FakeClock,
    FakeEventScoringGateway,
    FakePredictionScoreWriter,
    FakeSeasonConfigGateway,
    FakeUserDirectory,
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
    uc = RecomputeRatings(
        gateway=gateway,
        ratings=repo,
        clock=FakeClock(FIXED_NOW),
        season_config=FakeSeasonConfigGateway(),
    )

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
    uc = RecomputeRatings(
        gateway=gateway,
        ratings=repo,
        clock=FakeClock(FIXED_NOW),
        season_config=FakeSeasonConfigGateway(),
    )

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
    uc = RecomputeRatings(
        gateway=gateway,
        ratings=repo,
        clock=FakeClock(FIXED_NOW),
        season_config=FakeSeasonConfigGateway(),
    )

    await uc.execute()

    season_board = await repo.leaderboard(ScopeType.SEASON, season_id)
    assert len(season_board) == 5
    assert {r.rank for r in season_board} == {1, 2, 3, 4, 5}


def test_touched_scopes_covers_global_category_and_season() -> None:
    category_id = uuid.uuid4()
    season_id = uuid.uuid4()
    assert RecomputeRatings.touched_scopes(
        category_id=category_id, season_id=None
    ) == {(ScopeType.GLOBAL, None), (ScopeType.CATEGORY, category_id)}
    assert RecomputeRatings.touched_scopes(
        category_id=category_id, season_id=season_id
    ) == {
        (ScopeType.GLOBAL, None),
        (ScopeType.CATEGORY, category_id),
        (ScopeType.SEASON, season_id),
    }


async def test_recompute_ratings_scope_filter_writes_only_touched_scopes() -> None:
    """Инкрементальный пересчёт: пишем только срезы события, но ранжируем полно."""
    category_id = uuid.uuid4()
    other_category = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    # Два события в разных категориях; целимся только в первую.
    target, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        category_id=category_id,
        user_ids=ids,
    )
    other, _ = make_event(
        outcome=1,
        probabilities=[0.5, 0.5, 0.5, 0.7, 0.9],
        category_id=other_category,
    )
    gateway = FakeEventScoringGateway(resolved=[target, other])
    repo = InMemoryRatingRepository()
    uc = RecomputeRatings(
        gateway=gateway,
        ratings=repo,
        clock=FakeClock(FIXED_NOW),
        season_config=FakeSeasonConfigGateway(),
    )

    scopes = RecomputeRatings.touched_scopes(category_id=category_id, season_id=None)
    await uc.execute(scopes=scopes)

    # Целевая категория записана и полностью проранжирована.
    cat_board = await repo.leaderboard(ScopeType.CATEGORY, category_id)
    assert len(cat_board) == 5
    assert {r.rank for r in cat_board} == {1, 2, 3, 4, 5}
    # Глобальный срез — тоже (учитывает голоса обоих событий).
    assert len(await repo.leaderboard(ScopeType.GLOBAL, None)) == 10
    # Чужая категория НЕ тронута.
    assert await repo.leaderboard(ScopeType.CATEGORY, other_category) == []


def test_time_weight_from_earliness_bounds_and_monotonicity() -> None:
    assert time_weight_from_earliness(0.0) == 1.0  # у самого закрытия
    assert time_weight_from_earliness(1.0) == 1.5  # в момент открытия (lam=0.5)
    assert time_weight_from_earliness(0.5) == 1.25
    # Клампинг за пределами [0, 1].
    assert time_weight_from_earliness(-3.0) == 1.0
    assert time_weight_from_earliness(9.0) == 1.5
    # Монотонность по ранности.
    assert time_weight_from_earliness(0.2) < time_weight_from_earliness(0.8)


async def test_recompute_ratings_rewards_earlier_prediction() -> None:
    """Два игрока обыграли толпу одинаково, но кто раньше — тот выше рейтингом."""
    early_id, late_id = uuid.uuid4(), uuid.uuid4()
    crowd = [uuid.uuid4() for _ in range(3)]
    # Толпа уверена в ДА (0.9), исход НЕТ: обе «0.3» обыграли толпу одинаково,
    # но early голосовал рано (time_weight 1.5), late — у закрытия (1.0).
    votes = (
        PredictionVote(user_id=crowd[0], probability=0.9),
        PredictionVote(user_id=crowd[1], probability=0.9),
        PredictionVote(user_id=crowd[2], probability=0.9),
        PredictionVote(user_id=early_id, probability=0.3, time_weight=1.5),
        PredictionVote(user_id=late_id, probability=0.3, time_weight=1.0),
    )
    event = ResolvedEvent(
        event_id=uuid.uuid4(),
        category_id=uuid.uuid4(),
        season_id=None,
        outcome=0,
        votes=votes,
    )
    repo = InMemoryRatingRepository()
    await RecomputeRatings(
        gateway=FakeEventScoringGateway(resolved=[event]),
        ratings=repo,
        clock=FakeClock(FIXED_NOW),
        season_config=FakeSeasonConfigGateway(),
    ).execute()

    board = {r.user_id: r for r in await repo.leaderboard(ScopeType.GLOBAL, None)}
    assert board[early_id].skill_score > board[late_id].skill_score
    assert board[early_id].rank < board[late_id].rank


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
        gateway=gateway,
        ratings=repo,
        clock=FakeClock(FIXED_NOW),
        season_config=FakeSeasonConfigGateway(),
    ).execute()

    uc = GetLeaderboard(ratings=repo, users=FakeUserDirectory())
    # Каждый участник тут с n_resolved=1 (одно событие) — ниже порога участия
    # категории (5), поэтому дефолтный qualified_only=True всех бы спрятал;
    # для проверки чистого ранжирования отключаем фильтр явно.
    board, min_resolved = await uc.execute(
        scope_type=ScopeType.CATEGORY,
        scope_id=category_id,
        limit=3,
        qualified_only=False,
    )
    assert len(board) == 3
    assert board[0].rank == 1
    assert min_resolved is None


async def test_get_leaderboard_hides_users_below_participation_threshold() -> None:
    """Дефолт ``qualified_only=True`` скрывает n_resolved < порога; граница — видна."""
    repo = InMemoryRatingRepository()
    below_id, at_id, above_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await repo.upsert_many(
        [
            _rating(
                below_id,
                ScopeType.GLOBAL,
                None,
                rank=1,
                n_resolved=LEADERBOARD_MIN_RESOLVED_GLOBAL - 1,
            ),
            _rating(
                at_id,
                ScopeType.GLOBAL,
                None,
                rank=2,
                n_resolved=LEADERBOARD_MIN_RESOLVED_GLOBAL,
            ),
            _rating(
                above_id,
                ScopeType.GLOBAL,
                None,
                rank=3,
                n_resolved=LEADERBOARD_MIN_RESOLVED_GLOBAL + 5,
            ),
        ]
    )
    uc = GetLeaderboard(ratings=repo, users=FakeUserDirectory())

    board, min_resolved = await uc.execute(scope_type=ScopeType.GLOBAL, scope_id=None)
    ids = {r.user_id for r in board}
    assert below_id not in ids
    assert at_id in ids  # ровно порог — уже участвует
    assert above_id in ids
    assert min_resolved == LEADERBOARD_MIN_RESOLVED_GLOBAL

    full_board, no_threshold = await uc.execute(
        scope_type=ScopeType.GLOBAL, scope_id=None, qualified_only=False
    )
    assert {r.user_id for r in full_board} == {below_id, at_id, above_id}
    assert no_threshold is None


async def test_get_leaderboard_category_threshold_lower_than_global() -> None:
    """Категорийный порог (5) ниже глобального (10) — своя граница."""
    repo = InMemoryRatingRepository()
    category_id = uuid.uuid4()
    below_id, at_id = uuid.uuid4(), uuid.uuid4()
    await repo.upsert_many(
        [
            _rating(
                below_id,
                ScopeType.CATEGORY,
                category_id,
                rank=1,
                n_resolved=LEADERBOARD_MIN_RESOLVED_CATEGORY - 1,
            ),
            _rating(
                at_id,
                ScopeType.CATEGORY,
                category_id,
                rank=2,
                n_resolved=LEADERBOARD_MIN_RESOLVED_CATEGORY,
            ),
        ]
    )
    uc = GetLeaderboard(ratings=repo, users=FakeUserDirectory())

    board, min_resolved = await uc.execute(
        scope_type=ScopeType.CATEGORY, scope_id=category_id
    )
    assert {r.user_id for r in board} == {at_id}
    assert min_resolved == LEADERBOARD_MIN_RESOLVED_CATEGORY


async def test_get_leaderboard_hides_inactive_users() -> None:
    """Удалённый/заблокированный аккаунт не показывается; активные — со своим rank.

    Строка такого аккаунта выглядела бы как «@<uuid>» с мёртвой ссылкой:
    публичный профиль отдаётся только для ACTIVE. Ранги остальных остаются
    сохранёнными (в последовательности возможен разрыв).
    """
    repo = InMemoryRatingRepository()
    top_id, deleted_id, third_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await repo.upsert_many(
        [
            _rating(top_id, ScopeType.GLOBAL, None, rank=1),
            _rating(deleted_id, ScopeType.GLOBAL, None, rank=2),
            _rating(third_id, ScopeType.GLOBAL, None, rank=3),
        ]
    )
    users = FakeUserDirectory(inactive_ids={deleted_id})
    uc = GetLeaderboard(ratings=repo, users=users)

    board, _ = await uc.execute(scope_type=ScopeType.GLOBAL, scope_id=None)

    assert [(r.user_id, r.rank) for r in board] == [(top_id, 1), (third_id, 3)]


async def test_get_leaderboard_hides_inactive_users_without_threshold() -> None:
    """``qualified_only=False`` (админка/отладка) тоже не показывает неактивных."""
    repo = InMemoryRatingRepository()
    active_id, deleted_id = uuid.uuid4(), uuid.uuid4()
    await repo.upsert_many(
        [
            _rating(active_id, ScopeType.GLOBAL, None, rank=1, n_resolved=1),
            _rating(deleted_id, ScopeType.GLOBAL, None, rank=2, n_resolved=1),
        ]
    )
    uc = GetLeaderboard(
        ratings=repo, users=FakeUserDirectory(inactive_ids={deleted_id})
    )

    board, _ = await uc.execute(
        scope_type=ScopeType.GLOBAL, scope_id=None, qualified_only=False
    )

    assert [r.user_id for r in board] == [active_id]


async def test_get_season_leaderboard_hides_inactive_users() -> None:
    """Сезонная лига — та же фильтрация неактивных аккаунтов."""
    season_id = uuid.uuid4()
    repo = InMemoryRatingRepository()
    active_id, deleted_id = uuid.uuid4(), uuid.uuid4()
    await repo.upsert_many(
        [
            _rating(active_id, ScopeType.SEASON, season_id, rank=1),
            _rating(deleted_id, ScopeType.SEASON, season_id, rank=2),
        ]
    )
    uc = GetSeasonLeaderboard(
        ratings=repo,
        season_config=FakeSeasonConfigGateway(by_slug={"s1": season_id}),
        users=FakeUserDirectory(inactive_ids={deleted_id}),
    )

    resolved_id, board = await uc.execute(slug="s1")

    assert resolved_id == season_id
    assert [(r.user_id, r.rank) for r in board] == [(active_id, 1)]


async def test_get_user_calibration_resolves_username_and_delegates() -> None:
    user_id = uuid.uuid4()
    entries = [(0.70, 1)] * 31 + [(0.70, 0)] * 9
    gateway = FakeEventScoringGateway(user_entries={user_id: entries})
    users = FakeUserDirectory({"alice": user_id})
    uc = GetUserCalibration(gateway=gateway, users=users)

    resolved_id, report = await uc.execute(username="alice")

    assert resolved_id == user_id
    assert report.n_total == 40
    assert report.bins[0].frequency == pytest.approx(0.775, abs=1e-4)


async def test_get_user_calibration_unknown_profile_raises() -> None:
    uc = GetUserCalibration(
        gateway=FakeEventScoringGateway(), users=FakeUserDirectory()
    )
    with pytest.raises(ProfileNotFoundError):
        await uc.execute(username="ghost")


async def test_recalibrate_season_gradations_fits_observed_frequencies() -> None:
    season_id = uuid.uuid4()
    # «Скорее да» (0.70) сбывался в 80% случаев → номинал должен подрасти к 0.8.
    entries = [(0.70, 1)] * 8 + [(0.70, 0)] * 2 + [(0.90, 1)] * 9 + [(0.90, 0)] * 1
    gateway = FakeEventScoringGateway(season_entries={season_id: entries})

    result = await RecalibrateSeasonGradations(gateway=gateway).execute(
        season_id=season_id
    )

    by_nominal = {r.nominal: r for r in result}
    assert by_nominal[0.70].observed_freq == pytest.approx(0.80, abs=1e-9)
    assert by_nominal[0.70].fitted == pytest.approx(0.80, abs=1e-9)
    assert by_nominal[0.90].fitted == pytest.approx(0.90, abs=1e-9)
    # Монотонность сохранена: fitted(0.70) <= fitted(0.90).
    assert by_nominal[0.70].fitted <= by_nominal[0.90].fitted


async def test_recalibrate_enforces_monotonicity_on_inversions() -> None:
    season_id = uuid.uuid4()
    # Инверсия: 0.30 сбывается чаще (0.6), чем 0.50 (0.4) — изотония их сольёт.
    entries = [(0.30, 1)] * 6 + [(0.30, 0)] * 4 + [(0.50, 1)] * 4 + [(0.50, 0)] * 6
    gateway = FakeEventScoringGateway(season_entries={season_id: entries})

    result = await RecalibrateSeasonGradations(gateway=gateway).execute(
        season_id=season_id
    )
    fitted = [r.fitted for r in sorted(result, key=lambda r: r.nominal)]
    # После PAV последовательность неубывающая (инверсия устранена объединением).
    assert fitted == sorted(fitted)


# ── GetProfileSummary ────────────────────────────────────────────────────────


def _rating(
    user_id: uuid.UUID,
    scope_type: ScopeType,
    scope_id: uuid.UUID | None,
    *,
    rank: int,
    n_resolved: int = 10,
) -> Rating:
    return Rating(
        user_id=user_id,
        scope_type=scope_type,
        scope_id=scope_id,
        mean_brier=Decimal("0.15000"),
        skill_score=Decimal("0.05000"),
        calibration_error=Decimal("0.02000"),
        n_resolved=n_resolved,
        rank=rank,
    )


async def test_get_profile_summary_unknown_profile_raises() -> None:
    uc = GetProfileSummary(
        ratings=InMemoryRatingRepository(),
        users=FakeUserDirectory(),
        categories=FakeCategoryDirectory(),
        season_config=FakeSeasonConfigGateway(),
    )
    with pytest.raises(ProfileNotFoundError):
        await uc.execute(username="ghost")


async def test_get_profile_summary_empty_for_user_without_ratings() -> None:
    """Пользователь без разрешённых событий — пустая сводка, не ошибка."""
    user_id = uuid.uuid4()
    uc = GetProfileSummary(
        ratings=InMemoryRatingRepository(),
        users=FakeUserDirectory({"newbie": user_id}),
        categories=FakeCategoryDirectory(),
        season_config=FakeSeasonConfigGateway(),
    )

    summary = await uc.execute(username="newbie")

    assert summary.user_id == user_id
    assert summary.global_rating is None
    assert summary.categories == []
    assert summary.season_rating is None
    assert summary.active_season_id is None


async def test_get_profile_summary_assembles_global_categories_and_season() -> None:
    user_id = uuid.uuid4()
    cat_a, cat_b = uuid.uuid4(), uuid.uuid4()
    season_id = uuid.uuid4()
    other_season_id = uuid.uuid4()

    repo = InMemoryRatingRepository()
    await repo.upsert_many(
        [
            _rating(user_id, ScopeType.GLOBAL, None, rank=4),
            _rating(user_id, ScopeType.CATEGORY, cat_a, rank=2),
            _rating(user_id, ScopeType.CATEGORY, cat_b, rank=1),
            _rating(user_id, ScopeType.SEASON, season_id, rank=3),
            # Рейтинг в неактивном сезоне не должен попасть в сводку.
            _rating(user_id, ScopeType.SEASON, other_season_id, rank=1),
        ]
    )
    categories = FakeCategoryDirectory()
    categories.set(cat_a, slug="politics", title="Политика")
    categories.set(cat_b, slug="sport", title="Спорт")

    uc = GetProfileSummary(
        ratings=repo,
        users=FakeUserDirectory({"alice": user_id}),
        categories=categories,
        season_config=FakeSeasonConfigGateway(active_season_id=season_id),
    )

    summary = await uc.execute(username="alice")

    assert summary.global_rating is not None and summary.global_rating.rank == 4
    assert summary.active_season_id == season_id
    assert summary.season_rating is not None and summary.season_rating.rank == 3
    # Лучшая категория (наименьший ранг) — первой.
    assert [c.category.slug for c in summary.categories] == ["sport", "politics"]


async def test_get_profile_summary_skips_category_without_known_name() -> None:
    """Категория, удалённая из справочника, тихо выпадает из сводки (не 500)."""
    user_id = uuid.uuid4()
    unknown_cat = uuid.uuid4()
    repo = InMemoryRatingRepository()
    await repo.upsert_many([_rating(user_id, ScopeType.CATEGORY, unknown_cat, rank=1)])

    uc = GetProfileSummary(
        ratings=repo,
        users=FakeUserDirectory({"alice": user_id}),
        categories=FakeCategoryDirectory(),  # пустой справочник
        season_config=FakeSeasonConfigGateway(),
    )

    summary = await uc.execute(username="alice")
    assert summary.categories == []
