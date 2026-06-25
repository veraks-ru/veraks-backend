"""Composition root домена resolutions (FastAPI DI).

Здесь — и только здесь — порты связываются с конкретными адаптерами и
собираются use-cases. В тестах достаточно переопределить провайдеры портов
(репозитории, шлюзы, аудит, часы) и аутентификацию через ``dependency_overrides``.

HTTP-слой не ставит фоновые задачи: постановка ``score_event`` живёт в воркере
(``CloseDisputeWindows``), которому доступен arq-пул.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SettingsDep
from app.db.session import get_session
from app.modules.identity.api.dependencies import CurrentUser
from app.modules.resolutions.adapters.clock import SystemClock
from app.modules.resolutions.adapters.event_gateway import (
    SqlAlchemyEventResolutionGateway,
)
from app.modules.resolutions.adapters.participation_gateway import (
    SqlAlchemyParticipationGateway,
)
from app.modules.resolutions.adapters.repositories import (
    SqlAlchemyDisputeRepository,
    SqlAlchemyResolutionRepository,
)
from app.modules.resolutions.application.dto import Actor
from app.modules.resolutions.application.use_cases import (
    DecideDispute,
    FixResolution,
    GetResolution,
    ListDisputes,
    RaiseDispute,
)
from app.modules.resolutions.ports.clock import Clock
from app.modules.resolutions.ports.gateways import (
    EventResolutionGateway,
    ParticipationGateway,
)
from app.modules.resolutions.ports.repositories import (
    DisputeRepository,
    ResolutionRepository,
)
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from app.shared.audit.ports.audit_trail import AuditTrail

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Порты → адаптеры ──────────────────────────────────────────────────────


def get_clock() -> Clock:
    """Серверные часы (переопределяются в тестах фиксированными)."""
    return SystemClock()


def get_resolution_repository(session: SessionDep) -> ResolutionRepository:
    """Репозиторий журнала решений."""
    return SqlAlchemyResolutionRepository(session)


def get_dispute_repository(session: SessionDep) -> DisputeRepository:
    """Репозиторий споров."""
    return SqlAlchemyDisputeRepository(session)


def get_event_gateway(session: SessionDep) -> EventResolutionGateway:
    """Шлюз смены статуса события (поверх таблицы events)."""
    return SqlAlchemyEventResolutionGateway(session)


def get_participation_gateway(session: SessionDep) -> ParticipationGateway:
    """Шлюз проверки участия (поверх таблицы predictions)."""
    return SqlAlchemyParticipationGateway(session)


def get_audit_trail(session: SessionDep) -> AuditTrail:
    """Неизменяемый аудит-журнал (общая инфраструктура)."""
    return SqlAlchemyAuditTrail(session)


ClockDep = Annotated[Clock, Depends(get_clock)]
ResolutionRepoDep = Annotated[ResolutionRepository, Depends(get_resolution_repository)]
DisputeRepoDep = Annotated[DisputeRepository, Depends(get_dispute_repository)]
EventGatewayDep = Annotated[EventResolutionGateway, Depends(get_event_gateway)]
ParticipationDep = Annotated[ParticipationGateway, Depends(get_participation_gateway)]
AuditDep = Annotated[AuditTrail, Depends(get_audit_trail)]


# ── Актор (RBAC/SoD) ──────────────────────────────────────────────────────


def get_actor(current_user: CurrentUser) -> Actor:
    """Актор операции из аутентифицированного пользователя identity."""
    return Actor(user_id=current_user.id, role=current_user.role)


ActorDep = Annotated[Actor, Depends(get_actor)]


# ── Use-cases ─────────────────────────────────────────────────────────────


def get_fix_resolution(
    resolutions: ResolutionRepoDep,
    events: EventGatewayDep,
    audit: AuditDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> FixResolution:
    """Use-case фиксации исхода."""
    return FixResolution(
        resolutions=resolutions,
        events=events,
        audit=audit,
        clock=clock,
        dispute_window=settings.resolutions.dispute_window,
    )


def get_resolution(resolutions: ResolutionRepoDep) -> GetResolution:
    """Use-case чтения текущего решения."""
    return GetResolution(resolutions=resolutions)


def get_list_disputes(disputes: DisputeRepoDep) -> ListDisputes:
    """Use-case списка споров события."""
    return ListDisputes(disputes=disputes)


def get_raise_dispute(
    disputes: DisputeRepoDep,
    resolutions: ResolutionRepoDep,
    events: EventGatewayDep,
    participation: ParticipationDep,
    audit: AuditDep,
    clock: ClockDep,
) -> RaiseDispute:
    """Use-case подачи оспаривания."""
    return RaiseDispute(
        disputes=disputes,
        resolutions=resolutions,
        events=events,
        participation=participation,
        audit=audit,
        clock=clock,
    )


def get_decide_dispute(
    disputes: DisputeRepoDep,
    resolutions: ResolutionRepoDep,
    events: EventGatewayDep,
    audit: AuditDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> DecideDispute:
    """Use-case решения по спору (reject/overturn)."""
    return DecideDispute(
        disputes=disputes,
        resolutions=resolutions,
        events=events,
        audit=audit,
        clock=clock,
        dispute_window=settings.resolutions.dispute_window,
    )
