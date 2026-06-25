"""Фабрика FastAPI-приложения и регистрация обработчиков ошибок.

Доменные исключения маппятся в HTTP-ответы здесь, в одном месте, чтобы
прикладной/доменный слой не зависел от деталей транспорта.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.modules.events.api.router import router as events_router
from app.modules.events.domain.errors import (
    CategoryNotFoundError,
    CategorySlugTakenError,
    EventEditNotAllowedError,
    EventError,
    EventNotFoundError,
    EventPermissionError,
    InvalidEventDataError,
    InvalidEventTransitionError,
    InvalidEventWindowError,
)
from app.modules.predictions.api.router import router as predictions_router
from app.modules.predictions.domain.errors import (
    PredictionError,
    PredictionLockedError,
    PredictionNotFoundError,
    PredictionsClosedError,
    PredictionTargetEventNotFoundError,
)
from app.modules.scoring.api.router import router as scoring_router
from app.modules.scoring.domain.errors import (
    EventNotResolvedError,
    RatingNotFoundError,
    ScoringError,
    ScoringPermissionError,
    ScoringTargetEventNotFoundError,
)
from app.modules.identity.api.router import router as identity_router
from app.modules.identity.domain.errors import (
    AccountDeletedError,
    AccountSuspendedError,
    EsiaExchangeError,
    IdentityError,
    InvalidSnilsError,
    InvalidStateError,
    InvalidTokenError,
    UnconfirmedEsiaAccountError,
    UserNotFoundError,
)

# Карта «доменная ошибка → HTTP-статус».
_ERROR_STATUS: dict[type[Exception], int] = {
    InvalidSnilsError: status.HTTP_400_BAD_REQUEST,
    UnconfirmedEsiaAccountError: status.HTTP_403_FORBIDDEN,
    AccountDeletedError: status.HTTP_403_FORBIDDEN,
    AccountSuspendedError: status.HTTP_403_FORBIDDEN,
    InvalidStateError: status.HTTP_400_BAD_REQUEST,
    EsiaExchangeError: status.HTTP_502_BAD_GATEWAY,
    InvalidTokenError: status.HTTP_401_UNAUTHORIZED,
    UserNotFoundError: status.HTTP_404_NOT_FOUND,
    # events
    EventNotFoundError: status.HTTP_404_NOT_FOUND,
    CategoryNotFoundError: status.HTTP_404_NOT_FOUND,
    EventPermissionError: status.HTTP_403_FORBIDDEN,
    CategorySlugTakenError: status.HTTP_409_CONFLICT,
    InvalidEventTransitionError: status.HTTP_409_CONFLICT,
    EventEditNotAllowedError: status.HTTP_409_CONFLICT,
    InvalidEventWindowError: status.HTTP_400_BAD_REQUEST,
    InvalidEventDataError: status.HTTP_400_BAD_REQUEST,
    # predictions
    PredictionTargetEventNotFoundError: status.HTTP_404_NOT_FOUND,
    PredictionNotFoundError: status.HTTP_404_NOT_FOUND,
    PredictionsClosedError: status.HTTP_409_CONFLICT,
    PredictionLockedError: status.HTTP_409_CONFLICT,
    # scoring
    ScoringTargetEventNotFoundError: status.HTTP_404_NOT_FOUND,
    RatingNotFoundError: status.HTTP_404_NOT_FOUND,
    EventNotResolvedError: status.HTTP_409_CONFLICT,
    ScoringPermissionError: status.HTTP_403_FORBIDDEN,
}


def _resolve_status(exc: Exception) -> int:
    """Подбирает HTTP-статус по типу исключения (с учётом наследования)."""
    for error_type, code in _ERROR_STATUS.items():
        if isinstance(exc, error_type):
            return code
    return status.HTTP_400_BAD_REQUEST


def create_app() -> FastAPI:
    """Собирает приложение: роутеры доменов + обработчики ошибок."""
    app = FastAPI(title="Orakul — биржа репутации предсказателей")

    @app.exception_handler(IdentityError)
    async def _identity_error_handler(
        _request: Request, exc: IdentityError
    ) -> JSONResponse:
        """Единый маппинг доменных ошибок identity в JSON-ответ."""
        return JSONResponse(
            status_code=_resolve_status(exc),
            content={"detail": str(exc), "error": type(exc).__name__},
        )

    @app.exception_handler(EventError)
    async def _event_error_handler(
        _request: Request, exc: EventError
    ) -> JSONResponse:
        """Единый маппинг доменных ошибок events в JSON-ответ."""
        return JSONResponse(
            status_code=_resolve_status(exc),
            content={"detail": str(exc), "error": type(exc).__name__},
        )

    @app.exception_handler(PredictionError)
    async def _prediction_error_handler(
        _request: Request, exc: PredictionError
    ) -> JSONResponse:
        """Единый маппинг доменных ошибок predictions в JSON-ответ."""
        return JSONResponse(
            status_code=_resolve_status(exc),
            content={"detail": str(exc), "error": type(exc).__name__},
        )

    @app.exception_handler(ScoringError)
    async def _scoring_error_handler(
        _request: Request, exc: ScoringError
    ) -> JSONResponse:
        """Единый маппинг доменных ошибок scoring в JSON-ответ."""
        return JSONResponse(
            status_code=_resolve_status(exc),
            content={"detail": str(exc), "error": type(exc).__name__},
        )

    app.include_router(identity_router)
    app.include_router(events_router)
    app.include_router(predictions_router)
    app.include_router(scoring_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Liveness-проба."""
        return {"status": "ok"}

    return app


app = create_app()
