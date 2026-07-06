"""Юнит-тесты use-cases identity (логика, через порты-фейки).

Покрывают ядро домена: гарантию «один человек — один аккаунт», политику
подтверждённой учётки, надгробие удалённого аккаунта, ротацию refresh.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.modules.identity.adapters.security import (
    FernetFieldEncryptor,
    HmacSnilsHasher,
    JwtTokenIssuer,
)
from app.modules.identity.application.use_cases import (
    CompleteEsiaLogin,
    GetCurrentUser,
    GetPublicProfile,
    LogoutSession,
    RefreshSession,
    UpdateMyProfile,
)
from app.modules.identity.domain.entities import User, UserRole, UserStatus
from app.modules.identity.domain.errors import (
    AccountDeletedError,
    InvalidStateError,
    InvalidTokenError,
    UnconfirmedEsiaAccountError,
    UserNotFoundError,
)
from app.modules.identity.domain.value_objects import EsiaIdentity
from app.modules.identity.ports.repositories import UsernameTakenError
from tests.identity.fakes import (
    FakeEsiaGateway,
    FakeRefreshTokenStore,
    FakeStateStore,
    InMemoryUserRepository,
)


def _build_complete_login(
    *,
    identity: EsiaIdentity,
    repo: InMemoryUserRepository,
    state_store: FakeStateStore,
    refresh_store: FakeRefreshTokenStore,
    hasher: HmacSnilsHasher,
    encryptor: FernetFieldEncryptor,
    token_issuer: JwtTokenIssuer,
    require_confirmed: bool = True,
) -> CompleteEsiaLogin:
    return CompleteEsiaLogin(
        esia=FakeEsiaGateway(identity),
        users=repo,
        snils_hasher=hasher,
        encryptor=encryptor,
        tokens=token_issuer,
        refresh_store=refresh_store,
        state_store=state_store,
        require_confirmed=require_confirmed,
        access_ttl_seconds=900,
        refresh_ttl_seconds=3600,
    )


@pytest.fixture
def repo() -> InMemoryUserRepository:
    return InMemoryUserRepository()


@pytest.fixture
def state_store() -> FakeStateStore:
    store = FakeStateStore()
    store.seed("valid-state")
    return store


@pytest.fixture
def refresh_store() -> FakeRefreshTokenStore:
    return FakeRefreshTokenStore()


async def test_first_login_creates_account(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    result = await uc.execute(code="abc", state="valid-state")

    assert result.is_new_user is True
    stored = await repo.get_by_id(result.user_id)
    assert stored is not None
    # Псевдонимный хэндл, НЕ производный от ФИО (H-PII).
    assert stored.username.startswith("predictor-")
    # display_name по умолчанию = псевдоним, а не реальное ФИО.
    assert stored.display_name == stored.username
    # ФИО хранится зашифрованным, не открытым текстом.
    assert stored.real_name_enc is not None
    assert b"Petrov" not in stored.real_name_enc
    # Access-токен валиден.
    claims = token_issuer.verify_access(result.tokens.access_token)
    assert claims.user_id == result.user_id


class _UsernameRaceRepo(InMemoryUserRepository):
    """Эмулирует гонку UNIQUE(username): первый ``add`` падает, затем успех."""

    def __init__(self) -> None:
        super().__init__()
        self._raised = False

    async def add(self, user: User) -> User:
        if not self._raised:
            self._raised = True
            raise UsernameTakenError(user.username)
        return await super().add(user)


async def test_login_retries_on_username_race(
    confirmed_identity, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    """Гонка на UNIQUE(username) при регистрации не валит логин — хэндл переаллоцируется."""
    repo = _UsernameRaceRepo()
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )

    result = await uc.execute(code="abc", state="valid-state")

    assert result.is_new_user is True
    stored = await repo.get_by_id(result.user_id)
    assert stored is not None and stored.username.startswith("predictor-")


async def test_second_login_same_citizen_reuses_account(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    """Один человек = один аккаунт: повторный вход не создаёт второй аккаунт."""
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    first = await uc.execute(code="abc", state="valid-state")

    state_store.seed("valid-state-2")
    second = await uc.execute(code="def", state="valid-state-2")

    assert second.is_new_user is False
    assert second.user_id == first.user_id


async def test_unconfirmed_account_rejected(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    identity = dataclasses.replace(confirmed_identity, trusted=False)
    uc = _build_complete_login(
        identity=identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    with pytest.raises(UnconfirmedEsiaAccountError):
        await uc.execute(code="abc", state="valid-state")


async def test_deleted_account_is_tombstone(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    """Удалённый аккаунт нельзя пере-зарегистрировать тем же СНИЛС."""
    snils_hash = snils_hasher.hash(confirmed_identity.snils)
    await repo.add(
        User(
            esia_oid="old-oid",
            snils_hash=snils_hash,
            username="старый",
            display_name="Старый",
            real_name_enc=None,
            status=UserStatus.DELETED,
        )
    )
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    with pytest.raises(AccountDeletedError):
        await uc.execute(code="abc", state="valid-state")


async def test_invalid_state_rejected(
    confirmed_identity, repo, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=FakeStateStore(),  # пустой → state неизвестен
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    with pytest.raises(InvalidStateError):
        await uc.execute(code="abc", state="unknown")


async def test_username_collision_gets_suffix(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    """Разные граждане с одинаковым ФИО получают разные хэндлы."""
    first_uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    first = await first_uc.execute(code="abc", state="valid-state")

    other_identity = dataclasses.replace(
        confirmed_identity, oid="esia-oid-2", snils=_other_snils()
    )
    state_store.seed("valid-state-2")
    second_uc = _build_complete_login(
        identity=other_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    second = await second_uc.execute(code="def", state="valid-state-2")

    u1 = await repo.get_by_id(first.user_id)
    u2 = await repo.get_by_id(second.user_id)
    assert u1 is not None and u2 is not None
    assert u1.username != u2.username


async def test_refresh_rotates_and_revokes_old(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    result = await login.execute(code="abc", state="valid-state")
    old_refresh = result.tokens.refresh_token

    refresh_uc = RefreshSession(
        users=repo,
        tokens=token_issuer,
        refresh_store=refresh_store,
        access_ttl_seconds=900,
        refresh_ttl_seconds=3600,
    )
    rotated = await refresh_uc.execute(refresh_token=old_refresh)
    assert rotated.refresh_token != old_refresh

    # Старый refresh отозван — повторное использование запрещено.
    with pytest.raises(InvalidTokenError):
        await refresh_uc.execute(refresh_token=old_refresh)


async def test_refresh_reuse_revokes_whole_family(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    """Детект кражи (M-REFRESH): повтор украденного токена рвёт всё семейство."""
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    result = await login.execute(code="abc", state="valid-state")
    old_refresh = result.tokens.refresh_token

    refresh_uc = RefreshSession(
        users=repo,
        tokens=token_issuer,
        refresh_store=refresh_store,
        access_ttl_seconds=900,
        refresh_ttl_seconds=3600,
    )
    rotated = await refresh_uc.execute(refresh_token=old_refresh)
    new_refresh = rotated.refresh_token

    # Повтор уже ротированного (украденного) токена детектится...
    with pytest.raises(InvalidTokenError):
        await refresh_uc.execute(refresh_token=old_refresh)
    # ...и рвёт всё семейство: даже «легитимный» новый токен больше не работает
    # (обе сессии — атакующая и жертвенная — принудительно завершены).
    with pytest.raises(InvalidTokenError):
        await refresh_uc.execute(refresh_token=new_refresh)


async def test_logout_revokes_refresh(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    result = await login.execute(code="abc", state="valid-state")

    logout = LogoutSession(tokens=token_issuer, refresh_store=refresh_store)
    await logout.execute(refresh_token=result.tokens.refresh_token)

    refresh_uc = RefreshSession(
        users=repo,
        tokens=token_issuer,
        refresh_store=refresh_store,
        access_ttl_seconds=900,
        refresh_ttl_seconds=3600,
    )
    with pytest.raises(InvalidTokenError):
        await refresh_uc.execute(refresh_token=result.tokens.refresh_token)


async def test_get_current_user_by_access_token(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, encryptor, token_issuer
) -> None:
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    result = await login.execute(code="abc", state="valid-state")

    uc = GetCurrentUser(users=repo, tokens=token_issuer)
    user = await uc.from_access_token(result.tokens.access_token)
    assert user.id == result.user_id


def _other_snils():
    """Второй валидный СНИЛС для теста коллизий хэндлов (087-654-303 00)."""
    from app.modules.identity.domain.value_objects import Snils

    return Snils.parse("08765430300")


# ── Профили (GetPublicProfile / UpdateMyProfile) ──────────────────────────


def _user(username="alice", display="Алиса", status=UserStatus.ACTIVE) -> User:
    return User(
        esia_oid=f"oid-{username}",
        snils_hash=f"hash-{username}",
        username=username,
        display_name=display,
        real_name_enc=None,
        role=UserRole.USER,
        status=status,
    )


async def test_public_profile_returns_active_user(repo) -> None:
    await repo.add(_user(username="alice", display="Алиса"))
    profile = await GetPublicProfile(users=repo).execute(username="alice")
    assert profile.username == "alice"
    assert profile.display_name == "Алиса"


async def test_public_profile_case_insensitive(repo) -> None:
    await repo.add(_user(username="alice"))
    profile = await GetPublicProfile(users=repo).execute(username="ALICE")
    assert profile.username == "alice"


async def test_public_profile_unknown_raises(repo) -> None:
    with pytest.raises(UserNotFoundError):
        await GetPublicProfile(users=repo).execute(username="ghost")


async def test_public_profile_hides_suspended(repo) -> None:
    await repo.add(_user(username="bob", status=UserStatus.SUSPENDED))
    with pytest.raises(UserNotFoundError):
        await GetPublicProfile(users=repo).execute(username="bob")


async def test_update_profile_changes_display_name(repo) -> None:
    user = _user(username="carol", display="Старое")
    await repo.add(user)
    updated = await UpdateMyProfile(users=repo).execute(
        user_id=user.id, display_name="Новое имя"
    )
    assert updated.display_name == "Новое имя"
    stored = await repo.get_by_id(user.id)
    assert stored is not None and stored.display_name == "Новое имя"


async def test_update_profile_noop_when_none(repo) -> None:
    user = _user(username="dave", display="Дэйв")
    await repo.add(user)
    updated = await UpdateMyProfile(users=repo).execute(
        user_id=user.id, display_name=None
    )
    assert updated.display_name == "Дэйв"


async def test_update_profile_unknown_user_raises(repo) -> None:
    import uuid

    with pytest.raises(UserNotFoundError):
        await UpdateMyProfile(users=repo).execute(
            user_id=uuid.uuid4(), display_name="X"
        )
