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

# Отмена автопродления при самостоятельном удалении аккаунта (T4): identity
# знает о billing ТОЛЬКО здесь, на уровне HTTP composition root — по тому же
# паттерну, что events.api.dependencies.get_lock_event_predictions знает о
# predictions (см. её докстринг). Ни domain, ни application identity billing
# не импортируют.
from app.modules.billing.adapters.clock import SystemClock as _BillingSystemClock
from app.modules.billing.adapters.repositories import (
    SqlAlchemySubscriptionRepository as _BillingSqlAlchemySubscriptionRepository,
)
from app.modules.billing.application.use_cases import (
    CancelSubscription as BillingCancelSubscription,
)
from app.modules.billing.ports.repositories import (
    SubscriptionRepository as BillingSubscriptionRepository,
)
from app.modules.identity.adapters.esia_gateway import EsiaOidcGateway
from app.modules.identity.adapters.id_token import EsiaIdTokenVerifier
from app.modules.identity.adapters.repository import (
    SqlAlchemyConsentRepository,
    SqlAlchemyUserRepository,
)
from app.modules.identity.adapters.security import (
    FernetFieldEncryptor,
    HmacEsiaOidHasher,
    HmacSnilsHasher,
    JwtTokenIssuer,
)
from app.modules.identity.adapters.stores import (
    RedisMagicLinkStore,
    RedisRefreshTokenStore,
    RedisStateStore,
)
from app.modules.identity.application.login import SessionIssuer
from app.modules.identity.application.use_cases import (
    ChangeUserEmail,
    CompleteEmailLogin,
    CompleteEsiaLogin,
    CompleteOnboarding,
    DeleteMyAccount,
    GetCurrentUser,
    GetMyConsents,
    GetOnboardingStatus,
    GetPublicProfile,
    InitiateEsiaLogin,
    ListUsers,
    LogoutSession,
    RefreshSession,
    ReinstateUser,
    RequestEmailLogin,
    SuspendUser,
    UpdateMyProfile,
)
from app.modules.identity.domain.consent import ConsentDocument
from app.modules.identity.domain.entities import User, UserRole
from app.modules.identity.domain.errors import (
    AuthProviderDisabledError,
    ConsentRequiredError,
    IdentityError,
)
from app.modules.identity.ports.consents import ConsentRepository
from app.modules.identity.ports.esia import EsiaGateway
from app.modules.identity.ports.magic_link import MagicLinkStore
from app.modules.identity.ports.repositories import UserRepository
from app.modules.identity.ports.security import (
    EsiaOidHasher,
    FieldEncryptor,
    RefreshTokenStore,
    SnilsHasher,
    StateStore,
    TokenIssuer,
)
from app.redis import get_redis
from app.shared.audit.adapters.trail import (
    ImmediatelyCommittingAuditTrail,
    SqlAlchemyAuditTrail,
)
from app.shared.audit.ports.audit_trail import AuditTrail
from app.shared.http import http_client
from app.shared.mail.adapters.factory import build_email_sender
from app.shared.mail.ports.sender import EmailSender

# Способ фиксации согласий через веб-онбординг (PRD/т.з. T2).
_ONBOARDING_METHOD = "onboarding_web"

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_audit_trail(session: SessionDep) -> AuditTrail:
    """Общий append-only аудит-журнал (``app/shared/audit``)."""
    return SqlAlchemyAuditTrail(session)


AuditDep = Annotated[AuditTrail, Depends(get_audit_trail)]


def get_security_audit_trail() -> AuditTrail:
    """Аудит для событий безопасности, которые пишутся прямо перед ``raise``.

    В отличие от :func:`get_audit_trail`, НЕ делит сессию/транзакцию запроса:
    ``RefreshSession`` детектит повторное использование refresh-токена и сразу
    поднимает ``InvalidTokenError`` — обычная запись через сессию запроса
    откатилась бы вместе с ней (``get_session`` делает rollback при
    исключении), и след инцидента терялся бы. ``ImmediatelyCommittingAuditTrail``
    коммитит запись в своей короткой транзакции сразу же — она переживает
    любой последующий откат (см. её докстринг).
    """
    return ImmediatelyCommittingAuditTrail()


SecurityAuditDep = Annotated[AuditTrail, Depends(get_security_audit_trail)]


def get_redis_client() -> Redis:
    """Провайдер Redis (переопределяется в тестах)."""
    return get_redis()


RedisDep = Annotated[Redis, Depends(get_redis_client)]


async def get_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент для запросов к шлюзу ЕСИА (на запрос)."""
    async with http_client(timeout=10.0) as client:
        yield client


# ── Порты → адаптеры ──────────────────────────────────────────────────────


def get_user_repository(session: SessionDep) -> UserRepository:
    """Репозиторий пользователей."""
    return SqlAlchemyUserRepository(session)


def get_consent_repository(session: SessionDep) -> ConsentRepository:
    """Репозиторий согласий (152-ФЗ)."""
    return SqlAlchemyConsentRepository(session)


def get_required_consents(settings: SettingsDep) -> list[ConsentDocument]:
    """Реестр обязательных документов и их текущих версий из конфигурации."""
    return [
        ConsentDocument(document=document, version=version)
        for document, version in settings.consents.required_documents.items()
    ]


# Валидаторы id_token живут дольше запроса: в них кэш ключей JWKS. Ключ карты —
# значимые настройки (в тестах их подменяют, и кэш не должен «залипать»).
# ``lru_cache`` не годится: pydantic-модель настроек нехешируема.
_ID_TOKEN_VERIFIERS: dict[tuple[str, ...], EsiaIdTokenVerifier] = {}


def get_id_token_verifier(settings: SettingsDep) -> EsiaIdTokenVerifier:
    """Валидатор ``id_token``; один на конфигурацию (кэш JWKS переживает запросы)."""
    esia = settings.esia
    key = (
        esia.jwks_url,
        esia.issuer,
        esia.client_id,
        esia.id_token_algorithms,
        str(esia.jwks_cache_ttl_seconds),
    )
    verifier = _ID_TOKEN_VERIFIERS.get(key)
    if verifier is None:
        verifier = EsiaIdTokenVerifier(esia)
        _ID_TOKEN_VERIFIERS[key] = verifier
    return verifier


def get_esia_gateway(
    settings: SettingsDep,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    id_token_verifier: Annotated[
        EsiaIdTokenVerifier, Depends(get_id_token_verifier)
    ],
) -> EsiaGateway:
    """Шлюз ЕСИА (HTTP-клиент — на запрос, валидатор id_token — на процесс)."""
    return EsiaOidcGateway(settings.esia, client, id_token_verifier)


@lru_cache
def _snils_hasher(key: str) -> HmacSnilsHasher:
    return HmacSnilsHasher(key)


def get_snils_hasher(settings: SettingsDep) -> SnilsHasher:
    """HMAC-хешер СНИЛС."""
    return _snils_hasher(settings.security.snils_hmac_key)


@lru_cache
def _esia_oid_hasher(key: str) -> HmacEsiaOidHasher:
    return HmacEsiaOidHasher(key)


def get_esia_oid_hasher(settings: SettingsDep) -> EsiaOidHasher:
    """HMAC-хешер идентификатора ЕСИА (тот же ключ, что у СНИЛС; изоляция —
    доменным префиксом сообщения, см. docstring ``HmacEsiaOidHasher``)."""
    return _esia_oid_hasher(settings.security.snils_hmac_key)


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


def get_magic_link_store(redis: RedisDep) -> MagicLinkStore:
    """Хранилище одноразовых ссылок входа и счётчика писем на адрес."""
    return RedisMagicLinkStore(redis)


def get_email_sender(settings: SettingsDep) -> EmailSender:
    """Транспорт писем: SMTP при заданном ``MAIL_HOST``, иначе — лог.

    Выбор осознанно без fail-fast (обоснование —
    ``app.shared.mail.adapters.factory``): вход по email сейчас единственный,
    и падение старта из-за ненастроенного SMTP означало бы недоступность
    платформы целиком.
    """
    return build_email_sender(settings.mail)


def get_session_issuer(
    settings: SettingsDep,
    tokens: Annotated[TokenIssuer, Depends(get_token_issuer)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
) -> SessionIssuer:
    """Выпуск сессии — один и тот же для обоих потоков входа."""
    sec = settings.security
    return SessionIssuer(
        tokens=tokens,
        refresh_store=refresh_store,
        access_ttl_seconds=sec.access_token_ttl_seconds,
        refresh_ttl_seconds=sec.refresh_token_ttl_seconds,
    )


# ── Гарды провайдеров входа ───────────────────────────────────────────────


def require_esia_provider(settings: SettingsDep) -> None:
    """Гард: эндпоинты ЕСИА доступны, только если провайдер включён.

    При выключенной ЕСИА её код остаётся в приложении (вернуть провайдер —
    это одна переменная окружения), но снаружи эндпоинтов как бы нет: 404.
    Без гарда обращение к ним при пустых ``ESIA_*`` давало бы 500 из недр
    HTTP-клиента — сбой вместо внятного «такого способа входа здесь нет».
    """
    if not settings.auth.esia_enabled:
        raise AuthProviderDisabledError("Вход через Госуслуги сейчас недоступен")


def require_email_provider(settings: SettingsDep) -> None:
    """Гард: эндпоинты входа по email доступны, только если провайдер включён."""
    if not settings.auth.email_enabled:
        raise AuthProviderDisabledError("Вход по email сейчас недоступен")


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
    esia_oid_hasher: Annotated[EsiaOidHasher, Depends(get_esia_oid_hasher)],
    encryptor: Annotated[FieldEncryptor, Depends(get_field_encryptor)],
    sessions: Annotated[SessionIssuer, Depends(get_session_issuer)],
    state_store: Annotated[StateStore, Depends(get_state_store)],
    audit: AuditDep,
) -> CompleteEsiaLogin:
    """Use-case завершения логина ЕСИА (find-or-create + сессия)."""
    return CompleteEsiaLogin(
        esia=esia,
        users=users,
        snils_hasher=hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        sessions=sessions,
        state_store=state_store,
        require_confirmed=settings.esia.require_confirmed,
        audit=audit,
    )


def get_request_email_login(
    settings: SettingsDep,
    links: Annotated[MagicLinkStore, Depends(get_magic_link_store)],
    sender: Annotated[EmailSender, Depends(get_email_sender)],
) -> RequestEmailLogin:
    """Use-case запроса ссылки входа (письмо со ссылкой на фронт)."""
    return RequestEmailLogin(
        links=links, sender=sender, link_base_url=settings.mail.link_base_url
    )


def get_complete_email_login(
    links: Annotated[MagicLinkStore, Depends(get_magic_link_store)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    sessions: Annotated[SessionIssuer, Depends(get_session_issuer)],
    audit: AuditDep,
) -> CompleteEmailLogin:
    """Use-case входа по ссылке из письма (find-or-create по email + сессия)."""
    return CompleteEmailLogin(
        links=links, users=users, sessions=sessions, audit=audit
    )


def get_refresh_session(
    settings: SettingsDep,
    users: Annotated[UserRepository, Depends(get_user_repository)],
    tokens: Annotated[TokenIssuer, Depends(get_token_issuer)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
    audit: SecurityAuditDep,
) -> RefreshSession:
    """Use-case обновления сессии.

    Аудит — ``get_security_audit_trail`` (не сессия запроса, см. её докстринг):
    единственная запись, которую пишет ``RefreshSession``
    (``identity.refresh.reuse_detected``), делается прямо перед ``raise`` и
    должна пережить откат транзакции запроса.
    """
    sec = settings.security
    return RefreshSession(
        users=users,
        tokens=tokens,
        refresh_store=refresh_store,
        access_ttl_seconds=sec.access_token_ttl_seconds,
        refresh_ttl_seconds=sec.refresh_token_ttl_seconds,
        audit=audit,
    )


def get_logout_session(
    tokens: Annotated[TokenIssuer, Depends(get_token_issuer)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
    audit: AuditDep,
) -> LogoutSession:
    """Use-case завершения сессии."""
    return LogoutSession(tokens=tokens, refresh_store=refresh_store, audit=audit)


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


def get_onboarding_status_uc(
    consents: Annotated[ConsentRepository, Depends(get_consent_repository)],
    required: Annotated[list[ConsentDocument], Depends(get_required_consents)],
) -> GetOnboardingStatus:
    """Use-case расчёта ``needs_onboarding``/недостающих согласий."""
    return GetOnboardingStatus(consents=consents, required=required)


def get_complete_onboarding_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    consents: Annotated[ConsentRepository, Depends(get_consent_repository)],
    required: Annotated[list[ConsentDocument], Depends(get_required_consents)],
) -> CompleteOnboarding:
    """Use-case прохождения онбординга."""
    return CompleteOnboarding(
        users=users, consents=consents, required=required, method=_ONBOARDING_METHOD
    )


def get_my_consents_uc(
    consents: Annotated[ConsentRepository, Depends(get_consent_repository)],
) -> GetMyConsents:
    """Use-case списка согласий текущего пользователя."""
    return GetMyConsents(consents=consents)


def get_delete_my_account_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
    audit: AuditDep,
) -> DeleteMyAccount:
    """Use-case самостоятельного удаления аккаунта (152-ФЗ)."""
    return DeleteMyAccount(users=users, refresh_store=refresh_store, audit=audit)


def get_suspend_user_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
    audit: AuditDep,
) -> SuspendUser:
    """Use-case блокировки аккаунта модерацией (B7)."""
    return SuspendUser(users=users, refresh_store=refresh_store, audit=audit)


def get_reinstate_user_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    audit: AuditDep,
) -> ReinstateUser:
    """Use-case снятия блокировки (B7)."""
    return ReinstateUser(users=users, audit=audit)


def get_change_user_email_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_store: Annotated[RefreshTokenStore, Depends(get_refresh_store)],
    audit: AuditDep,
) -> ChangeUserEmail:
    """Use-case смены email аккаунта администратором (по обращению в поддержку)."""
    return ChangeUserEmail(users=users, refresh_store=refresh_store, audit=audit)


def get_list_users_uc(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> ListUsers:
    """Use-case постраничного списка пользователей для админки (B7)."""
    return ListUsers(users=users)


def get_billing_subscription_repository(
    session: SessionDep,
) -> BillingSubscriptionRepository:
    """Композит-рут HTTP: репозиторий подписок billing.

    Нужен только чтобы перед удалением аккаунта проверить, есть ли активная
    подписка, которую нужно отменить (см. ``get_cancel_subscription_on_delete``).
    """
    return _BillingSqlAlchemySubscriptionRepository(session)


def get_cancel_subscription_on_delete(session: SessionDep) -> BillingCancelSubscription:
    """Композит-рут HTTP: отмена автопродления при удалении аккаунта.

    Переиспользует use-case billing ``CancelSubscription`` — тот же путь, что
    и ручной ``POST /billing/subscriptions/{id}/cancel``, чтобы у удалённого
    аккаунта не продолжались списания. Возврат уже списанных средств не
    делаем — это вопрос к юристу, не к коду.
    """
    return BillingCancelSubscription(
        subscriptions=_BillingSqlAlchemySubscriptionRepository(session),
        audit=SqlAlchemyAuditTrail(session),
        clock=_BillingSystemClock(),
    )


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


async def require_onboarded_user(
    current_user: CurrentUser,
    uc: Annotated[GetOnboardingStatus, Depends(get_onboarding_status_uc)],
) -> User:
    """Гард: действие доступно только пользователю с завершённым онбордингом.

    Участие в конкурсе (постановка/изменение прогноза, предложение события)
    юридически возможно только после акцепта оферты и согласия на обработку
    ПДн (PRD §7, 152-ФЗ). Клиентский гард ``AuthProvider`` отправляет на
    ``/onboarding``, но это UX: пользователь с прямым доступом к API обошёл бы
    его — поэтому запрет продублирован на сервере.

    Таблица истины одна и та же для ``GET /auth/me`` и для этого гарда —
    use-case :class:`GetOnboardingStatus` (сверка принятых согласий с реестром
    обязательных документов из конфигурации). Ничего не дублируется: подняв
    версию документа через ``CONSENTS_*``, юрист блокирует участие до
    переподтверждения.

    Гард живёт в composition root **identity** и переиспользуется соседними
    доменами по тому же шву, что и :data:`CurrentUser` (predictions/events
    импортируют только ``api/dependencies`` identity, не её domain — прецедент
    identity→billing из T4).
    """
    needs_onboarding, missing = await uc.execute(user=current_user)
    if needs_onboarding:
        documents = ", ".join(doc.document for doc in missing)
        detail = (
            "Подтвердите согласия, чтобы участвовать: " + documents
            if documents
            else "Завершите онбординг, чтобы участвовать"
        )
        raise ConsentRequiredError(detail)
    return current_user


OnboardedUser = Annotated[User, Depends(require_onboarded_user)]


def require_admin(current_user: CurrentUser) -> User:
    """Гард: модерация пользователей (``/admin/users/*``) — только администратору."""
    if current_user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступно только администратору",
        )
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]
