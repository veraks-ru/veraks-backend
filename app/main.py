"""Фабрика FastAPI-приложения и регистрация обработчиков ошибок.

Доменные исключения маппятся в HTTP-ответы здесь, в одном месте, чтобы
прикладной/доменный слой не зависел от деталей транспорта.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

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
_ERROR_STATUS: dict[type[IdentityError], int] = {
    InvalidSnilsError: status.HTTP_400_BAD_REQUEST,
    UnconfirmedEsiaAccountError: status.HTTP_403_FORBIDDEN,
    AccountDeletedError: status.HTTP_403_FORBIDDEN,
    AccountSuspendedError: status.HTTP_403_FORBIDDEN,
    InvalidStateError: status.HTTP_400_BAD_REQUEST,
    EsiaExchangeError: status.HTTP_502_BAD_GATEWAY,
    InvalidTokenError: status.HTTP_401_UNAUTHORIZED,
    UserNotFoundError: status.HTTP_404_NOT_FOUND,
}


def _resolve_status(exc: IdentityError) -> int:
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

    app.include_router(identity_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Liveness-проба."""
        return {"status": "ok"}

    return app


app = create_app()
