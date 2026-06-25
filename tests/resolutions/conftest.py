"""Фикстуры тестов домена resolutions.

Глобальное тест-окружение (env) выставляет ``tests/conftest.py``; здесь —
фиксированное «сейчас», окно оспаривания и собранный из фейков «стенд» со
всеми use-cases на общих in-memory портах.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.identity.domain.entities import UserRole
from app.modules.resolutions.application.dto import Actor
from app.modules.resolutions.application.use_cases import (
    CloseDisputeWindows,
    DecideDispute,
    FixResolution,
    GetResolution,
    ListDisputes,
    RaiseDispute,
)
from tests.resolutions.fakes import (
    FakeAuditTrail,
    FakeClock,
    FakeEventResolutionGateway,
    FakeParticipationGateway,
    FakeTaskScheduler,
    InMemoryDisputeRepository,
    InMemoryResolutionRepository,
    InMemoryScoringDispatchRepository,
)

FIXED_NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
DISPUTE_WINDOW = timedelta(hours=72)


@dataclass
class Stand:
    """Собранный стенд: фейки портов + готовые use-cases на общем времени."""

    clock: FakeClock
    events: FakeEventResolutionGateway
    resolutions: InMemoryResolutionRepository
    disputes: InMemoryDisputeRepository
    dispatches: InMemoryScoringDispatchRepository
    participation: FakeParticipationGateway
    tasks: FakeTaskScheduler
    audit: FakeAuditTrail
    fix: FixResolution
    get: GetResolution
    list_disputes: ListDisputes
    raise_dispute: RaiseDispute
    decide: DecideDispute
    close_windows: CloseDisputeWindows


@pytest.fixture
def stand() -> Stand:
    """Стенд с фейками и use-cases (общие часы ``FIXED_NOW``, окно 72ч)."""
    clock = FakeClock(FIXED_NOW)
    events = FakeEventResolutionGateway()
    resolutions = InMemoryResolutionRepository()
    disputes = InMemoryDisputeRepository()
    dispatches = InMemoryScoringDispatchRepository()
    participation = FakeParticipationGateway()
    tasks = FakeTaskScheduler()
    audit = FakeAuditTrail()
    return Stand(
        clock=clock,
        events=events,
        resolutions=resolutions,
        disputes=disputes,
        dispatches=dispatches,
        participation=participation,
        tasks=tasks,
        audit=audit,
        fix=FixResolution(
            resolutions=resolutions,
            events=events,
            audit=audit,
            clock=clock,
            dispute_window=DISPUTE_WINDOW,
        ),
        get=GetResolution(resolutions=resolutions),
        list_disputes=ListDisputes(disputes=disputes),
        raise_dispute=RaiseDispute(
            disputes=disputes,
            resolutions=resolutions,
            events=events,
            participation=participation,
            audit=audit,
            clock=clock,
        ),
        decide=DecideDispute(
            disputes=disputes,
            resolutions=resolutions,
            events=events,
            audit=audit,
            clock=clock,
            dispute_window=DISPUTE_WINDOW,
        ),
        close_windows=CloseDisputeWindows(
            events=events,
            resolutions=resolutions,
            disputes=disputes,
            dispatches=dispatches,
            tasks=tasks,
            audit=audit,
            clock=clock,
        ),
    )


@pytest.fixture
def editor() -> Actor:
    return Actor(user_id=uuid.uuid4(), role=UserRole.EDITOR)


@pytest.fixture
def arbiter() -> Actor:
    return Actor(user_id=uuid.uuid4(), role=UserRole.ARBITER)


@pytest.fixture
def participant() -> Actor:
    return Actor(user_id=uuid.uuid4(), role=UserRole.USER)
