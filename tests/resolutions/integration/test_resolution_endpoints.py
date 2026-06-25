"""Интеграционные тесты HTTP-эндпоинтов resolutions.

Поднимают реальное FastAPI-приложение, но I/O-порты (репозитории, шлюзы,
аудит, часы) и аутентификацию подменяют фейками через ``dependency_overrides``.
Один и тот же набор фейков переживает все запросы клиента, а активный
пользователь переключается мутабельным холдером (разные роли в одном сценарии).

БД-инварианты (триггеры append-only ``resolutions``/``audit_log``, UNIQUE,
enum) проверяются отдельно e2e против Postgres.

TODO(resolutions-infra): добавить e2e против реального Postgres (testcontainers)
для триггеров append-only и FK на events/users.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.events.domain.entities import EventStatus
from app.modules.identity.api.dependencies import get_current_user
from app.modules.identity.domain.entities import User, UserRole
from app.modules.resolutions.api.dependencies import (
    get_audit_trail,
    get_clock,
    get_dispute_repository,
    get_event_gateway,
    get_participation_gateway,
    get_resolution_repository,
)
from tests.resolutions.conftest import FIXED_NOW
from tests.resolutions.fakes import (
    FakeAuditTrail,
    FakeClock,
    FakeEventResolutionGateway,
    FakeParticipationGateway,
    InMemoryDisputeRepository,
    InMemoryResolutionRepository,
)


def _user(role: UserRole) -> User:
    """Аутентифицированный пользователь с заданной ролью."""
    return User(
        esia_oid=f"oid-{uuid.uuid4()}",
        snils_hash=f"hash-{uuid.uuid4()}",
        username=f"user-{uuid.uuid4().hex[:8]}",
        display_name="Тест",
        real_name_enc=None,
        role=role,
    )


@dataclass
class Ctx:
    """Контекст интеграционного клиента с доступом к фейкам."""

    client: TestClient
    events: FakeEventResolutionGateway
    participation: FakeParticipationGateway
    holder: dict


@pytest.fixture
def ctx():
    """Клиент с общими фейками и переключаемым активным пользователем."""
    events = FakeEventResolutionGateway()
    resolutions = InMemoryResolutionRepository()
    disputes = InMemoryDisputeRepository()
    participation = FakeParticipationGateway()
    audit = FakeAuditTrail()
    holder: dict = {"user": None}

    app = create_app()
    app.dependency_overrides[get_resolution_repository] = lambda: resolutions
    app.dependency_overrides[get_dispute_repository] = lambda: disputes
    app.dependency_overrides[get_event_gateway] = lambda: events
    app.dependency_overrides[get_participation_gateway] = lambda: participation
    app.dependency_overrides[get_audit_trail] = lambda: audit
    app.dependency_overrides[get_clock] = lambda: FakeClock(FIXED_NOW)

    def _current_user() -> User:
        user = holder["user"]
        if user is None:  # имитация отсутствия аутентификации
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return user

    app.dependency_overrides[get_current_user] = _current_user

    client = TestClient(app)
    try:
        yield Ctx(client=client, events=events, participation=participation, holder=holder)
    finally:
        client.close()


def _act_as(ctx: Ctx, user: User) -> None:
    ctx.holder["user"] = user


# ── Фиксация исхода ─────────────────────────────────────────────────────────


def test_fix_requires_auth(ctx) -> None:
    event_id = uuid.uuid4()
    ctx.events.seed(event_id, status=EventStatus.CLOSED)
    resp = ctx.client.post(
        f"/events/{event_id}/resolution",
        json={"outcome": True, "source_reference": "src"},
    )
    assert resp.status_code == 401


def test_fix_forbidden_for_user(ctx) -> None:
    event_id = uuid.uuid4()
    ctx.events.seed(event_id, status=EventStatus.CLOSED)
    _act_as(ctx, _user(UserRole.USER))
    resp = ctx.client.post(
        f"/events/{event_id}/resolution",
        json={"outcome": True, "source_reference": "src"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "ResolutionPermissionError"


def test_fix_and_get_resolution(ctx) -> None:
    event_id = uuid.uuid4()
    ctx.events.seed(event_id, status=EventStatus.CLOSED)
    _act_as(ctx, _user(UserRole.EDITOR))

    created = ctx.client.post(
        f"/events/{event_id}/resolution",
        json={"outcome": True, "source_reference": "https://src.example"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "final"
    assert body["outcome"] is True

    fetched = ctx.client.get(f"/events/{event_id}/resolution")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]


def test_fix_unresolvable_event_conflict(ctx) -> None:
    event_id = uuid.uuid4()
    ctx.events.seed(event_id, status=EventStatus.OPEN)
    _act_as(ctx, _user(UserRole.EDITOR))
    resp = ctx.client.post(
        f"/events/{event_id}/resolution",
        json={"outcome": True, "source_reference": "src"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "EventNotResolvableError"


def test_get_missing_resolution_404(ctx) -> None:
    resp = ctx.client.get(f"/events/{uuid.uuid4()}/resolution")
    assert resp.status_code == 404


# ── Споры ───────────────────────────────────────────────────────────────────


def _resolve(ctx: Ctx, event_id: uuid.UUID) -> None:
    """Фиксирует исход события редактором (через API)."""
    _act_as(ctx, _user(UserRole.EDITOR))
    resp = ctx.client.post(
        f"/events/{event_id}/resolution",
        json={"outcome": True, "source_reference": "https://src.example"},
    )
    assert resp.status_code == 201


def test_raise_dispute_by_participant(ctx) -> None:
    event_id = uuid.uuid4()
    ctx.events.seed(event_id, status=EventStatus.CLOSED)
    _resolve(ctx, event_id)

    participant = _user(UserRole.USER)
    ctx.participation.allow(participant.id, event_id)
    _act_as(ctx, participant)

    resp = ctx.client.post(
        f"/events/{event_id}/disputes",
        json={"reason": "Источник опровергает исход"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "open"
    assert ctx.events.status_of(event_id) is EventStatus.DISPUTED

    listed = ctx.client.get(f"/events/{event_id}/disputes")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_raise_dispute_non_participant_forbidden(ctx) -> None:
    event_id = uuid.uuid4()
    ctx.events.seed(event_id, status=EventStatus.CLOSED)
    _resolve(ctx, event_id)
    _act_as(ctx, _user(UserRole.USER))
    resp = ctx.client.post(
        f"/events/{event_id}/disputes", json={"reason": "нет прогноза"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "DisputeNotAllowedError"


def _open_dispute(ctx: Ctx, event_id: uuid.UUID) -> str:
    """Доводит событие до открытого спора; возвращает dispute_id."""
    ctx.events.seed(event_id, status=EventStatus.CLOSED)
    _resolve(ctx, event_id)
    participant = _user(UserRole.USER)
    ctx.participation.allow(participant.id, event_id)
    _act_as(ctx, participant)
    resp = ctx.client.post(
        f"/events/{event_id}/disputes", json={"reason": "спорно"}
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_decide_reject(ctx) -> None:
    event_id = uuid.uuid4()
    dispute_id = _open_dispute(ctx, event_id)

    _act_as(ctx, _user(UserRole.ARBITER))
    resp = ctx.client.post(
        f"/disputes/{dispute_id}/decision",
        json={"accept": False, "decision_notes": "без оснований"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert ctx.events.status_of(event_id) is EventStatus.RESOLVED


def test_decide_accept_overturns(ctx) -> None:
    event_id = uuid.uuid4()
    dispute_id = _open_dispute(ctx, event_id)

    _act_as(ctx, _user(UserRole.ARBITER))
    resp = ctx.client.post(
        f"/disputes/{dispute_id}/decision",
        json={"accept": True, "new_outcome": False, "decision_notes": "пересмотр"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    current = ctx.client.get(f"/events/{event_id}/resolution").json()
    assert current["outcome"] is False
    assert current["supersedes_id"] is not None


def test_decide_accept_without_outcome_400(ctx) -> None:
    event_id = uuid.uuid4()
    dispute_id = _open_dispute(ctx, event_id)
    _act_as(ctx, _user(UserRole.ARBITER))
    resp = ctx.client.post(
        f"/disputes/{dispute_id}/decision", json={"accept": True}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "InvalidResolutionDataError"
