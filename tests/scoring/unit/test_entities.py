"""Юнит-тесты доменных сущностей и value-objects скоринга."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.modules.scoring.domain.entities import Rating, ScopeType
from app.modules.scoring.domain.value_objects import (
    PredictionVote,
    ResolvedEvent,
    quantize_score,
)


def test_scope_type_values() -> None:
    assert ScopeType.GLOBAL.value == "global"
    assert ScopeType.CATEGORY.value == "category"
    assert ScopeType.SEASON.value == "season"


def test_rating_holds_scope_and_metrics() -> None:
    user_id = uuid.uuid4()
    rating = Rating(
        user_id=user_id,
        scope_type=ScopeType.GLOBAL,
        scope_id=None,
        mean_brier=Decimal("0.18000"),
        skill_score=Decimal("0.07520"),
        calibration_error=Decimal("0.07500"),
        n_resolved=30,
    )
    assert rating.user_id == user_id
    assert rating.scope_id is None
    assert rating.rank == 0  # ранг проставляется при перестроении лидерборда


def test_rating_assign_rank() -> None:
    rating = Rating(
        user_id=uuid.uuid4(),
        scope_type=ScopeType.SEASON,
        scope_id=uuid.uuid4(),
        mean_brier=Decimal("0.20000"),
        skill_score=Decimal("0.04545"),
        calibration_error=Decimal("0.05000"),
        n_resolved=60,
    )
    rating.assign_rank(1)
    assert rating.rank == 1


def test_resolved_event_projections() -> None:
    event = ResolvedEvent(
        event_id=uuid.uuid4(),
        category_id=uuid.uuid4(),
        season_id=None,
        outcome=1,
        votes=(
            PredictionVote(user_id=uuid.uuid4(), probability=0.9),
            PredictionVote(user_id=uuid.uuid4(), probability=0.7),
        ),
    )
    assert event.predictor_count == 2
    assert event.probabilities() == [0.9, 0.7]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.09, Decimal("0.09000")),
        (0.123456, Decimal("0.12346")),  # округление до 5 знаков
        (0.0752238, Decimal("0.07522")),
        (-0.0925, Decimal("-0.09250")),  # skill score может быть отрицательным
    ],
)
def test_quantize_score_to_five_places(value: float, expected: Decimal) -> None:
    assert quantize_score(value) == expected
