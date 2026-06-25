"""SQLAlchemy-репозитории resolutions поверх async-сессии.

``ResolutionRepository`` — только INSERT (журнал неизменяем). «Текущее»
решение = последняя ``final``-строка по ``resolved_at`` (overturn пишет более
свежую ``final`` со ссылкой ``supersedes_id``). ``ScoringDispatchRepository.add``
идемпотентен через ``ON CONFLICT DO NOTHING``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.events.adapters.orm import EventORM
from app.modules.resolutions.adapters.orm import (
    DisputeORM,
    ResolutionORM,
    ScoringDispatchORM,
)
from app.modules.resolutions.domain.entities import (
    Dispute,
    DisputeStatus,
    Resolution,
    ResolutionStatus,
)

_OPEN_DISPUTE_STATUSES = (DisputeStatus.OPEN, DisputeStatus.UNDER_REVIEW)


class SqlAlchemyResolutionRepository:
    """Append-only журнал решений поверх таблицы ``resolutions``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, resolution: Resolution) -> Resolution:
        """Вставляет новое решение."""
        orm = ResolutionORM.from_domain(resolution)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def current_final(self, event_id: uuid.UUID) -> Resolution | None:
        """Текущее финальное решение события (голова цепочки пересмотров).

        Текущее = финальное решение, которое никем не отменено (на него не
        ссылается ``supersedes_id`` другой строки). Это устойчиво к равным
        ``resolved_at`` (overturn в ту же секунду), в отличие от «последнее по
        времени». ``order_by`` — лишь страховка на случай рассинхрона.
        """
        superseding = aliased(ResolutionORM)
        stmt = (
            select(ResolutionORM)
            .where(
                ResolutionORM.event_id == event_id,
                ResolutionORM.status == ResolutionStatus.FINAL,
                ~select(superseding.id)
                .where(superseding.supersedes_id == ResolutionORM.id)
                .exists(),
            )
            .order_by(ResolutionORM.resolved_at.desc())
            .limit(1)
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def list_for_event(self, event_id: uuid.UUID) -> list[Resolution]:
        """Полная история решений события (новые выше)."""
        stmt = (
            select(ResolutionORM)
            .where(ResolutionORM.event_id == event_id)
            .order_by(ResolutionORM.resolved_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]


class SqlAlchemyDisputeRepository:
    """Хранилище споров поверх таблицы ``disputes``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, dispute: Dispute) -> Dispute:
        """Вставляет новый спор."""
        orm = DisputeORM.from_domain(dispute)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def get_by_id(self, dispute_id: uuid.UUID) -> Dispute | None:
        """Спор по PK."""
        orm = await self._session.get(DisputeORM, dispute_id)
        return orm.to_domain() if orm else None

    async def update(self, dispute: Dispute) -> Dispute:
        """Синхронизирует изменяемые поля решения по спору."""
        orm = await self._session.get(DisputeORM, dispute.id)
        if orm is None:  # pragma: no cover — вызывается только для существующих
            raise DisputeRowVanishedError(str(dispute.id))
        orm.status = dispute.status
        orm.decided_by = dispute.decided_by
        orm.decision_notes = dispute.decision_notes
        orm.decided_at = dispute.decided_at
        await self._session.flush()
        return orm.to_domain()

    async def list_for_event(self, event_id: uuid.UUID) -> list[Dispute]:
        """Все споры события (новые выше)."""
        stmt = (
            select(DisputeORM)
            .where(DisputeORM.event_id == event_id)
            .order_by(DisputeORM.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]

    async def has_open_for_event(self, event_id: uuid.UUID) -> bool:
        """Есть ли по событию незакрытый спор."""
        stmt = select(
            exists().where(
                DisputeORM.event_id == event_id,
                DisputeORM.status.in_(_OPEN_DISPUTE_STATUSES),
            )
        )
        return bool((await self._session.execute(stmt)).scalar())

    async def has_open_in_season(self, season_id: uuid.UUID) -> bool:
        """Есть ли незакрытые споры по событиям сезона (join на ``events``)."""
        stmt = select(
            exists()
            .select_from(DisputeORM)
            .join(EventORM, EventORM.id == DisputeORM.event_id)
            .where(
                EventORM.season_id == season_id,
                DisputeORM.status.in_(_OPEN_DISPUTE_STATUSES),
            )
        )
        return bool((await self._session.execute(stmt)).scalar())


class SqlAlchemyScoringDispatchRepository:
    """Маркеры поставленного скоринга поверх ``resolution_scoring_dispatches``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, resolution_id: uuid.UUID) -> bool:
        """Был ли уже поставлен скоринг по резолюции."""
        stmt = select(
            exists().where(ScoringDispatchORM.resolution_id == resolution_id)
        )
        return bool((await self._session.execute(stmt)).scalar())

    async def add(
        self, *, resolution_id: uuid.UUID, event_id: uuid.UUID, now: datetime
    ) -> bool:
        """Идемпотентно фиксирует диспатч; ``False``, если уже существовал."""
        stmt = (
            pg_insert(ScoringDispatchORM)
            .values(
                resolution_id=resolution_id,
                event_id=event_id,
                dispatched_at=now,
            )
            .on_conflict_do_nothing(index_elements=["resolution_id"])
            .returning(ScoringDispatchORM.resolution_id)
        )
        inserted = (await self._session.execute(stmt)).scalar_one_or_none()
        return inserted is not None


class DisputeRowVanishedError(RuntimeError):
    """Строка спора исчезла между чтением и записью (не должно случаться)."""
