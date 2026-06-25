"""Use-cases домена predictions.

Каждый класс — одна бизнес-операция; зависимости передаются только через
порты (конструктор), поэтому use-cases изолированы от FastAPI/SQLAlchemy и
покрываются юнит-тестами с фейками.

Операции:
  * :class:`PlacePrediction` — поставить/изменить свой прогноз (PUT, upsert)
    до дедлайна, с записью истории в аудит;
  * :class:`GetMyPrediction` — прочитать свой прогноз по событию;
  * :class:`LockEventPredictions` — массово заблокировать прогнозы при
    закрытии события (вызывается доменом events).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from app.modules.predictions.application.dto import (
    PredictionAuditEntry,
    PredictionSummary,
)
from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction
from app.modules.predictions.domain.errors import (
    PredictionNotFoundError,
    PredictionSummaryHiddenError,
    PredictionTargetEventNotFoundError,
    ProfileUserNotFoundError,
)
from app.modules.predictions.domain.policies import ensure_event_accepts_predictions
from app.modules.predictions.ports.audit import AuditRecorder
from app.modules.predictions.ports.clock import Clock
from app.modules.predictions.ports.events import EventGateway
from app.modules.predictions.ports.repositories import (
    PredictionAlreadyExistsError,
    PredictionRepository,
)
from app.modules.predictions.ports.users import UserDirectory

_ACTION_CREATED = "prediction.created"
_ACTION_UPDATED = "prediction.updated"


class PlacePrediction:
    """Постановка/изменение прогноза пользователя по событию (PUT, upsert).

    Реализует «приём градации → вероятность» и запрет правок после дедлайна:
      1. читает снимок события через шлюз; нет события → 404-ошибка домена;
      2. проверяет, что приём открыт (статус + серверный ``closes_at``);
      3. upsert: меняет существующий прогноз либо создаёт новый;
      4. фиксирует изменение в аудит (история правок).

    Гонку параллельных постановок (UNIQUE) ловит и сводит к обновлению.
    """

    def __init__(
        self,
        *,
        predictions: PredictionRepository,
        events: EventGateway,
        clock: Clock,
        audit: AuditRecorder,
    ) -> None:
        self._predictions = predictions
        self._events = events
        self._clock = clock
        self._audit = audit

    async def execute(
        self, *, user_id: uuid.UUID, event_id: uuid.UUID, grade: ConfidenceGrade
    ) -> Prediction:
        """Ставит или обновляет прогноз; возвращает актуальное состояние."""
        now = self._clock.now()
        snapshot = await self._events.get_snapshot(event_id)
        if snapshot is None:
            raise PredictionTargetEventNotFoundError("Событие не найдено")
        ensure_event_accepts_predictions(snapshot, now=now)

        existing = await self._predictions.get_for_user_event(user_id, event_id)
        if existing is not None:
            return await self._apply_change(existing, grade, now=now)

        prediction = Prediction.place(
            user_id=user_id, event_id=event_id, grade=grade, now=now
        )
        try:
            saved = await self._predictions.add(prediction)
        except PredictionAlreadyExistsError:
            # Параллельная постановка того же пользователя победила — обновляем её.
            racing = await self._predictions.get_for_user_event(user_id, event_id)
            if racing is None:  # pragma: no cover — UNIQUE гарантирует наличие
                raise
            return await self._apply_change(racing, grade, now=now)

        await self._record(saved, action=_ACTION_CREATED, before=None)
        return saved

    async def _apply_change(
        self, prediction: Prediction, grade: ConfidenceGrade, *, now: datetime
    ) -> Prediction:
        """Применяет смену градации к существующему прогнозу (с аудитом).

        Идемпотентность: повтор той же градации не пишет ни UPDATE, ни историю.
        """
        previous = prediction.confidence_grade
        if not prediction.change_grade(grade, now=now):
            return prediction
        saved = await self._predictions.update(prediction)
        await self._record(saved, action=_ACTION_UPDATED, before=previous)
        return saved

    async def _record(
        self, prediction: Prediction, *, action: str, before: ConfidenceGrade | None
    ) -> None:
        """Пишет запись истории изменения прогноза в аудит."""
        await self._audit.record(
            PredictionAuditEntry(
                action=action,
                actor_id=prediction.user_id,
                event_id=prediction.event_id,
                prediction_id=prediction.id,
                before=before.value if before is not None else None,
                after=prediction.confidence_grade.value,
                occurred_at=prediction.updated_at,
            )
        )


class GetMyPrediction:
    """Чтение собственного прогноза пользователя по событию."""

    def __init__(self, *, predictions: PredictionRepository) -> None:
        self._predictions = predictions

    async def execute(
        self, *, user_id: uuid.UUID, event_id: uuid.UUID
    ) -> Prediction:
        """Возвращает прогноз или поднимает :class:`PredictionNotFoundError`."""
        prediction = await self._predictions.get_for_user_event(user_id, event_id)
        if prediction is None:
            raise PredictionNotFoundError("Прогноз по событию не найден")
        return prediction


class GetEventPredictionSummary:
    """Агрегированный «сигнал толпы» по событию (распределение + консенсус).

    Виден **только после закрытия приёма** (анти-якорение, дизайн скоринга §5):
    пока окно открыто, раскрытие консенсуса позволяло бы списывать/якорить
    прогноз. До закрытия — :class:`PredictionSummaryHiddenError`.
    """

    def __init__(
        self,
        *,
        predictions: PredictionRepository,
        events: EventGateway,
        clock: Clock,
    ) -> None:
        self._predictions = predictions
        self._events = events
        self._clock = clock

    async def execute(self, *, event_id: uuid.UUID) -> PredictionSummary:
        """Считает распределение по градациям и средний прогноз (``c_e``)."""
        snapshot = await self._events.get_snapshot(event_id)
        if snapshot is None:
            raise PredictionTargetEventNotFoundError("Событие не найдено")
        if snapshot.is_accepting_at(self._clock.now()):
            raise PredictionSummaryHiddenError(
                "Сигнал толпы скрыт до закрытия приёма прогнозов"
            )

        votes = await self._predictions.list_for_event(event_id)
        distribution = {grade: 0 for grade in ConfidenceGrade}
        for vote in votes:
            distribution[vote.confidence_grade] += 1

        total = len(votes)
        mean = (
            sum((v.probability for v in votes), Decimal(0)) / total
            if total
            else None
        )
        return PredictionSummary(
            event_id=event_id,
            total_count=total,
            distribution=distribution,
            mean_probability=mean,
        )


class ListMyPredictions:
    """Свои прогнозы (все, включая ожидающие разрешения)."""

    def __init__(self, *, predictions: PredictionRepository) -> None:
        self._predictions = predictions

    async def execute(self, *, user_id: uuid.UUID) -> list[Prediction]:
        """Прогнозы текущего пользователя, новые сверху."""
        return await self._predictions.list_for_user(user_id)


class ListUserPredictions:
    """Публичный трек-рекорд: разрешённые прогнозы пользователя по хэндлу."""

    def __init__(
        self, *, users: UserDirectory, predictions: PredictionRepository
    ) -> None:
        self._users = users
        self._predictions = predictions

    async def execute(self, *, username: str) -> list[Prediction]:
        """Разрешённые (засчитанные) прогнозы пользователя; 404, если нет."""
        user_id = await self._users.resolve_username(username)
        if user_id is None:
            raise ProfileUserNotFoundError("Профиль не найден")
        return await self._predictions.list_for_user(user_id, resolved_only=True)


class LockEventPredictions:
    """Массовая блокировка прогнозов при закрытии приёма по событию.

    Проставляет ``is_locked = true`` всем прогнозам события — после этого
    правки невозможны (см. ``Prediction.change_grade``). Это подготовка к
    скорингу: заблокированные прогнозы домен scoring оценивает по Brier.

    TODO(events-integration): вызывается при переходе события ``open → closed``
    (events ``CloseEvent``) или системным воркером по наступлению ``closes_at``.
    """

    def __init__(self, *, predictions: PredictionRepository, clock: Clock) -> None:
        self._predictions = predictions
        self._clock = clock

    async def execute(self, *, event_id: uuid.UUID) -> int:
        """Блокирует прогнозы события; возвращает число затронутых."""
        return await self._predictions.lock_for_event(event_id, now=self._clock.now())
