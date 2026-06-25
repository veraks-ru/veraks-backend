"""Use-cases домена identity.

Каждый класс — одна бизнес-операция. Зависимости передаются только через
порты (конструктор), поэтому use-cases полностью изолированы от FastAPI,
SQLAlchemy и сети и покрываются юнит-тестами с фейками.
"""

from __future__ import annotations

import secrets
import uuid

from app.modules.identity.application.dto import (
    AuthorizationRedirect,
    LoginResult,
    SessionClaims,
    SessionTokens,
)
from app.modules.identity.domain.entities import (
    User,
    UserStatus,
    generate_username_seed,
)
from app.modules.identity.domain.errors import (
    InvalidStateError,
    InvalidTokenError,
    UserNotFoundError,
)
from app.modules.identity.domain.policies import (
    ensure_account_can_authenticate,
    ensure_esia_confirmed,
)
from app.modules.identity.domain.value_objects import EsiaIdentity
from app.modules.identity.ports.esia import EsiaGateway
from app.modules.identity.ports.repositories import (
    SnilsAlreadyExistsError,
    UsernameTakenError,
    UserRepository,
)
from app.modules.identity.ports.security import (
    FieldEncryptor,
    RefreshTokenStore,
    SnilsHasher,
    StateStore,
    TokenIssuer,
)

_STATE_TTL_SECONDS = 600
_MAX_USERNAME_ATTEMPTS = 1000
# Сколько раз переаллоцировать username при гонке на UNIQUE(username) во вставке.
_MAX_REGISTER_ATTEMPTS = 5


class InitiateEsiaLogin:
    """Шаг 1: сгенерировать ``state`` и отдать URL авторизации ЕСИА."""

    def __init__(self, *, esia: EsiaGateway, state_store: StateStore) -> None:
        self._esia = esia
        self._state_store = state_store

    async def execute(self) -> AuthorizationRedirect:
        """Создаёт одноразовый state (анти-CSRF) и URL страницы ЕСИА."""
        state = secrets.token_urlsafe(32)
        await self._state_store.save(state, _STATE_TTL_SECONDS)
        url = self._esia.build_authorization_url(state=state)
        return AuthorizationRedirect(authorization_url=url, state=state)


class CompleteEsiaLogin:
    """Шаг 2: обработать callback ЕСИА (find-or-create + выпуск сессии).

    Реализует инвариант «один человек — один аккаунт»:
    поиск по ``snils_hash`` → вход в существующий аккаунт либо создание нового.
    Гонку параллельных регистраций ловим по UNIQUE-нарушению и повторяем поиск.
    """

    def __init__(
        self,
        *,
        esia: EsiaGateway,
        users: UserRepository,
        snils_hasher: SnilsHasher,
        encryptor: FieldEncryptor,
        tokens: TokenIssuer,
        refresh_store: RefreshTokenStore,
        state_store: StateStore,
        require_confirmed: bool,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> None:
        self._esia = esia
        self._users = users
        self._snils_hasher = snils_hasher
        self._encryptor = encryptor
        self._tokens = tokens
        self._refresh_store = refresh_store
        self._state_store = state_store
        self._require_confirmed = require_confirmed
        self._access_ttl = access_ttl_seconds
        self._refresh_ttl = refresh_ttl_seconds

    async def execute(self, *, code: str, state: str) -> LoginResult:
        """Полный цикл обмена кода на сессию."""
        if not await self._state_store.consume(state):
            raise InvalidStateError("Неизвестный или просроченный state")

        esia_tokens = await self._esia.exchange_code(code=code)
        identity = await self._esia.fetch_identity(esia_tokens)

        ensure_esia_confirmed(identity, require_confirmed=self._require_confirmed)
        snils_hash = self._snils_hasher.hash(identity.snils)

        user, is_new = await self._find_or_create(identity, snils_hash)
        session = await self._issue_session(user)
        return LoginResult(user_id=user.id, tokens=session, is_new_user=is_new)

    async def _find_or_create(
        self, identity: EsiaIdentity, snils_hash: str
    ) -> tuple[User, bool]:
        """Находит аккаунт по snils_hash или создаёт новый (с защитой от гонки)."""
        existing = await self._users.get_by_snils_hash(snils_hash)
        if existing is not None:
            ensure_account_can_authenticate(existing)
            if existing.apply_esia_refresh(
                identity=identity, real_name_enc=self._encrypt_name(identity)
            ):
                existing = await self._users.update(existing)
            return existing, False

        user = User.register_from_esia(
            identity=identity,
            snils_hash=snils_hash,
            username=await self._allocate_username(identity),
            real_name_enc=self._encrypt_name(identity),
        )
        # Регистрация с защитой от двух гонок на UNIQUE-индексах:
        #  * snils_hash — тот же гражданин зарегался параллельно → входим в него;
        #  * username — хэндл заняли между pre-check и INSERT → переаллоцируем и
        #    повторяем (логин не должен падать из-за гонки имён).
        for _attempt in range(_MAX_REGISTER_ATTEMPTS):
            try:
                created = await self._users.add(user)
            except SnilsAlreadyExistsError:
                winner = await self._users.get_by_snils_hash(snils_hash)
                if winner is None:  # pragma: no cover — UNIQUE гарантирует наличие
                    raise
                ensure_account_can_authenticate(winner)
                return winner, False
            except UsernameTakenError:
                user.username = await self._allocate_username(identity)
                continue
            return created, True
        # Исчерпали попытки переаллокации хэндла — крайне маловероятно.
        raise UsernameTakenError(  # pragma: no cover — защитный предел
            "Не удалось подобрать свободный username при регистрации"
        )

    def _encrypt_name(self, identity: EsiaIdentity) -> bytes | None:
        """Шифрует ФИО для хранения (None, если ФИО пустое)."""
        full = identity.full_name()
        return self._encryptor.encrypt(full) if full else None

    async def _allocate_username(self, identity: EsiaIdentity) -> str:
        """Подбирает свободный хэндл (seed + числовой суффикс при коллизии)."""
        seed = generate_username_seed(identity)
        if not await self._users.username_exists(seed):
            return seed
        for suffix in range(1, _MAX_USERNAME_ATTEMPTS):
            candidate = f"{seed}{suffix}"
            if not await self._users.username_exists(candidate):
                return candidate
        # Крайне маловероятно: добавляем случайный хвост.
        return f"{seed}{secrets.token_hex(4)}"

    async def _issue_session(self, user: User) -> SessionTokens:
        """Выпускает пару токенов и регистрирует refresh для отзыва."""
        claims = SessionClaims(user_id=user.id, role=user.role)
        access = self._tokens.issue_access(claims)
        refresh, jti = self._tokens.issue_refresh(claims)
        await self._refresh_store.register(jti, self._refresh_ttl)
        return SessionTokens(
            access_token=access,
            refresh_token=refresh,
            access_ttl_seconds=self._access_ttl,
            refresh_ttl_seconds=self._refresh_ttl,
        )


class RefreshSession:
    """Обновление access-токена по refresh-токену (с ротацией refresh)."""

    def __init__(
        self,
        *,
        users: UserRepository,
        tokens: TokenIssuer,
        refresh_store: RefreshTokenStore,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> None:
        self._users = users
        self._tokens = tokens
        self._refresh_store = refresh_store
        self._access_ttl = access_ttl_seconds
        self._refresh_ttl = refresh_ttl_seconds

    async def execute(self, *, refresh_token: str) -> SessionTokens:
        """Проверяет refresh, отзывает старый jti, выпускает новую пару."""
        claims, jti = self._tokens.verify_refresh(refresh_token)
        if not await self._refresh_store.is_active(jti):
            raise InvalidTokenError("Refresh-токен отозван")

        user = await self._users.get_by_id(claims.user_id)
        if user is None:
            raise UserNotFoundError("Пользователь не найден")
        ensure_account_can_authenticate(user)

        await self._refresh_store.revoke(jti)  # ротация: старый токен инвалидируем
        new_claims = SessionClaims(user_id=user.id, role=user.role)
        access = self._tokens.issue_access(new_claims)
        refresh, new_jti = self._tokens.issue_refresh(new_claims)
        await self._refresh_store.register(new_jti, self._refresh_ttl)
        return SessionTokens(
            access_token=access,
            refresh_token=refresh,
            access_ttl_seconds=self._access_ttl,
            refresh_ttl_seconds=self._refresh_ttl,
        )


class LogoutSession:
    """Завершение сессии: отзыв refresh-токена (клиент чистит cookie)."""

    def __init__(self, *, tokens: TokenIssuer, refresh_store: RefreshTokenStore) -> None:
        self._tokens = tokens
        self._refresh_store = refresh_store

    async def execute(self, *, refresh_token: str | None) -> None:
        """Отзывает refresh-токен, если он валиден; молча игнорирует мусор."""
        if not refresh_token:
            return
        try:
            _, jti = self._tokens.verify_refresh(refresh_token)
        except InvalidTokenError:
            return
        await self._refresh_store.revoke(jti)


class GetCurrentUser:
    """Загрузка текущего пользователя по access-токену."""

    def __init__(self, *, users: UserRepository, tokens: TokenIssuer) -> None:
        self._users = users
        self._tokens = tokens

    async def from_access_token(self, token: str) -> User:
        """Верифицирует access-токен и возвращает активного пользователя."""
        claims = self._tokens.verify_access(token)
        return await self.by_id(claims.user_id)

    async def by_id(self, user_id: uuid.UUID) -> User:
        """Загружает пользователя по id с проверкой статуса."""
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError("Пользователь не найден")
        ensure_account_can_authenticate(user)
        return user


class GetPublicProfile:
    """Публичный профиль по хэндлу (псевдоним, без ПДн)."""

    def __init__(self, *, users: UserRepository) -> None:
        self._users = users

    async def execute(self, *, username: str) -> User:
        """Возвращает активного пользователя по username или 404.

        Удалённые/заблокированные аккаунты публично не показываются.
        """
        user = await self._users.get_by_username(username)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise UserNotFoundError("Профиль не найден")
        return user


class UpdateMyProfile:
    """Редактирование собственного профиля (display_name)."""

    def __init__(self, *, users: UserRepository) -> None:
        self._users = users

    async def execute(
        self, *, user_id: uuid.UUID, display_name: str | None
    ) -> User:
        """Применяет правки профиля и сохраняет при изменении."""
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError("Пользователь не найден")
        if user.edit_profile(display_name=display_name):
            return await self._users.update(user)
        return user
