"""Composition root модуля identity (FastAPI DI).

Здесь — и только здесь — конкретные адаптеры связываются с портами и
собираются use-cases. Остальной код зависит от абстракций. Благодаря этому
в тестах достаточно переопределить несколько провайдеров.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import Cookie, Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SettingsDep
from app.db.session import get_session
from app.redis import get_redis
from app.modules.identity.adapters.esia_gateway import EsiaOidcGateway
from app.modules.identity.adapters.repository import SqlAlchemyUserRepository
from app.modules.identity.adapters.security import (
    FernetFieldEncryptor,
    HmacSnilsHasher,
    JwtTokenIssuer,
)
from app.modules.identity.adapters.stores import (
    RedisRefreshTokenStore,
    RedisStateStore,
)
from app.modules.identity.application.use_cases import (
    CompleteEsiaLogin,
    GetCurrentUser,
    GetPublicProfile,
    InitiateEsiaLogin,
    LogoutSession,
    RefreshSession,
    UpdateMyProfile,
)
from app.modules.identity.domain.entities import User
from app.modules.identity.domain.errors import IdentityError
from app.modules.identity.ports.esia import EsiaGateway
from app.modules.identity.ports.repositories import UserRepository
from app.modules.identity.ports.security import (
    FieldEncryptor,
    RefreshTokenStore,
    SnilsHasher,
    StateStore,
    TokenIssuer,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_redis_client() -> Redis:
    """Провайдер Redis (переопределяется в тестах)."""
    return get_redis()


RedisDep = Annotated[Redis, Depends(get_redis_client)]


async def get_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент для запросов к шлюзу ЕСИА (на запрос)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        yield client


# ── Порты → адаптеры ──────────────────────────────────────────────────────


def get_user_repository(session: SessionDep) -> UserRepository:
    """Репозиторий пользователей."""
    return SqlAlchemyUserRepository(session)


def get_esia_gateway(
    settings: SettingsDep,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> EsiaGateway:
    """Шлюз ЕСИА."""
    return EsiaOidcGateway(settings.esia, client)


@lru_cache
def _snils_hasher(key: str) -> HmacSnilsHasher:
    return HmacSnilsHasher(key)


def get_snils_hasher(settings: SettingsDep) -> SnilsHasher:
    """HMAC-хешер СНИЛС."""
    return _snils_hasher(settings.security.snils_hmac_key)


@lru_cache
def _encryptor(key: str) -> FernetFieldEncryptor:
    return FernetFieldEncryptor(key)


def get_field_encryptor(settings: SettingsDep) -> FieldEncryptor:
    """Шифратор ФИО."""
    return _encryptor(settings.security.field_encryption_key)


def get_token_issuer(settings: SettingsDep) -> TokenIssuer:
    """Выпуск/верификация JWT."""
    sec = settings.security
    return JwtTokenIssuer(
        secret=sec.jwt_secret,
        algorithm=sec.jwt_algorithm,
        access_ttl_seconds=sec.access_token_ttl_seconds,
        refresh_ttl_seconds=sec.refresh_token_ttl_seconds,
    )


def get_state_store(redis: RedisDep) -> StateStore:
    """Хранилище OIDC-state."""
    return RedisStateStore(redis)


def get_refresh_store(redis: RedisDep) -> RefreshTokenStore:
    """Реестр refresh-токенов."""
    return RedisRefreshTokenStore(redis)


# ── Use-cases ─────────────────────────────────────────────────────────────


def get_initiate_login(
    esia: Annotated[EsiaGateway, Depends(get_esia_gateway)],
    state_store: Annotated[StateStore, Depends(get_state_store)],
) -> InitiateEsiaLogin:
    """Use-case инициации логина."""
    return InitiateEsiaLogin(esia=esia, state_store=state_store)


def get_complete_login(
    settings: SettingsDep,
    esia: Annotated[EsiaGateway, Depends(get_esia_gateway)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    hasher: Annotated[SnilsHasher, Depends(get_snils_hasher)],
    encryptor: Annotated[FieldEncryptor, Depends(get_field_encryptor)],
    tokens: Annotated[TokenIssuer, Depends(get_token_issuer)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
    state_store: Annotated[StateStore, Depends(get_state_store)],
) -> CompleteEsiaLogin:
    """Use-case завершения логина (find-or-create + сессия)."""
    sec = settings.security
    return CompleteEsiaLogin(
        esia=esia,
        users=users,
        snils_hasher=hasher,
        encryptor=encryptor,
        tokens=tokens,
        refresh_store=refresh_store,
        state_store=state_store,
        require_confirmed=settings.esia.require_confirmed,
        access_ttl_seconds=sec.access_token_ttl_seconds,
        refresh_ttl_seconds=sec.refresh_token_ttl_seconds,
    )


def get_refresh_session(
    settings: SettingsDep,
    users: Annotated[UserRepository, Depends(get_user_repository)],
    tokens: Annotated[TokenIssuer, Depends(get_token_issuer)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
) -> RefreshSession:
    """Use-case обновления сессии."""
    sec = settings.security
    return RefreshSession(
        users=users,
        tokens=tokens,
        refresh_store=refresh_store,
        access_ttl_seconds=sec.access_token_ttl_seconds,
        refresh_ttl_seconds=sec.refresh_token_ttl_seconds,
    )


def get_logout_session(
    tokens: Annotated[TokenIssuer, Depends(get_token_issuer)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
) -> LogoutSession:
    """Use-case завершения сессии."""
    return LogoutSession(tokens=tokens, refresh_store=refresh_store)


def get_current_user_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    tokens: Annotated[TokenIssuer, Depends(get_token_issuer)],
) -> GetCurrentUser:
    """Use-case загрузки текущего пользователя."""
    return GetCurrentUser(users=users, tokens=tokens)


def get_public_profile_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> GetPublicProfile:
    """Use-case публичного профиля по хэндлу."""
    return GetPublicProfile(users=users)


def get_update_profile_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> UpdateMyProfile:
    """Use-case редактирования своего профиля."""
    return UpdateMyProfile(users=users)


# ── Аутентификация запроса ────────────────────────────────────────────────


async def get_current_user(
    uc: Annotated[GetCurrentUser, Depends(get_current_user_uc)],
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Cookie()] = None,
) -> User:
    """FastAPI-зависимость: текущий пользователь из Bearer-заголовка или cookie."""
    token = _extract_bearer(authorization) or access_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется авторизация"
        )
    try:
        return await uc.from_access_token(token)
    except IdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc


async def get_current_user_optional(
    uc: Annotated[GetCurrentUser, Depends(get_current_user_uc)],
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Cookie()] = None,
) -> User | None:
    """Как :func:`get_current_user`, но возвращает ``None`` вместо 401.

    Для публичных эндпоинтов с опциональной авторизацией: анонимному зрителю
    показываем только публичное, авторизованному — с учётом его прав.
    """
    token = _extract_bearer(authorization) or access_token
    if not token:
        return None
    try:
        return await uc.from_access_token(token)
    except IdentityError:
        return None


def _extract_bearer(header: str | None) -> str | None:
    """Достаёт токен из заголовка ``Authorization: Bearer <token>``."""
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalCurrentUser = Annotated[User | None, Depends(get_current_user_optional)]
