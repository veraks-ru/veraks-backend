"""FastAPI-роутер домена identity (`/auth`).

Эндпоинты тонкие: валидируют вход, дергают use-case, маппят результат и
ставят/чистят cookie. Вся бизнес-логика — в прикладном слое.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse

from app.config import SettingsDep
from app.modules.identity.api.dependencies import (
    CurrentUser,
    get_complete_login,
    get_initiate_login,
    get_logout_session,
    get_refresh_session,
)
from app.modules.identity.api.schemas import (
    AccessTokenResponse,
    CallbackRequest,
    MeResponse,
)
from app.modules.identity.application.dto import SessionTokens
from app.modules.identity.application.use_cases import (
    CompleteEsiaLogin,
    InitiateEsiaLogin,
    LogoutSession,
    RefreshSession,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE = "refresh_token"
_ACCESS_COOKIE = "access_token"


def _set_session_cookies(
    response: Response, tokens: SessionTokens, settings: SettingsDep
) -> None:
    """Кладёт access/refresh в httpOnly+Secure cookie (защита от XSS-кражи)."""
    secure = settings.security.cookie_secure
    domain = settings.security.cookie_domain or None
    response.set_cookie(
        _ACCESS_COOKIE,
        tokens.access_token,
        max_age=tokens.access_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        domain=domain,
    )
    # refresh ограничен путём /auth — на остальные запросы не уходит.
    response.set_cookie(
        _REFRESH_COOKIE,
        tokens.refresh_token,
        max_age=tokens.refresh_ttl_seconds,
        path="/auth",
        httponly=True,
        secure=secure,
        samesite="lax",
        domain=domain,
    )


def _clear_session_cookies(response: Response) -> None:
    """Удаляет сессионные cookie при logout."""
    response.delete_cookie(_ACCESS_COOKIE)
    response.delete_cookie(_REFRESH_COOKIE, path="/auth")


@router.get("/esia/login", summary="Редирект на страницу авторизации ЕСИА")
async def esia_login(
    uc: Annotated[InitiateEsiaLogin, Depends(get_initiate_login)],
) -> RedirectResponse:
    """Генерирует анти-CSRF state и редиректит пользователя в ЕСИА."""
    redirect = await uc.execute()
    return RedirectResponse(
        redirect.authorization_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )


@router.get(
    "/esia/callback",
    response_model=AccessTokenResponse,
    summary="Callback ЕСИА: обмен кода на сессию (find-or-create)",
)
async def esia_callback(
    params: Annotated[CallbackRequest, Depends()],
    response: Response,
    settings: SettingsDep,
    uc: Annotated[CompleteEsiaLogin, Depends(get_complete_login)],
) -> AccessTokenResponse:
    """Завершает OIDC-поток, ставит cookie и отдаёт access-токен."""
    result = await uc.execute(code=params.code, state=params.state)
    _set_session_cookies(response, result.tokens, settings)
    if result.is_new_user:
        response.status_code = status.HTTP_201_CREATED
    return AccessTokenResponse(
        access_token=result.tokens.access_token,
        expires_in=result.tokens.access_ttl_seconds,
    )


@router.post(
    "/refresh",
    response_model=AccessTokenResponse,
    summary="Обновление access-токена по refresh",
)
async def refresh(
    response: Response,
    settings: SettingsDep,
    uc: Annotated[RefreshSession, Depends(get_refresh_session)],
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> AccessTokenResponse:
    """Ротация сессии: новый access + новый refresh, старый refresh отзывается."""
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Нет refresh-токена"
        )
    tokens = await uc.execute(refresh_token=refresh_token)
    _set_session_cookies(response, tokens, settings)
    return AccessTokenResponse(
        access_token=tokens.access_token, expires_in=tokens.access_ttl_seconds
    )


@router.post(
    "/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Завершение сессии"
)
async def logout(
    response: Response,
    uc: Annotated[LogoutSession, Depends(get_logout_session)],
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Отзывает refresh-токен и очищает cookie."""
    await uc.execute(refresh_token=refresh_token)
    _clear_session_cookies(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=MeResponse, summary="Текущий пользователь")
async def me(current_user: CurrentUser) -> MeResponse:
    """Возвращает профиль аутентифицированного пользователя (без ПДн)."""
    return MeResponse.from_domain(current_user)
