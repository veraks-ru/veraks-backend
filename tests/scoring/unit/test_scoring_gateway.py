"""Юнит-тесты батч-группировки прогнозов в адаптере scoring_gateway.

``_group_predictions_by_event`` — чистая функция без I/O, вынесенная из
``SqlAlchemyEventScoringGateway._load_predictions_by_event`` (батч-загрузка
прогнозов множества событий одним SQL-запросом вместо запроса на событие).
Здесь она проверяется без БД: только группировка/порядок/пустой ввод.
"""

from __future__ import annotations

import uuid

from app.modules.predictions.adapters.orm import PredictionORM
from app.modules.scoring.adapters.scoring_gateway import _group_predictions_by_event


def _prediction(event_id: uuid.UUID, user_id: uuid.UUID) -> PredictionORM:
    # Только поля, нужные группировке (event_id) и переносу в votes (user_id) —
    # остальные атрибуты ORM здесь не участвуют.
    return PredictionORM(event_id=event_id, user_id=user_id)


def test_empty_input_gives_empty_mapping() -> None:
    assert _group_predictions_by_event([]) == {}


def test_groups_by_event_id() -> None:
    event_a, event_b = uuid.uuid4(), uuid.uuid4()
    user_1, user_2, user_3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    predictions = [
        _prediction(event_a, user_1),
        _prediction(event_b, user_2),
        _prediction(event_a, user_3),
    ]

    grouped = _group_predictions_by_event(predictions)

    assert set(grouped) == {event_a, event_b}
    assert [p.user_id for p in grouped[event_a]] == [user_1, user_3]
    assert [p.user_id for p in grouped[event_b]] == [user_2]


def test_preserves_arrival_order_within_group() -> None:
    """Внутри одной группы порядок — как во входной последовательности."""
    event_id = uuid.uuid4()
    users = [uuid.uuid4() for _ in range(5)]
    predictions = [_prediction(event_id, u) for u in users]

    grouped = _group_predictions_by_event(predictions)

    assert [p.user_id for p in grouped[event_id]] == users


def test_event_without_predictions_gets_no_key() -> None:
    """Событие, для которого прогнозов не пришло, не создаёт пустой список.

    Вызывающая сторона (``list_resolved_events``/``get_resolved_event``)
    сама подставляет ``()`` через ``.get(event_id, ())``.
    """
    known_event, other_event = uuid.uuid4(), uuid.uuid4()
    grouped = _group_predictions_by_event([_prediction(known_event, uuid.uuid4())])

    assert other_event not in grouped
    assert grouped.get(other_event, ()) == ()
