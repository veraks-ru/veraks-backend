"""Use-cases домена predictions.

Каждый класс — одна бизнес-операция; зависимости передаются только через
порты (конструктор), поэтому use-cases изолированы от FastAPI/SQLAlchemy и
покрываются юнит-тестами с фейками.

Операции:
  * :class:`PlacePrediction` — поставить/изменить свой прогноз (PUT, upsert)
    до дедлайна, с записью истории в аудит;
  * :class:`GetMyPrediction` — прочитать свой прогноз по событию;
  * :class:`GetEventTopPredictions` — доска лучших прогнозов разрешённого
    события (публичная витрина точности);
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
    TopPredictionEntry,
)
from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction
from app.modules.predictions.domain.errors import (
    EventTopPredictionsUnavailableError,
    PredictionNotFoundError,
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
        """Ставит или обновляет прогноз; возвращает актуальное состояние.

        Участие в конкурсе бесплатно (гл. 57 ГК РФ, PRD §7.1/§7.4): любой
        верифицированный пользователь может голосовать без подписки. Подписка
        даёт только расширенную аналитику, но НЕ право участия.
        """
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

    Виден всем и всегда, включая открытый приём и незалогиненных читателей.
    Это продуктовое решение: публичный индикатор «во что верят люди» ценен сам
    по себе, ради него на площадку заходят и те, кто не прогнозирует, — и
    именно он делает её живой.

    Прежде сводка пряталась до закрытия приёма (анти-якорение): средний
    прогноз ``c_e`` — бенчмарк leave-one-out скоринга, и раньше времени он
    позволял бы к нему подстраиваться. Плата за раскрытие осознанная: голоса
    перестают быть независимыми, а поздний участник видит больше раннего.
    Компенсировать это должен скоринг — сравнивать человека с консенсусом на
    момент ЕГО прогноза, а не с итоговым (см. scoring_system_design.md §5).
    """

    def __init__(
        self, *, predictions: PredictionRepository, events: EventGateway
    ) -> None:
        self._predictions = predictions
        self._events = events

    async def execute(self, *, event_id: uuid.UUID) -> PredictionSummary:
        """Считает распределение по градациям и средний прогноз (``c_e``)."""
        snapshot = await self._events.get_snapshot(event_id)
        if snapshot is None:
            raise PredictionTargetEventNotFoundError("Событие не найдено")

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
    """Публичный трек-рекорд: разрешённые прогнозы пользователя по хэндлу.

    Прогнозы по аннулированным событиям сюда не попадают (фильтр в
    репозитории): аннулированное событие вычеркнуто из рейтингов и калибровки,
    и публичные агрегаты профиля, которые строятся из этой выдачи, обязаны
    считать так же.
    """

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


class GetEventTopPredictions:
    """Доска лучших прогнозов разрешённого события (публичная витрина точности).

    Социальное доказательство «не казино» (PRD §7): показываем точность
    (Brier), а не выигрыш. Владение данными: ``brier_score`` проставляет
    домен scoring, но живёт он на прогнозе (``predictions``) — здесь и есть
    естественное место чтения, без обратной зависимости predictions → scoring.

    Доступна только для события в статусе ``resolved`` (не для
    открытого/аннулированного/оспариваемого — см. :class:`EventGateway.is_resolved`).
    Скрытых пользователей (удалённые/заблокированные аккаунты) в выдаче нет:
    их прогнозы по-прежнему считаются в среднем Brier толпы, но сама строка
    доски не показывается — профиль публично недоступен.
    """

    def __init__(
        self,
        *,
        predictions: PredictionRepository,
        events: EventGateway,
        users: UserDirectory,
    ) -> None:
        self._predictions = predictions
        self._events = events
        self._users = users

    async def execute(
        self, *, event_id: uuid.UUID, limit: int = 10
    ) -> list[TopPredictionEntry]:
        """Топ-``limit`` прогнозов по возрастанию Brier (точнее — выше)."""
        resolved = await self._events.is_resolved(event_id)
        if resolved is None:
            raise PredictionTargetEventNotFoundError("Событие не найдено")
        if not resolved:
            raise EventTopPredictionsUnavailableError(
                "Доска лучших доступна только для разрешённого события"
            )

        votes = await self._predictions.list_for_event(event_id)
        # Пары (прогноз, Brier) — а не фильтрация по атрибуту отдельным шагом,
        # чтобы mypy сузил ``Decimal | None`` до ``Decimal`` уже в компрехеншене.
        scored = [(v, v.brier_score) for v in votes if v.brier_score is not None]
        if not scored:
            return []

        # Простое среднее по всем засчитанным прогнозам — та же величина,
        # что уже показана на экране события как «средний Brier толпы»
        # (не leave-one-out: это витринная метрика, не скоринговый бенчмарк).
        crowd_mean = sum((brier for _, brier in scored), Decimal(0)) / len(scored)

        refs = await self._users.list_active_by_ids([v.user_id for v, _ in scored])
        visible = [(v, brier) for v, brier in scored if v.user_id in refs]
        visible.sort(key=lambda item: (item[1], str(item[0].user_id)))

        return [
            TopPredictionEntry(
                user_id=v.user_id,
                username=refs[v.user_id].username,
                display_name=refs[v.user_id].display_name,
                confidence_grade=v.confidence_grade,
                brier_score=brier,
                beat_crowd=brier < crowd_mean,
            )
            for v, brier in visible[:limit]
        ]


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
