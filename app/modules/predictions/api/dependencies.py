"""Composition root модуля predictions (FastAPI DI).

Здесь — и только здесь — конкретные адаптеры связываются с портами и
собираются use-cases. В тестах достаточно переопределить несколько
провайдеров (репозиторий, шлюз events, часы, аудит) и аутентификацию.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.modules.events.adapters.repository import SqlAlchemyEventRepository
from app.modules.predictions.adapters.audit_trail import AuditTrailRecorder
from app.modules.predictions.adapters.clock import SystemClock
from app.modules.predictions.adapters.event_gateway import EventRepositoryGateway
from app.modules.predictions.adapters.repository import (
    SqlAlchemyPredictionRepository,
)
from app.modules.predictions.application.use_cases import (
    GetMyPrediction,
    LockEventPredictions,
    PlacePrediction,
)
from app.modules.predictions.ports.audit import AuditRecorder
from app.modules.predictions.ports.clock import Clock
from app.modules.predictions.ports.events import EventGateway
from app.modules.predictions.ports.repositories import PredictionRepository
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Порты → адаптеры ──────────────────────────────────────────────────────


def get_prediction_repository(session: SessionDep) -> PredictionRepository:
    """Репозиторий прогнозов."""
    return SqlAlchemyPredictionRepository(session)


def get_event_gateway(session: SessionDep) -> EventGateway:
    """Шлюз к состоянию событий (поверх репозитория events).

    TODO(events-integration): прямое чтение events в монолите; вынести за
    сетевой контракт при выделении events в отдельный сервис.
    """
    return EventRepositoryGateway(SqlAlchemyEventRepository(session))


def get_clock() -> Clock:
    """Серверные часы (переопределяются в тестах фиксированными)."""
    return SystemClock()


def get_audit_recorder(session: SessionDep) -> AuditRecorder:
    """Приёмник истории прогнозов — общий append-only журнал с хеш-цепочкой."""
    return AuditTrailRecorder(SqlAlchemyAuditTrail(session))


PredictionRepoDep = Annotated[
    PredictionRepository, Depends(get_prediction_repository)
]
EventGatewayDep = Annotated[EventGateway, Depends(get_event_gateway)]
ClockDep = Annotated[Clock, Depends(get_clock)]
AuditDep = Annotated[AuditRecorder, Depends(get_audit_recorder)]


# ── Use-cases ─────────────────────────────────────────────────────────────


def get_place_prediction(
    predictions: PredictionRepoDep,
    events: EventGatewayDep,
    clock: ClockDep,
    audit: AuditDep,
) -> PlacePrediction:
    """Use-case постановки/изменения прогноза."""
    return PlacePrediction(
        predictions=predictions, events=events, clock=clock, audit=audit
    )


def get_my_prediction(predictions: PredictionRepoDep) -> GetMyPrediction:
    """Use-case чтения своего прогноза."""
    return GetMyPrediction(predictions=predictions)


def get_lock_event_predictions(
    predictions: PredictionRepoDep, clock: ClockDep
) -> LockEventPredictions:
    """Use-case массовой блокировки прогнозов события.

    TODO(events-integration): дёргается доменом events при закрытии приёма.
    """
    return LockEventPredictions(predictions=predictions, clock=clock)
