"""Use-cases домена resolutions (по одному классу на операцию).

Каждый use-case оркеструет порты, полученные через конструктор, и не знает о
FastAPI/SQLAlchemy. Значимые изменения состояния пишутся в неизменяемый
``audit_log`` через порт ``AuditTrail``. Смена статуса события идёт только через
``EventResolutionGateway`` (владелец автомата — домен events).

Поток: ``FixResolution`` (closed→resolved, окно) → ``RaiseDispute``
(resolved→disputed) → ``DecideDispute`` (reject→resolved | accept→overturn) →
``CloseDisputeWindows`` (по истечении окна без споров ставит ``score_event``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.modules.events.domain.entities import EventStatus
from app.modules.identity.domain.entities import UserRole
from app.modules.resolutions.application.dto import Actor
from app.modules.resolutions.domain.entities import Dispute, Resolution
from app.modules.resolutions.domain.errors import (
    DisputeNotFoundError,
    DisputeNotAllowedError,
    DisputeWindowClosedError,
    EventNotResolvableError,
    InvalidResolutionDataError,
    ResolutionNotFoundError,
    ResolutionTargetEventNotFoundError,
)
from app.modules.resolutions.domain.policies import (
    ensure_can_decide_dispute,
    ensure_can_raise_dispute,
    ensure_can_resolve,
    ensure_not_self_decision,
)
from app.modules.resolutions.ports.clock import Clock
from app.modules.resolutions.ports.gateways import (
    EventResolutionGateway,
    ParticipationGateway,
)
from app.modules.resolutions.ports.repositories import (
    DisputeRepository,
    ResolutionRepository,
    ScoringDispatchRepository,
)
from app.modules.resolutions.ports.tasks import TaskScheduler
from app.shared.audit.domain.entities import AuditActorType
from app.shared.audit.ports.audit_trail import AuditTrail

# Маппинг роли пользователя на тип актора аудита.
_ACTOR_TYPE_BY_ROLE: dict[UserRole, AuditActorType] = {
    UserRole.USER: AuditActorType.USER,
    UserRole.EDITOR: AuditActorType.EDITOR,
    UserRole.ARBITER: AuditActorType.ARBITER,
    UserRole.ADMIN: AuditActorType.ADMIN,
}


def _actor_type(role: UserRole) -> AuditActorType:
    """Тип актора аудита по роли (по умолчанию — рядовой пользователь)."""
    return _ACTOR_TYPE_BY_ROLE.get(role, AuditActorType.USER)


class FixResolution:
    """Фиксация исхода события: ``closed → resolved`` + открытие окна оспаривания.

    Одношагово (MVP): пишется одна ``final``-резолюция. Событие проводится через
    ``resolving`` к ``resolved`` внутри шлюза в одной транзакции.
    """

    def __init__(
        self,
        *,
        resolutions: ResolutionRepository,
        events: EventResolutionGateway,
        audit: AuditTrail,
        clock: Clock,
        dispute_window: timedelta,
    ) -> None:
        self._resolutions = resolutions
        self._events = events
        self._audit = audit
        self._clock = clock
        self._window = dispute_window

    async def execute(
        self,
        *,
        event_id: uuid.UUID,
        actor: Actor,
        outcome: bool,
        source_reference: str,
        notes: str = "",
    ) -> Resolution:
        """Фиксирует исход; возвращает сохранённое финальное решение."""
        ensure_can_resolve(actor.role)

        lifecycle = await self._events.get_lifecycle(event_id)
        if lifecycle is None:
            raise ResolutionTargetEventNotFoundError("Событие не найдено")
        if lifecycle.status is not EventStatus.CLOSED:
            raise EventNotResolvableError(
                "Фиксировать исход можно только у закрытого события (closed)"
            )

        now = self._clock.now()
        window_end = now + self._window
        resolution = Resolution.finalize(
            event_id=event_id,
            outcome=outcome,
            resolved_by=actor.user_id,
            source_reference=source_reference,
            notes=notes,
            now=now,
        )
        saved = await self._resolutions.add(resolution)
        await self._events.fix_outcome(
            event_id, outcome=outcome, dispute_window_ends_at=window_end, now=now
        )
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="resolution.finalized",
            entity_type="resolution",
            entity_id=saved.id,
            after={
                "outcome": outcome,
                "status": saved.status.value,
                "source_reference": saved.source_reference,
            },
            metadata={
                "event_id": str(event_id),
                "dispute_window_ends_at": window_end.isoformat(),
            },
        )
        return saved


class GetResolution:
    """Текущее (финальное) решение события — публичное чтение."""

    def __init__(self, *, resolutions: ResolutionRepository) -> None:
        self._resolutions = resolutions

    async def execute(self, *, event_id: uuid.UUID) -> Resolution:
        """Возвращает текущее решение либо поднимает ``ResolutionNotFoundError``."""
        current = await self._resolutions.current_final(event_id)
        if current is None:
            raise ResolutionNotFoundError("У события нет зафиксированного исхода")
        return current


class ListDisputes:
    """Список споров события — публичное чтение (прозрачность арбитража)."""

    def __init__(self, *, disputes: DisputeRepository) -> None:
        self._disputes = disputes

    async def execute(self, *, event_id: uuid.UUID) -> list[Dispute]:
        """Все споры события (новые выше)."""
        return await self._disputes.list_for_event(event_id)


class RaiseDispute:
    """Подача оспаривания участником: ``resolved → disputed``.

    Допускается только в пределах окна и только участнику события (есть прогноз).
    Так как поднять спор можно лишь у ``resolved``-события, в каждый момент по
    событию открыт максимум один спор.
    """

    def __init__(
        self,
        *,
        disputes: DisputeRepository,
        resolutions: ResolutionRepository,
        events: EventResolutionGateway,
        participation: ParticipationGateway,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._disputes = disputes
        self._resolutions = resolutions
        self._events = events
        self._participation = participation
        self._audit = audit
        self._clock = clock

    async def execute(
        self,
        *,
        event_id: uuid.UUID,
        actor: Actor,
        reason: str,
        evidence: str = "",
    ) -> Dispute:
        """Регистрирует спор и переводит событие в ``disputed``."""
        ensure_can_raise_dispute(actor.role)

        lifecycle = await self._events.get_lifecycle(event_id)
        if lifecycle is None:
            raise ResolutionTargetEventNotFoundError("Событие не найдено")
        if lifecycle.status is not EventStatus.RESOLVED:
            raise DisputeWindowClosedError(
                "Оспаривать можно только разрешённое событие в пределах окна"
            )

        now = self._clock.now()
        if (
            lifecycle.dispute_window_ends_at is None
            or now >= lifecycle.dispute_window_ends_at
        ):
            raise DisputeWindowClosedError("Окно оспаривания истекло")

        if not await self._participation.has_prediction(
            user_id=actor.user_id, event_id=event_id
        ):
            raise DisputeNotAllowedError(
                "Оспаривать вправе только участник события (поставивший прогноз)"
            )

        current = await self._resolutions.current_final(event_id)
        if current is None:  # инвариант: у resolved-события есть final-решение
            raise ResolutionNotFoundError("У события нет зафиксированного исхода")

        dispute = Dispute.open_for(
            event_id=event_id,
            resolution_id=current.id,
            raised_by=actor.user_id,
            reason=reason,
            evidence=evidence,
            now=now,
        )
        saved = await self._disputes.add(dispute)
        await self._events.open_dispute(event_id, now=now)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="dispute.raised",
            entity_type="dispute",
            entity_id=saved.id,
            after={"status": saved.status.value, "reason": saved.reason},
            metadata={
                "event_id": str(event_id),
                "resolution_id": str(current.id),
            },
        )
        return saved


class DecideDispute:
    """Решение арбитра по спору: отклонение или удовлетворение (overturn).

    ``reject`` возвращает событие в ``resolved`` с прежним исходом. ``accept``
    пересматривает исход: пишет новую ``final``-резолюцию (``supersedes`` на
    текущую), обновляет денормализованный ``outcome`` и открывает окно заново —
    overturn можно оспорить так же, как исходное решение.
    """

    def __init__(
        self,
        *,
        disputes: DisputeRepository,
        resolutions: ResolutionRepository,
        events: EventResolutionGateway,
        audit: AuditTrail,
        clock: Clock,
        dispute_window: timedelta,
    ) -> None:
        self._disputes = disputes
        self._resolutions = resolutions
        self._events = events
        self._audit = audit
        self._clock = clock
        self._window = dispute_window

    async def execute(
        self,
        *,
        dispute_id: uuid.UUID,
        actor: Actor,
        accept: bool,
        decision_notes: str = "",
        new_outcome: bool | None = None,
    ) -> Dispute:
        """Закрывает спор и применяет последствия к событию/журналу решений."""
        ensure_can_decide_dispute(actor.role)

        dispute = await self._disputes.get_by_id(dispute_id)
        if dispute is None:
            raise DisputeNotFoundError("Спор не найден")
        ensure_not_self_decision(
            decided_by=actor.user_id, raised_by=dispute.raised_by
        )

        now = self._clock.now()
        if accept:
            await self._overturn(dispute, actor=actor, new_outcome=new_outcome, now=now)
        else:
            await self._events.dismiss_dispute(dispute.event_id, now=now)

        dispute.decide(
            accepted=accept,
            decided_by=actor.user_id,
            decision_notes=decision_notes,
            now=now,
        )
        saved = await self._disputes.update(dispute)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="dispute.accepted" if accept else "dispute.rejected",
            entity_type="dispute",
            entity_id=saved.id,
            after={
                "status": saved.status.value,
                "decision_notes": saved.decision_notes,
            },
            metadata={"event_id": str(saved.event_id)},
        )
        return saved

    async def _overturn(
        self,
        dispute: Dispute,
        *,
        actor: Actor,
        new_outcome: bool | None,
        now: datetime,
    ) -> None:
        """Пересматривает исход события новой ``final``-резолюцией."""
        if new_outcome is None:
            raise InvalidResolutionDataError(
                "Для удовлетворения спора нужен новый исход (new_outcome)"
            )
        current = await self._resolutions.current_final(dispute.event_id)
        if current is None:
            raise ResolutionNotFoundError("У события нет зафиксированного исхода")
        if new_outcome == current.outcome:
            raise InvalidResolutionDataError(
                "Новый исход overturn'а совпадает с текущим — пересмотр не нужен"
            )

        window_end = now + self._window
        revision = Resolution.finalize(
            event_id=dispute.event_id,
            outcome=new_outcome,
            resolved_by=actor.user_id,
            source_reference=f"dispute:{dispute.id}",
            notes=dispute.decision_notes,
            supersedes_id=current.id,
            now=now,
        )
        saved = await self._resolutions.add(revision)
        await self._events.overturn_outcome(
            dispute.event_id,
            outcome=new_outcome,
            dispute_window_ends_at=window_end,
            now=now,
        )
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="resolution.overturned",
            entity_type="resolution",
            entity_id=saved.id,
            before={"outcome": current.outcome},
            after={"outcome": new_outcome, "supersedes_id": str(current.id)},
            metadata={
                "event_id": str(dispute.event_id),
                "dispute_id": str(dispute.id),
            },
        )


class CloseDisputeWindows:
    """Фоновое закрытие окон оспаривания и постановка скоринга.

    Находит ``resolved``-события с истёкшим окном без открытых споров и,
    если скоринг по текущей резолюции ещё не ставился, фиксирует диспатч и
    ставит ``score_event``. Маркер диспатча ограничивает скан и даёт
    идемпотентность (повторный тик не дублирует постановку).
    """

    def __init__(
        self,
        *,
        events: EventResolutionGateway,
        resolutions: ResolutionRepository,
        disputes: DisputeRepository,
        dispatches: ScoringDispatchRepository,
        tasks: TaskScheduler,
        audit: AuditTrail,
        clock: Clock,
    ) -> None:
        self._events = events
        self._resolutions = resolutions
        self._disputes = disputes
        self._dispatches = dispatches
        self._tasks = tasks
        self._audit = audit
        self._clock = clock

    async def execute(self) -> int:
        """Возвращает число событий, поставленных в скоринг на этом проходе."""
        now = self._clock.now()
        event_ids = await self._events.find_resolved_past_window(now=now)
        dispatched = 0
        for event_id in event_ids:
            if await self._disputes.has_open_for_event(event_id):
                continue  # защита: у resolved-события открытых споров быть не должно
            current = await self._resolutions.current_final(event_id)
            if current is None:
                continue
            if await self._dispatches.exists(current.id):
                continue
            inserted = await self._dispatches.add(
                resolution_id=current.id, event_id=event_id, now=now
            )
            if not inserted:  # гонка: другой воркер уже поставил
                continue
            await self._tasks.enqueue_score_event(event_id)
            await self._audit.record(
                actor_id=None,
                actor_type=AuditActorType.SYSTEM,
                action="event.scoring_enqueued",
                entity_type="event",
                entity_id=event_id,
                metadata={"resolution_id": str(current.id)},
            )
            dispatched += 1
        return dispatched
