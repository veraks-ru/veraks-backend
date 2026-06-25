"""Юнит-тесты use-cases predictions (через порты-фейки).

Покрывают: приём градации → вероятность, upsert (создание/правка),
идемпотентность, запрет постановки на закрытое/несуществующее событие,
чтение своего прогноза, запись истории в аудит и массовую блокировку.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.modules.predictions.application.use_cases import (
    GetEventPredictionSummary,
    GetMyPrediction,
    LockEventPredictions,
    PlacePrediction,
)
from app.modules.predictions.domain.entities import ConfidenceGrade
from app.modules.predictions.domain.errors import (
    PredictionNotFoundError,
    PredictionsClosedError,
    PredictionSummaryHiddenError,
    PredictionTargetEventNotFoundError,
)
from tests.predictions.conftest import FIXED_NOW
from tests.predictions.fakes import (
    FakeAuditRecorder,
    FakeClock,
    FakeEventGateway,
    InMemoryPredictionRepository,
)


@pytest.fixture
def predictions() -> InMemoryPredictionRepository:
    return InMemoryPredictionRepository()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FIXED_NOW)


@pytest.fixture
def audit() -> FakeAuditRecorder:
    return FakeAuditRecorder()


def _place_uc(predictions, events, clock, audit) -> PlacePrediction:
    return PlacePrediction(
        predictions=predictions, events=events, clock=clock, audit=audit
    )


async def test_place_creates_prediction_when_open(
    predictions, clock, audit, open_snapshot, user_id, event_id
) -> None:
    events = FakeEventGateway([open_snapshot])
    uc = _place_uc(predictions, events, clock, audit)

    result = await uc.execute(
        user_id=user_id, event_id=event_id, grade=ConfidenceGrade.DEFINITELY_YES
    )

    assert result.probability == Decimal("0.90")
    assert await predictions.get_for_user_event(user_id, event_id) is not None
    assert [e.action for e in audit.entries] == ["prediction.created"]
    assert audit.entries[0].before is None
    assert audit.entries[0].after == "definitely_yes"


async def test_place_updates_existing_prediction(
    predictions, clock, audit, open_snapshot, user_id, event_id
) -> None:
    events = FakeEventGateway([open_snapshot])
    uc = _place_uc(predictions, events, clock, audit)

    await uc.execute(
        user_id=user_id, event_id=event_id, grade=ConfidenceGrade.FIFTY_FIFTY
    )
    updated = await uc.execute(
        user_id=user_id, event_id=event_id, grade=ConfidenceGrade.PROBABLY_NO
    )

    assert updated.probability == Decimal("0.30")
    # Одна строка (latest-wins), а в истории — создание + правка.
    assert len(await predictions.list_for_event(event_id)) == 1
    assert [e.action for e in audit.entries] == [
        "prediction.created",
        "prediction.updated",
    ]
    assert audit.entries[1].before == "fifty_fifty"
    assert audit.entries[1].after == "probably_no"


async def test_place_same_grade_is_idempotent(
    predictions, clock, audit, open_snapshot, user_id, event_id
) -> None:
    events = FakeEventGateway([open_snapshot])
    uc = _place_uc(predictions, events, clock, audit)

    await uc.execute(
        user_id=user_id, event_id=event_id, grade=ConfidenceGrade.FIFTY_FIFTY
    )
    await uc.execute(
        user_id=user_id, event_id=event_id, grade=ConfidenceGrade.FIFTY_FIFTY
    )

    # Повтор той же градации не порождает запись об изменении.
    assert [e.action for e in audit.entries] == ["prediction.created"]


async def test_place_rejected_when_event_closed(
    predictions, clock, audit, closed_snapshot, user_id, event_id
) -> None:
    events = FakeEventGateway([closed_snapshot])
    uc = _place_uc(predictions, events, clock, audit)

    with pytest.raises(PredictionsClosedError):
        await uc.execute(
            user_id=user_id, event_id=event_id, grade=ConfidenceGrade.FIFTY_FIFTY
        )


async def test_place_rejected_when_event_not_open(
    predictions, clock, audit, user_id, event_id
) -> None:
    # Окно ещё актуально по времени, но событие не в статусе open.
    from datetime import timedelta

    from app.modules.predictions.domain.value_objects import EventSnapshot

    draft_snapshot = EventSnapshot(
        event_id=event_id,
        is_open=False,
        opens_at=FIXED_NOW - timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=1),
    )
    events = FakeEventGateway([draft_snapshot])
    uc = _place_uc(predictions, events, clock, audit)

    with pytest.raises(PredictionsClosedError):
        await uc.execute(
            user_id=user_id, event_id=event_id, grade=ConfidenceGrade.FIFTY_FIFTY
        )


async def test_place_rejected_when_event_missing(
    predictions, clock, audit, user_id, event_id
) -> None:
    events = FakeEventGateway([])  # снимка нет
    uc = _place_uc(predictions, events, clock, audit)

    with pytest.raises(PredictionTargetEventNotFoundError):
        await uc.execute(
            user_id=user_id, event_id=event_id, grade=ConfidenceGrade.FIFTY_FIFTY
        )


async def test_get_my_prediction_returns_existing(
    predictions, clock, audit, open_snapshot, user_id, event_id
) -> None:
    events = FakeEventGateway([open_snapshot])
    await _place_uc(predictions, events, clock, audit).execute(
        user_id=user_id, event_id=event_id, grade=ConfidenceGrade.PROBABLY_YES
    )

    result = await GetMyPrediction(predictions=predictions).execute(
        user_id=user_id, event_id=event_id
    )
    assert result.confidence_grade is ConfidenceGrade.PROBABLY_YES


async def test_get_my_prediction_missing_raises(predictions, user_id, event_id) -> None:
    with pytest.raises(PredictionNotFoundError):
        await GetMyPrediction(predictions=predictions).execute(
            user_id=user_id, event_id=event_id
        )


def _summary_uc(predictions, events, clock) -> GetEventPredictionSummary:
    return GetEventPredictionSummary(predictions=predictions, events=events, clock=clock)


async def test_summary_aggregates_distribution_after_close(
    predictions, clock, audit, closed_snapshot, event_id
) -> None:
    # Сигнал толпы виден только после закрытия приёма (анти-якорение, §5).
    # Кладём прогнозы напрямую (окно уже закрыто) и агрегируем.
    from app.modules.predictions.domain.entities import Prediction

    grades = [
        ConfidenceGrade.DEFINITELY_YES,  # 0.90
        ConfidenceGrade.DEFINITELY_YES,  # 0.90
        ConfidenceGrade.FIFTY_FIFTY,     # 0.50
        ConfidenceGrade.PROBABLY_NO,     # 0.30
    ]
    for grade in grades:
        await predictions.add(
            Prediction.place(user_id=uuid.uuid4(), event_id=event_id, grade=grade)
        )
    events = FakeEventGateway([closed_snapshot])

    summary = await _summary_uc(predictions, events, clock).execute(event_id=event_id)

    assert summary.total_count == 4
    assert summary.distribution[ConfidenceGrade.DEFINITELY_YES] == 2
    assert summary.distribution[ConfidenceGrade.FIFTY_FIFTY] == 1
    assert summary.distribution[ConfidenceGrade.PROBABLY_NO] == 1
    assert summary.distribution[ConfidenceGrade.DEFINITELY_NO] == 0
    # Консенсус толпы c_e = среднее вероятностей = (0.9+0.9+0.5+0.3)/4 = 0.65.
    assert summary.mean_probability == Decimal("0.65")


async def test_summary_hidden_while_event_open(
    predictions, clock, open_snapshot, event_id
) -> None:
    events = FakeEventGateway([open_snapshot])
    with pytest.raises(PredictionSummaryHiddenError):
        await _summary_uc(predictions, events, clock).execute(event_id=event_id)


async def test_summary_empty_after_close(
    predictions, clock, closed_snapshot, event_id
) -> None:
    events = FakeEventGateway([closed_snapshot])
    summary = await _summary_uc(predictions, events, clock).execute(event_id=event_id)
    assert summary.total_count == 0
    assert summary.mean_probability is None


async def test_summary_unknown_event_raises(predictions, clock, event_id) -> None:
    events = FakeEventGateway([])
    with pytest.raises(PredictionTargetEventNotFoundError):
        await _summary_uc(predictions, events, clock).execute(event_id=event_id)


async def test_lock_event_predictions_locks_all(
    predictions, clock, audit, open_snapshot, event_id
) -> None:
    events = FakeEventGateway([open_snapshot])
    place = _place_uc(predictions, events, clock, audit)
    await place.execute(
        user_id=uuid.uuid4(), event_id=event_id, grade=ConfidenceGrade.FIFTY_FIFTY
    )
    await place.execute(
        user_id=uuid.uuid4(), event_id=event_id, grade=ConfidenceGrade.PROBABLY_NO
    )

    locked = await LockEventPredictions(predictions=predictions, clock=clock).execute(
        event_id=event_id
    )
    assert locked == 2
    assert all(p.is_locked for p in await predictions.list_for_event(event_id))
    # Повторный вызов идемпотентен — нечего блокировать.
    assert (
        await LockEventPredictions(predictions=predictions, clock=clock).execute(
            event_id=event_id
        )
        == 0
    )
