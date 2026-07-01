"""Адаптеры-шлюзы scoring к таблицам events/predictions (монолит, единая БД).

Реализуют порты :class:`EventScoringGateway` и :class:`PredictionScoreWriter`
прямым чтением/записью соседних таблиц. Это интеграционный шов: при выносе
events/predictions в отдельные сервисы заменяется на сетевой контракт, а
порты и use-cases не меняются.

TODO(scoring-infra): заменить N+1 (событие → его прогнозы) на единый JOIN с
агрегацией; для пер-событийной записи Brier — bulk ``UPDATE … FROM (VALUES …)``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select, update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.orm import EventORM
from app.modules.events.domain.entities import EventStatus
from app.modules.predictions.adapters.orm import PredictionORM
from app.modules.scoring.application.dto import EventScoringStatus, PredictionScore
from app.modules.scoring.domain.formulas import time_weight_from_earliness
from app.modules.scoring.domain.value_objects import PredictionVote, ResolvedEvent
from app.modules.scoring.ports.clock import Clock


def _to_outcome(value: bool | None) -> int | None:
    """``bool`` исхода события → ``int ∈ {0, 1}`` домена скоринга."""
    if value is None:
        return None
    return 1 if value else 0


def _earliness(
    made_at: datetime, opens_at: datetime, closes_at: datetime
) -> float:
    """Доля «ранности» прогноза в окне приёма ``∈ [0, 1]``.

    ``1`` — прогноз в момент открытия, ``0`` — в момент закрытия. Вырожденное
    окно (``closes_at ≤ opens_at``) → ``1.0`` (все прогнозы одинаково «ранние»).
    Границы клампятся в самом ``time_weight_from_earliness``.
    """
    span = (closes_at - opens_at).total_seconds()
    if span <= 0:
        return 1.0
    return (closes_at - made_at).total_seconds() / span


class SqlAlchemyEventScoringGateway:
    """``EventScoringGateway`` поверх таблиц events/predictions."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def get_status(self, event_id: uuid.UUID) -> EventScoringStatus:
        """Готовность события к скорингу (статус + окно оспаривания)."""
        event = await self._session.get(EventORM, event_id)
        if event is None:
            return EventScoringStatus(
                found=False, is_resolved=False, is_final=False, outcome=None
            )
        is_resolved = (
            event.status is EventStatus.RESOLVED and event.outcome is not None
        )
        is_final = is_resolved and self._dispute_window_passed(
            event.dispute_window_ends_at
        )
        return EventScoringStatus(
            found=True,
            is_resolved=is_resolved,
            is_final=is_final,
            outcome=_to_outcome(event.outcome),
        )

    async def get_resolved_event(self, event_id: uuid.UUID) -> ResolvedEvent | None:
        """Полное разрешённое событие с заблокированными прогнозами или ``None``."""
        event = await self._session.get(EventORM, event_id)
        if event is None or not self._is_scoreable(event):
            return None
        return await self._build_resolved(event)

    async def list_resolved_events(
        self, *, season_id: uuid.UUID | None = None
    ) -> list[ResolvedEvent]:
        """Все финально разрешённые события (опц. сезон) с их прогнозами."""
        stmt = select(EventORM).where(
            EventORM.status == EventStatus.RESOLVED,
            EventORM.outcome.is_not(None),
        )
        if season_id is not None:
            stmt = stmt.where(EventORM.season_id == season_id)
        events = (await self._session.execute(stmt)).scalars().all()

        resolved: list[ResolvedEvent] = []
        for event in events:
            if self._is_scoreable(event):
                resolved.append(await self._build_resolved(event))
        return resolved

    async def list_user_calibration_entries(
        self, user_id: uuid.UUID
    ) -> list[tuple[float, int]]:
        """Пары ``(номинал, исход)`` по засчитанным (scored) прогнозам пользователя."""
        stmt = (
            select(PredictionORM.probability, EventORM.outcome)
            .join(EventORM, EventORM.id == PredictionORM.event_id)
            .where(
                PredictionORM.user_id == user_id,
                PredictionORM.scored_at.is_not(None),
                EventORM.outcome.is_not(None),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            (float(probability), 1 if outcome else 0)
            for probability, outcome in rows
        ]

    async def list_season_calibration_entries(
        self, season_id: uuid.UUID
    ) -> list[tuple[float, int]]:
        """Пары ``(номинал, исход)`` по всем засчитанным прогнозам сезона."""
        stmt = (
            select(PredictionORM.probability, EventORM.outcome)
            .join(EventORM, EventORM.id == PredictionORM.event_id)
            .where(
                EventORM.season_id == season_id,
                PredictionORM.scored_at.is_not(None),
                EventORM.outcome.is_not(None),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            (float(probability), 1 if outcome else 0)
            for probability, outcome in rows
        ]

    # ── Внутреннее ──────────────────────────────────────────────────────────

    def _is_scoreable(self, event: EventORM) -> bool:
        return (
            event.status is EventStatus.RESOLVED
            and event.outcome is not None
            and self._dispute_window_passed(event.dispute_window_ends_at)
        )

    def _dispute_window_passed(self, ends_at: datetime | None) -> bool:
        """Закрыто ли окно оспаривания (нет окна — считается закрытым)."""
        return ends_at is None or ends_at <= self._clock.now()

    async def _build_resolved(self, event: EventORM) -> ResolvedEvent:
        stmt = select(PredictionORM).where(
            PredictionORM.event_id == event.id,
            PredictionORM.is_locked.is_(True),
        )
        predictions = (await self._session.execute(stmt)).scalars().all()
        votes = tuple(
            PredictionVote(
                user_id=p.user_id,
                probability=float(p.probability),
                time_weight=time_weight_from_earliness(
                    _earliness(p.created_at, event.opens_at, event.closes_at)
                ),
            )
            for p in predictions
        )
        return ResolvedEvent(
            event_id=event.id,
            category_id=event.category_id,
            season_id=event.season_id,
            outcome=cast("int", _to_outcome(event.outcome)),
            votes=votes,
        )


class SqlAlchemyPredictionScoreWriter:
    """``PredictionScoreWriter`` — проставляет Brier обратно в ``predictions``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_event_scores(
        self,
        event_id: uuid.UUID,
        scores: Sequence[PredictionScore],
        *,
        now: datetime,
    ) -> int:
        """Обновляет ``brier_score``/``scored_at`` по каждому прогнозу события."""
        updated = 0
        for score in scores:
            stmt = (
                sa_update(PredictionORM)
                .where(
                    PredictionORM.event_id == event_id,
                    PredictionORM.user_id == score.user_id,
                )
                .values(brier_score=score.brier, scored_at=now)
            )
            result = cast("CursorResult[Any]", await self._session.execute(stmt))
            updated += result.rowcount or 0
        return updated
