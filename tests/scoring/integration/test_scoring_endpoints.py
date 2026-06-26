"""Интеграционные тесты HTTP-эндпоинтов scoring.

Поднимают реальное FastAPI-приложение, но I/O-порты (шлюз, писатель,
репозиторий рейтингов, часы) и аутентификацию подменяют фейками через
``dependency_overrides``. БД-интеграция с Postgres (UNIQUE области, enum
rating_scope, FK) — отдельным e2e (TODO).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from app.modules.scoring.api.dependencies import (
    get_clock,
    get_dispute_guard,
    get_event_scoring_gateway,
    get_prediction_score_writer,
    get_rating_repository,
    get_season_config_gateway,
    get_season_repository,
    get_user_directory,
)
from app.modules.scoring.application.dto import EventScoringStatus, SeasonConfigView
from app.modules.scoring.application.use_cases import RecomputeRatings
from app.modules.scoring.domain.entities import Rating, ScopeType
from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.value_objects import LeagueConfig
from tests.scoring.conftest import FIXED_NOW, make_event
from tests.scoring.fakes import (
    FakeClock,
    FakeEventScoringGateway,
    FakePredictionScoreWriter,
    FakeSeasonConfigGateway,
    FakeUserDirectory,
    InMemoryRatingRepository,
)
from tests.seasons.fakes import FakeDisputeGuard, InMemorySeasonRepository


def _user(role: UserRole = UserRole.USER) -> User:
    return User(
        esia_oid="oid",
        snils_hash="hash",
        username="predictor1",
        display_name="Предсказатель",
        real_name_enc=None,
        role=role,
    )


@pytest.fixture
def make_client():
    """Фабрика клиента: общие фейки + управляемая роль/аутентификация."""
    created: list[TestClient] = []

    def _build(
        *,
        gateway: FakeEventScoringGateway | None = None,
        repo: InMemoryRatingRepository | None = None,
        writer: FakePredictionScoreWriter | None = None,
        season_config: FakeSeasonConfigGateway | None = None,
        season_repo: InMemorySeasonRepository | None = None,
        dispute_guard: FakeDisputeGuard | None = None,
        users: FakeUserDirectory | None = None,
        role: UserRole | None = UserRole.USER,
    ):
        gateway = gateway or FakeEventScoringGateway()
        repo = repo or InMemoryRatingRepository()
        writer = writer or FakePredictionScoreWriter()
        season_config = season_config or FakeSeasonConfigGateway()
        season_repo = season_repo or InMemorySeasonRepository()
        dispute_guard = dispute_guard or FakeDisputeGuard()
        users = users or FakeUserDirectory()

        app = create_app()
        app.dependency_overrides[get_event_scoring_gateway] = lambda: gateway
        app.dependency_overrides[get_prediction_score_writer] = lambda: writer
        app.dependency_overrides[get_rating_repository] = lambda: repo
        app.dependency_overrides[get_season_config_gateway] = lambda: season_config
        app.dependency_overrides[get_season_repository] = lambda: season_repo
        app.dependency_overrides[get_dispute_guard] = lambda: dispute_guard
        app.dependency_overrides[get_user_directory] = lambda: users
        app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)
        if role is not None:
            app.dependency_overrides[get_current_user] = lambda: _user(role)

        client = TestClient(app)
        created.append(client)
        return client, gateway, repo, writer

    yield _build
    for client in created:
        client.close()


async def _seed_ratings(repo: InMemoryRatingRepository) -> tuple[uuid.UUID, list]:
    category_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    event, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        category_id=category_id,
        user_ids=ids,
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    await RecomputeRatings(
        gateway=gateway,
        ratings=repo,
        clock=FakeClock(FIXED_NOW),
        season_config=FakeSeasonConfigGateway(),
    ).execute()
    return category_id, ids


# ── Лидерборды ──────────────────────────────────────────────────────────────


async def test_global_leaderboard_returns_ranked_entries(make_client) -> None:
    repo = InMemoryRatingRepository()
    _, ids = await _seed_ratings(repo)
    client, _, _, _ = make_client(repo=repo)

    resp = client.get("/leaderboards/global")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_type"] == "global"
    assert body["entries"][0]["rank"] == 1
    assert body["entries"][0]["user_id"] == str(ids[4])  # игрок с 0.3 — №1


async def test_category_leaderboard_scoped(make_client) -> None:
    repo = InMemoryRatingRepository()
    category_id, _ = await _seed_ratings(repo)
    client, _, _, _ = make_client(repo=repo)

    resp = client.get(f"/leaderboards/categories/{category_id}?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_type"] == "category"
    assert len(body["entries"]) == 3


# ── Калибровка ──────────────────────────────────────────────────────────────


def test_user_calibration_endpoint(make_client) -> None:
    user_id = uuid.uuid4()
    entries = [(0.70, 1)] * 31 + [(0.70, 0)] * 9
    gateway = FakeEventScoringGateway(user_entries={user_id: entries})
    users = FakeUserDirectory({"alice": user_id})
    client, _, _, _ = make_client(gateway=gateway, users=users)

    resp = client.get("/users/alice/calibration")
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_total"] == 40
    assert body["bins"][0]["frequency"] == pytest.approx(0.775, abs=1e-4)
    assert body["ece"] == pytest.approx(0.075, abs=1e-4)


def test_user_calibration_unknown_profile_404(make_client) -> None:
    client, _, _, _ = make_client()  # пустой UserDirectory
    resp = client.get("/users/ghost/calibration")
    assert resp.status_code == 404


# ── Скоринг события (RBAC) ───────────────────────────────────────────────────


def test_score_event_requires_elevated_role(make_client) -> None:
    event, _ = make_event(outcome=1, probabilities=[0.9, 0.7])
    gateway = FakeEventScoringGateway(
        statuses={
            event.event_id: EventScoringStatus(
                found=True, is_resolved=True, is_final=True, outcome=1
            )
        },
        events={event.event_id: event},
    )
    client, _, _, _ = make_client(gateway=gateway, role=UserRole.USER)

    resp = client.post(f"/admin/events/{event.event_id}/score")
    assert resp.status_code == 403
    assert resp.json()["error"] == "ScoringPermissionError"


def test_score_event_as_editor(make_client) -> None:
    event, ids = make_event(outcome=1, probabilities=[0.9, 0.7])
    gateway = FakeEventScoringGateway(
        statuses={
            event.event_id: EventScoringStatus(
                found=True, is_resolved=True, is_final=True, outcome=1
            )
        },
        events={event.event_id: event},
    )
    writer = FakePredictionScoreWriter()
    client, _, _, _ = make_client(gateway=gateway, writer=writer, role=UserRole.EDITOR)

    resp = client.post(f"/admin/events/{event.event_id}/score")
    assert resp.status_code == 200
    assert resp.json()["scored"] == 2
    assert event.event_id in writer.saved


def test_score_unresolved_event_conflict(make_client) -> None:
    event_id = uuid.uuid4()
    gateway = FakeEventScoringGateway(
        statuses={
            event_id: EventScoringStatus(
                found=True, is_resolved=False, is_final=False, outcome=None
            )
        }
    )
    client, _, _, _ = make_client(gateway=gateway, role=UserRole.EDITOR)

    resp = client.post(f"/admin/events/{event_id}/score")
    assert resp.status_code == 409
    assert resp.json()["error"] == "EventNotResolvedError"


def test_score_missing_event_404(make_client) -> None:
    client, _, _, _ = make_client(role=UserRole.EDITOR)
    resp = client.post(f"/admin/events/{uuid.uuid4()}/score")
    assert resp.status_code == 404
    assert resp.json()["error"] == "ScoringTargetEventNotFoundError"


# ── Пересчёт рейтингов (RBAC) ────────────────────────────────────────────────


def test_recompute_requires_admin(make_client) -> None:
    client, _, _, _ = make_client(role=UserRole.EDITOR)
    resp = client.post("/admin/ratings/recompute")
    assert resp.status_code == 403


def test_season_recalibration_endpoint(make_client) -> None:
    season_id = uuid.uuid4()
    entries = [(0.70, 1)] * 8 + [(0.70, 0)] * 2  # «Скорее да» сбывался в 80%
    gateway = FakeEventScoringGateway(season_entries={season_id: entries})
    client, _, _, _ = make_client(gateway=gateway, role=UserRole.ADMIN)

    resp = client.get(f"/admin/seasons/{season_id}/recalibration")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body[0]["nominal"] == pytest.approx(0.70)
    assert body[0]["observed_freq"] == pytest.approx(0.80, abs=1e-9)
    assert body[0]["fitted"] == pytest.approx(0.80, abs=1e-9)


def test_season_recalibration_requires_admin(make_client) -> None:
    client, _, _, _ = make_client(role=UserRole.EDITOR)
    resp = client.get(f"/admin/seasons/{uuid.uuid4()}/recalibration")
    assert resp.status_code == 403


async def test_recompute_as_admin(make_client) -> None:
    ids = [uuid.uuid4() for _ in range(5)]
    event, _ = make_event(
        outcome=0, probabilities=[0.9, 0.9, 0.9, 0.9, 0.3], user_ids=ids
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    repo = InMemoryRatingRepository()
    client, _, _, _ = make_client(gateway=gateway, repo=repo, role=UserRole.ADMIN)

    resp = client.post("/admin/ratings/recompute")
    assert resp.status_code == 200
    # 5 пользователей × 2 области (global + category) = 10 рейтингов.
    assert resp.json()["upserted"] == 10
    board = await repo.leaderboard(ScopeType.GLOBAL, None)
    assert board[0].user_id == ids[4]


def test_leaderboard_requires_no_auth(make_client) -> None:
    """Лидерборды публичны — доступны без аутентификации."""
    repo = InMemoryRatingRepository()
    client, _, _, _ = make_client(repo=repo, role=None)
    resp = client.get("/leaderboards/global")
    assert resp.status_code == 200


# ── Сезонные лидерборды и квалификация ───────────────────────────────────────

_EASY_CFG = LeagueConfig(
    gradation_map=(0.1, 0.3, 0.5, 0.7, 0.9),
    n_min=1,
    c_min=1,
    w_min=0.0,
    m_per_category=1,
    k_shrink=6.0,
    min_predictors=5,
)


def _season_rating(season_id: uuid.UUID, *, rank: int, qualified: bool) -> Rating:
    return Rating(
        user_id=uuid.uuid4(),
        scope_type=ScopeType.SEASON,
        scope_id=season_id,
        mean_brier=Decimal("0.20000"),
        skill_score=Decimal("0.10000"),
        calibration_error=Decimal("0.05000"),
        n_resolved=30,
        rank=rank,
        qualified=qualified,
    )


async def test_season_leaderboard_by_slug_filters_qualified(make_client) -> None:
    season_id = uuid.uuid4()
    repo = InMemoryRatingRepository()
    qualified = _season_rating(season_id, rank=1, qualified=True)
    unqualified = _season_rating(season_id, rank=2, qualified=False)
    await repo.upsert_many([qualified, unqualified])

    season_config = FakeSeasonConfigGateway(by_slug={"2026q3": season_id})
    client, _, _, _ = make_client(
        repo=repo, season_config=season_config, role=None
    )

    full = client.get("/leaderboards/seasons/2026q3")
    assert full.status_code == 200
    assert full.json()["scope_id"] == str(season_id)
    assert len(full.json()["entries"]) == 2

    only = client.get(
        "/leaderboards/seasons/2026q3", params={"qualified_only": "true"}
    )
    assert only.status_code == 200
    entries = only.json()["entries"]
    assert [e["user_id"] for e in entries] == [str(qualified.user_id)]
    assert entries[0]["qualified"] is True


def test_season_leaderboard_unknown_slug_404(make_client) -> None:
    client, _, _, _ = make_client(role=None)
    resp = client.get("/leaderboards/seasons/missing")
    assert resp.status_code == 404
    assert resp.json()["error"] == "SeasonNotFoundError"


async def test_user_season_qualification_breakdown(make_client) -> None:
    season_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(4)] + [user_id]
    event, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        season_id=season_id,
        user_ids=ids,
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    season_config = FakeSeasonConfigGateway(
        by_slug={"2026q3": season_id},
        configs={
            season_id: SeasonConfigView(status=SeasonStatus.ACTIVE, config=_EASY_CFG)
        },
    )
    client, _, _, _ = make_client(
        gateway=gateway, season_config=season_config, role=None
    )

    resp = client.get(f"/users/{user_id}/seasons/2026q3/qualification")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["n_resolved"] == 1
    assert body["qualified"] is True  # мягкий конфиг квалифицирует с одного события


def test_qualification_404_when_season_not_activated(make_client) -> None:
    season_id = uuid.uuid4()
    season_config = FakeSeasonConfigGateway(
        by_slug={"2026q3": season_id},
        configs={
            season_id: SeasonConfigView(status=SeasonStatus.UPCOMING, config=None)
        },
    )
    client, _, _, _ = make_client(season_config=season_config, role=None)
    resp = client.get(f"/users/{uuid.uuid4()}/seasons/2026q3/qualification")
    assert resp.status_code == 404


# ── Финализация сезона (ручной admin-триггер) ────────────────────────────────


async def test_finalize_endpoint_finalizes_and_records_snapshot(make_client) -> None:
    season_id = uuid.uuid4()
    season = Season(
        slug="2026q3",
        title="Сезон III",
        starts_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        status=SeasonStatus.ACTIVE,
        league_config=_EASY_CFG,
        id=season_id,
    )
    season_repo = InMemorySeasonRepository()
    await season_repo.add(season)
    event, _ = make_event(
        outcome=0, probabilities=[0.9, 0.9, 0.9, 0.9, 0.3], season_id=season_id
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    season_config = FakeSeasonConfigGateway(
        configs={
            season_id: SeasonConfigView(status=SeasonStatus.ACTIVE, config=_EASY_CFG)
        }
    )
    client, _, _, _ = make_client(
        gateway=gateway,
        repo=InMemoryRatingRepository(),
        season_config=season_config,
        season_repo=season_repo,
        role=UserRole.ADMIN,
    )

    resp = client.post(f"/admin/seasons/{season_id}/finalize")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["finalized"] is True
    assert body["qualified_count"] == 5

    finished = await season_repo.get_by_id(season_id)
    assert finished is not None and finished.status is SeasonStatus.FINISHED
    assert len(season_repo.finalizations) == 1


def test_finalize_requires_admin(make_client) -> None:
    client, _, _, _ = make_client(role=UserRole.EDITOR)
    resp = client.post(f"/admin/seasons/{uuid.uuid4()}/finalize")
    assert resp.status_code == 403
