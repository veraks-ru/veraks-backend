"""Доменные сущности predictions: ``ConfidenceGrade`` и ``Prediction``.

Сущности — обычные dataclass'ы без знания о SQLAlchemy/pydantic. ORM-модель
(``adapters/orm.py``) и API-схемы (``api/schemas.py``) мапятся на них явными
``to_domain``/``from_domain``.

Ключевая бизнес-логика домена: пользователь выбирает градацию уверенности,
а она детерминированно отображается во внутреннюю вероятность. Вероятность
**хранится** вместе с прогнозом (а не вычисляется на чтении), чтобы её
смысл не «поплыл» при будущем изменении шкалы — это инвариант неизменяемости
уже принятых прогнозов.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from app.modules.predictions.domain.errors import PredictionLockedError


class ConfidenceGrade(str, enum.Enum):
    """Градация уверенности — то, что нажимает пользователь (бинарный исход).

    Шкала из пяти ступеней; человекочитаемые ярлыки на фронте, во внутреннюю
    вероятность отображаются через :data:`_GRADE_PROBABILITY`.
    """

    DEFINITELY_NO = "definitely_no"
    PROBABLY_NO = "probably_no"
    FIFTY_FIFTY = "fifty_fifty"
    PROBABLY_YES = "probably_yes"
    DEFINITELY_YES = "definitely_yes"


# Детерминированное отображение «градация → вероятность исхода ``Да``».
# Decimal (не float) — деньги/вероятности в системе считаются точно; колонка
# БД — ``numeric(3,2)``.
_GRADE_PROBABILITY: dict[ConfidenceGrade, Decimal] = {
    ConfidenceGrade.DEFINITELY_NO: Decimal("0.10"),
    ConfidenceGrade.PROBABLY_NO: Decimal("0.30"),
    ConfidenceGrade.FIFTY_FIFTY: Decimal("0.50"),
    ConfidenceGrade.PROBABLY_YES: Decimal("0.70"),
    ConfidenceGrade.DEFINITELY_YES: Decimal("0.90"),
}


def probability_for_grade(grade: ConfidenceGrade) -> Decimal:
    """Возвращает внутреннюю вероятность для градации уверенности.

    Чистая тотальная функция: определена для всех членов enum, поэтому
    ``KeyError`` здесь невозможен (защищено типом).
    """
    return _GRADE_PROBABILITY[grade]


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Prediction:
    """Прогноз пользователя по событию — один на пару ``(user, event)``.

    Хранится latest-wins (правки перезаписывают строку), история изменений
    уходит в аудит. ``probability`` денормализована из ``confidence_grade`` и
    фиксируется ради неизменяемости. ``brier_score``/``scored_at`` проставляет
    домен scoring **один раз** при разрешении события — здесь только хранятся.
    """

    user_id: uuid.UUID
    event_id: uuid.UUID
    confidence_grade: ConfidenceGrade
    probability: Decimal
    is_locked: bool = False
    brier_score: Decimal | None = None
    scored_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def place(
        cls,
        *,
        user_id: uuid.UUID,
        event_id: uuid.UUID,
        grade: ConfidenceGrade,
        now: datetime | None = None,
    ) -> Prediction:
        """Фабрика нового прогноза: выводит вероятность из градации.

        Проверку, что событие принимает прогнозы, делает прикладной слой
        (use-case) до вызова фабрики — домен сущности об окне события не знает.
        """
        moment = now or _utcnow()
        return cls(
            user_id=user_id,
            event_id=event_id,
            confidence_grade=grade,
            probability=probability_for_grade(grade),
            created_at=moment,
            updated_at=moment,
        )

    def change_grade(
        self, grade: ConfidenceGrade, *, now: datetime | None = None
    ) -> bool:
        """Меняет градацию (и производную вероятность) до блокировки.

        Возвращает ``True``, если значение действительно изменилось (нужен ли
        UPDATE и запись в историю). Идемпотентность: повтор той же градации —
        no-op (``False``).

        Поднимает :class:`PredictionLockedError`, если прогноз уже заблокирован
        — это последний рубеж инварианта «после дедлайна правок нет» на уровне
        сущности (первый рубеж — проверка окна события в use-case).
        """
        if self.is_locked:
            raise PredictionLockedError(
                "Прогноз заблокирован после закрытия приёма — изменение запрещено"
            )
        if grade is self.confidence_grade:
            return False
        self.confidence_grade = grade
        self.probability = probability_for_grade(grade)
        self.updated_at = now or _utcnow()
        return True

    def lock(self, *, now: datetime | None = None) -> bool:
        """Блокирует прогноз (после ``closes_at``); делает правки невозможными.

        Возвращает ``True``, если состояние изменилось (был не заблокирован).
        Идемпотентна: повторная блокировка — no-op.
        """
        if self.is_locked:
            return False
        self.is_locked = True
        self.updated_at = now or _utcnow()
        return True
