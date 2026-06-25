"""Юнит-тесты доменных сущностей predictions.

Покрывают ядро домена: детерминированное отображение «градация → вероятность»,
смену градации (идемпотентность, обновление вероятности) и инвариант
неизменяемости после блокировки.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.modules.predictions.domain.entities import (
    ConfidenceGrade,
    Prediction,
    probability_for_grade,
)
from app.modules.predictions.domain.errors import PredictionLockedError


@pytest.mark.parametrize(
    ("grade", "expected"),
    [
        (ConfidenceGrade.DEFINITELY_NO, Decimal("0.10")),
        (ConfidenceGrade.PROBABLY_NO, Decimal("0.30")),
        (ConfidenceGrade.FIFTY_FIFTY, Decimal("0.50")),
        (ConfidenceGrade.PROBABLY_YES, Decimal("0.70")),
        (ConfidenceGrade.DEFINITELY_YES, Decimal("0.90")),
    ],
)
def test_probability_for_grade_mapping(
    grade: ConfidenceGrade, expected: Decimal
) -> None:
    assert probability_for_grade(grade) == expected


def test_mapping_covers_every_grade() -> None:
    # Тотальность: функция определена для всех членов enum.
    for grade in ConfidenceGrade:
        assert isinstance(probability_for_grade(grade), Decimal)


def test_place_derives_probability_from_grade() -> None:
    prediction = Prediction.place(
        user_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        grade=ConfidenceGrade.PROBABLY_YES,
    )
    assert prediction.confidence_grade is ConfidenceGrade.PROBABLY_YES
    assert prediction.probability == Decimal("0.70")
    assert prediction.is_locked is False
    assert prediction.brier_score is None


def test_change_grade_updates_probability() -> None:
    prediction = Prediction.place(
        user_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        grade=ConfidenceGrade.FIFTY_FIFTY,
    )
    changed = prediction.change_grade(ConfidenceGrade.DEFINITELY_NO)
    assert changed is True
    assert prediction.probability == Decimal("0.10")


def test_change_grade_same_value_is_noop() -> None:
    prediction = Prediction.place(
        user_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        grade=ConfidenceGrade.FIFTY_FIFTY,
    )
    assert prediction.change_grade(ConfidenceGrade.FIFTY_FIFTY) is False


def test_change_grade_rejected_after_lock() -> None:
    prediction = Prediction.place(
        user_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        grade=ConfidenceGrade.FIFTY_FIFTY,
    )
    prediction.lock()
    with pytest.raises(PredictionLockedError):
        prediction.change_grade(ConfidenceGrade.DEFINITELY_YES)


def test_lock_is_idempotent() -> None:
    prediction = Prediction.place(
        user_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        grade=ConfidenceGrade.FIFTY_FIFTY,
    )
    assert prediction.lock() is True
    assert prediction.lock() is False
