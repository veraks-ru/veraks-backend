"""Composition root модуля events (FastAPI DI).

Здесь — и только здесь — конкретные адаптеры связываются с портами и
собираются use-cases. Благодаря этому в тестах достаточно переопределить
несколько провайдеров (репозитории, часы), а крипто/идентификацию оставить
реальными.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.modules.events.adapters.clock import SystemClock
from app.modules.events.adapters.repository import (
    SqlAlchemyCategoryRepository,
    SqlAlchemyEventRepository,
)
from app.modules.events.application.dto import Actor
from app.modules.events.application.use_cases import (
    CancelEvent,
    CreateCategory,
    CreateEvent,
    GetEvent,
    ListCategories,
    ListEvents,
    PublishEvent,
    CloseEvent,
    UpdateEvent,
)
from app.modules.events.ports.clock import Clock
from app.modules.events.ports.repositories import CategoryRepository, EventRepository
from app.modules.identity.api.dependencies import CurrentUser
from app.modules.predictions.adapters.clock import SystemClock as PredictionsClock
from app.modules.predictions.adapters.repository import SqlAlchemyPredictionRepository
from app.modules.predictions.application.use_cases import LockEventPredictions
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from app.shared.audit.ports.audit_trail import AuditTrail

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Порты → адаптеры ──────────────────────────────────────────────────────


def get_event_repository(session: SessionDep) -> EventRepository:
    """Репозиторий событий."""
    return SqlAlchemyEventRepository(session)


def get_category_repository(session: SessionDep) -> CategoryRepository:
    """Репозиторий категорий."""
    return SqlAlchemyCategoryRepository(session)


def get_clock() -> Clock:
    """Серверные часы (переопределяются в тестах фиксированными)."""
    return SystemClock()


def get_audit_trail(session: SessionDep) -> AuditTrail:
    """Общий append-only журнал с хеш-цепочкой."""
    return SqlAlchemyAuditTrail(session)


EventRepoDep = Annotated[EventRepository, Depends(get_event_repository)]
CategoryRepoDep = Annotated[CategoryRepository, Depends(get_category_repository)]
ClockDep = Annotated[Clock, Depends(get_clock)]
AuditDep = Annotated[AuditTrail, Depends(get_audit_trail)]


# ── Актор (RBAC) ──────────────────────────────────────────────────────────


def get_actor(current_user: CurrentUser) -> Actor:
    """Актор операции из аутентифицированного пользователя identity.

    Проверка достаточности роли — в доменной политике (use-case), здесь лишь
    переносим id и роль в нейтральный DTO.
    """
    return Actor(user_id=current_user.id, role=current_user.role)


ActorDep = Annotated[Actor, Depends(get_actor)]


# ── Use-cases ─────────────────────────────────────────────────────────────


def get_create_event(
    events: EventRepoDep, categories: CategoryRepoDep, clock: ClockDep, audit: AuditDep
) -> CreateEvent:
    """Use-case создания события."""
    return CreateEvent(events=events, categories=categories, clock=clock, audit=audit)


def get_update_event(
    events: EventRepoDep, categories: CategoryRepoDep, clock: ClockDep, audit: AuditDep
) -> UpdateEvent:
    """Use-case редактирования события."""
    return UpdateEvent(events=events, categories=categories, clock=clock, audit=audit)


def get_publish_event(events: EventRepoDep, clock: ClockDep, audit: AuditDep) -> PublishEvent:
    """Use-case публикации события."""
    return PublishEvent(events=events, clock=clock, audit=audit)


def get_close_event(events: EventRepoDep, clock: ClockDep, audit: AuditDep) -> CloseEvent:
    """Use-case закрытия приёма прогнозов."""
    return CloseEvent(events=events, clock=clock, audit=audit)


def get_lock_event_predictions(session: SessionDep) -> LockEventPredictions:
    """Композит-рут HTTP: блокировка прогнозов при закрытии приёма.

    Переход ``open → closed`` должен замораживать прогнозы так же, как это делает
    фоновый воркер по наступлению ``closes_at`` (см. ``LockEventPredictions``):
    иначе вручную закрытое событие не скорится (его прогнозы не ``is_locked``).
    Единственное место, где events-API знает о predictions — как воркер знает
    оба домена.
    """
    return LockEventPredictions(
        predictions=SqlAlchemyPredictionRepository(session),
        clock=PredictionsClock(),
    )


def get_cancel_event(events: EventRepoDep, clock: ClockDep, audit: AuditDep) -> CancelEvent:
    """Use-case отмены события."""
    return CancelEvent(events=events, clock=clock, audit=audit)


def get_get_event(events: EventRepoDep) -> GetEvent:
    """Use-case чтения события."""
    return GetEvent(events=events)


def get_list_events(events: EventRepoDep) -> ListEvents:
    """Use-case списка событий."""
    return ListEvents(events=events)


def get_create_category(categories: CategoryRepoDep) -> CreateCategory:
    """Use-case создания категории."""
    return CreateCategory(categories=categories)


def get_list_categories(categories: CategoryRepoDep) -> ListCategories:
    """Use-case списка категорий."""
    return ListCategories(categories=categories)
