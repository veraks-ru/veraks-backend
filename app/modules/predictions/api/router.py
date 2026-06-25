"""FastAPI-роутер домена predictions (`/events/{id}/prediction`).

Эндпоинты тонкие: валидируют вход (pydantic), дёргают use-case и маппят
результат. Прогноз ставит аутентифицированный пользователь — автор берётся из
сессии (identity), а не из тела запроса. Доменные ошибки маппятся в HTTP
централизованно в ``app/main.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.identity.api.dependencies import CurrentUser
from app.modules.predictions.api.dependencies import (
    get_my_prediction,
    get_place_prediction,
)
from app.modules.predictions.api.schemas import (
    PlacePredictionRequest,
    PredictionResponse,
)
from app.modules.predictions.application.use_cases import (
    GetMyPrediction,
    PlacePrediction,
)

router = APIRouter(tags=["predictions"])


@router.put(
    "/events/{event_id}/prediction",
    response_model=PredictionResponse,
    summary="Поставить/изменить свой прогноз (до дедлайна)",
)
async def put_prediction(
    event_id: uuid.UUID,
    payload: PlacePredictionRequest,
    current_user: CurrentUser,
    uc: Annotated[PlacePrediction, Depends(get_place_prediction)],
) -> PredictionResponse:
    """Upsert прогноза текущего пользователя по событию.

    Принимает градацию уверенности → выводит вероятность. Если приём закрыт
    (дедлайн прошёл/событие не открыто) — доменная ошибка ``409``.
    """
    prediction = await uc.execute(
        user_id=current_user.id,
        event_id=event_id,
        grade=payload.confidence_grade,
    )
    return PredictionResponse.from_domain(prediction)


@router.get(
    "/events/{event_id}/prediction/me",
    response_model=PredictionResponse,
    summary="Мой прогноз по событию",
)
async def get_my_prediction_endpoint(
    event_id: uuid.UUID,
    current_user: CurrentUser,
    uc: Annotated[GetMyPrediction, Depends(get_my_prediction)],
) -> PredictionResponse:
    """Возвращает прогноз текущего пользователя или ``404``, если его нет."""
    prediction = await uc.execute(user_id=current_user.id, event_id=event_id)
    return PredictionResponse.from_domain(prediction)
