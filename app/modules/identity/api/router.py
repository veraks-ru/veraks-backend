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
    get_onboarding_status_uc,
    get_refresh_session,
)
from app.modules.identity.api.schemas import (
    AccessTokenResponse,
    AuthMeResponse,
    CallbackRequest,
)
from app.modules.identity.application.dto import SessionTokens
from app.modules.identity.domain.errors import (
    EsiaAuthorizationDeniedError,
    EsiaExchangeError,
    IdentityError,
)
from app.modules.identity.application.use_cases import (
    CompleteEsiaLogin,
    GetOnboardingStatus,
    InitiateEsiaLogin,
    LogoutSession,
    RefreshSession,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE = "refresh_token"
_ACCESS_COOKIE = "access_token"
_STATE_COOKIE = "oidc_state"
_STATE_COOKIE_TTL = 600  # синхронно с TTL state в сторе (10 минут)

# Коды OIDC-ошибок, означающие «пользователь не дал согласие / прервал вход».
_DENIED_OIDC_ERRORS = frozenset(
    {"access_denied", "consent_required", "login_required", "interaction_required"}
)
# Остальные коды, определённые RFC 6749 §4.1.2.1 и OIDC Core §3.1.2.6: это
# сбой/кривой запрос на стороне провайдера — 502, а не отмена пользователем.
_KNOWN_OIDC_ERRORS = _DENIED_OIDC_ERRORS | {
    "invalid_request",
    "unauthorized_client",
    "unsupported_response_type",
    "invalid_scope",
    "server_error",
    "temporarily_unavailable",
    "account_selection_required",
    "invalid_request_uri",
    "invalid_request_object",
    "request_not_supported",
    "request_uri_not_supported",
    "registration_not_supported",
}


def _authorization_error(code: str, description: str | None) -> IdentityError:
    """Переводит ``?error=...`` из callback'а в доменную ошибку.

    Ни описание от провайдера, ни сырой код в ответ не пробрасываем: значение
    приходит из query-string, то есть управляется тем, кто открыл ссылку, и в
    теле ответа стало бы отражённым текстом. Наружу идёт только код из
    известного списка, всё прочее — ``unknown``; по нему фронт различает
    «отменено» и «сбой».
    """
    if code in _DENIED_OIDC_ERRORS:
        return EsiaAuthorizationDeniedError(
            "Вход через Госуслуги отменён — подтверждение не получено"
        )
    safe_code = code if code in _KNOWN_OIDC_ERRORS else "unknown"
    return EsiaExchangeError(f"Госуслуги вернули ошибку авторизации: {safe_code}")


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


def clear_session_cookies(response: Response, settings: SettingsDep) -> None:
    """Удаляет сессионные cookie (logout и самостоятельное удаление аккаунта).

    ``domain`` обязателен и при удалении: браузер сопоставляет cookie по тройке
    (имя, домен, путь). Без него в проде (``SECURITY_COOKIE_DOMAIN=.veraks.ru``)
    ставился бы Set-Cookie на хост ``api.veraks.ru``, а cookie домена
    ``.veraks.ru`` оставалась жить — logout визуально «не срабатывал».
    """
    domain = settings.security.cookie_domain or None
    response.delete_cookie(_ACCESS_COOKIE, domain=domain)
    response.delete_cookie(_REFRESH_COOKIE, path="/auth", domain=domain)


@router.get("/esia/login", summary="Редирект на страницу авторизации ЕСИА")
async def esia_login(
    settings: SettingsDep,
    uc: Annotated[InitiateEsiaLogin, Depends(get_initiate_login)],
) -> RedirectResponse:
    """Генерирует анти-CSRF state и редиректит пользователя в ЕСИА.

    ``state`` дополнительно кладётся в httpOnly-cookie: на callback он сверяется
    с параметром запроса, привязывая OIDC-поток к ИНИЦИИРОВАВШЕМУ его браузеру
    (защита от login-CSRF / фиксации сессии — M-OIDC).
    """
    redirect = await uc.execute()
    resp = RedirectResponse(
        redirect.authorization_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
    resp.set_cookie(
        _STATE_COOKIE,
        redirect.state,
        max_age=_STATE_COOKIE_TTL,
        path="/auth",
        httponly=True,
        secure=settings.security.cookie_secure,
        samesite="lax",
        domain=settings.security.cookie_domain or None,
    )
    return resp


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
    oidc_state: Annotated[str | None, Cookie()] = None,
) -> AccessTokenResponse:
    """Завершает OIDC-поток, ставит cookie и отдаёт access-токен.

    Сверяет ``state`` из запроса с ``oidc_state``-cookie (привязка к браузеру):
    несовпадение/отсутствие → 400, поток не продолжается.

    Провайдер может вернуть вместо кода ошибку (``?error=access_denied`` —
    пользователь отказался в Госуслугах). Тогда обмена не происходит: отказ
    маппится в доменную ошибку и человеческое сообщение (см.
    :func:`_authorization_error`). ``oidc_state``-cookie в этой ветке не
    чистим (ответ формирует централизованный обработчик ошибок, ему cookie не
    передать) — она короткоживущая и гаснет по своему TTL, а сам ``state``
    остаётся неиспользованным и протухает в сторе.
    """
    if params.error:
        raise _authorization_error(params.error, params.error_description)
    if not params.code or not params.state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Госуслуги не передали код авторизации",
        )
    if not oidc_state or oidc_state != params.state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Недействительный state (не совпадает с cookie браузера)",
        )
    result = await uc.execute(code=params.code, state=params.state)
    response.delete_cookie(
        _STATE_COOKIE,
        path="/auth",
        domain=settings.security.cookie_domain or None,
    )
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
    settings: SettingsDep,
    uc: Annotated[LogoutSession, Depends(get_logout_session)],
    refresh_token: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Отзывает refresh-токен и очищает cookie."""
    await uc.execute(refresh_token=refresh_token)
    clear_session_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get(
    "/me",
    response_model=AuthMeResponse,
    summary="Текущий пользователь + статус онбординга",
)
async def me(
    current_user: CurrentUser,
    uc: Annotated[GetOnboardingStatus, Depends(get_onboarding_status_uc)],
) -> AuthMeResponse:
    """Профиль аутентифицированного пользователя (без ПДн) + 152-ФЗ-статус.

    ``needs_onboarding``/``missing_consents`` говорят фронту, нужно ли
    показать экран онбординга (первый вход или юрист поменял версию
    документа в конфиге — см. ``ConsentsSettings``).
    """
    needs_onboarding, missing = await uc.execute(user=current_user)
    return AuthMeResponse.build(
        current_user, needs_onboarding=needs_onboarding, missing=missing
    )
