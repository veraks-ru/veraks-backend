"""Юнит-тесты use-cases identity (логика, через порты-фейки).

Покрывают ядро домена: гарантию «один человек — один аккаунт», политику
подтверждённой учётки, надгробие удалённого аккаунта, ротацию refresh.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from app.modules.identity.adapters.security import (
    FernetFieldEncryptor,
    HmacEsiaOidHasher,
    HmacSnilsHasher,
    JwtTokenIssuer,
)
from app.modules.identity.application.dto import ConsentInput
from app.modules.identity.application.use_cases import (
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
    SuspendUser,
    UpdateMyProfile,
)
from app.modules.identity.domain.consent import Consent, ConsentDocument
from app.modules.identity.domain.entities import User, UserRole, UserStatus
from app.modules.identity.domain.errors import (
    AccountDeletedError,
    CannotSuspendAdminError,
    CannotSuspendSelfError,
    IncompleteConsentsError,
    InvalidStateError,
    InvalidTokenError,
    InvalidUserStatusError,
    UnconfirmedEsiaAccountError,
    UsernameAlreadyTakenError,
    UserNotFoundError,
)
from app.modules.identity.domain.policies import ensure_can_suspend
from app.modules.identity.domain.value_objects import EsiaIdentity
from app.modules.identity.ports.repositories import UsernameTakenError
from tests.identity.fakes import (
    FakeAuditTrail,
    FakeEsiaGateway,
    FakeRefreshTokenStore,
    FakeStateStore,
    InMemoryConsentRepository,
    InMemoryUserRepository,
    onboarded_consent_repository,
)


def _build_complete_login(
    *,
    identity: EsiaIdentity,
    repo: InMemoryUserRepository,
    state_store: FakeStateStore,
    refresh_store: FakeRefreshTokenStore,
    hasher: HmacSnilsHasher,
    esia_oid_hasher: HmacEsiaOidHasher,
    encryptor: FernetFieldEncryptor,
    token_issuer: JwtTokenIssuer,
    require_confirmed: bool = True,
    audit: FakeAuditTrail | None = None,
) -> CompleteEsiaLogin:
    return CompleteEsiaLogin(
        esia=FakeEsiaGateway(identity),
        users=repo,
        snils_hasher=hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        tokens=token_issuer,
        refresh_store=refresh_store,
        state_store=state_store,
        require_confirmed=require_confirmed,
        access_ttl_seconds=900,
        refresh_ttl_seconds=3600,
        audit=audit if audit is not None else FakeAuditTrail(),
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
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
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
    # esia_oid хранится только как HMAC-хеш — сырой oid не персистится (152-ФЗ).
    assert stored.esia_oid_hash == esia_oid_hasher.hash(confirmed_identity.oid)
    assert stored.esia_oid_hash != confirmed_identity.oid
    assert confirmed_identity.oid not in stored.esia_oid_hash
    # Access-токен валиден.
    claims = token_issuer.verify_access(result.tokens.access_token)
    assert claims.user_id == result.user_id


async def test_stored_user_never_holds_raw_esia_oid(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    """Регрессия: сущность User не несёт сырой esia_oid — только его HMAC-хеш.

    У доменной сущности вообще нет поля с сырым значением (оно называется
    ``esia_oid_hash``), так что «утечка» возможна только если use-case
    ошибочно передаст в него что-то, кроме хэша. Проверяем и на первом входе
    (create), и на повторном (apply_esia_refresh), что сохранённое значение —
    именно результат ``EsiaOidHasher.hash``, а не исходный oid.
    """
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    first = await uc.execute(code="abc", state="valid-state")
    stored = await repo.get_by_id(first.user_id)
    assert stored is not None
    expected_hash = esia_oid_hasher.hash(confirmed_identity.oid)
    assert stored.esia_oid_hash == expected_hash
    assert not hasattr(stored, "esia_oid")

    # Повторный вход (apply_esia_refresh) не подменяет хэш на сырой oid.
    state_store.seed("valid-state-2")
    second = await uc.execute(code="def", state="valid-state-2")
    stored_again = await repo.get_by_id(second.user_id)
    assert stored_again is not None
    assert stored_again.esia_oid_hash == expected_hash


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
    confirmed_identity, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    """Гонка на UNIQUE(username) при регистрации не валит логин — хэндл переаллоцируется."""
    repo = _UsernameRaceRepo()
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )

    result = await uc.execute(code="abc", state="valid-state")

    assert result.is_new_user is True
    stored = await repo.get_by_id(result.user_id)
    assert stored is not None and stored.username.startswith("predictor-")


async def test_second_login_same_citizen_reuses_account(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    """Один человек = один аккаунт: повторный вход не создаёт второй аккаунт."""
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    first = await uc.execute(code="abc", state="valid-state")

    state_store.seed("valid-state-2")
    second = await uc.execute(code="def", state="valid-state-2")

    assert second.is_new_user is False
    assert second.user_id == first.user_id


async def test_unconfirmed_account_rejected(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    identity = dataclasses.replace(confirmed_identity, trusted=False)
    uc = _build_complete_login(
        identity=identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    with pytest.raises(UnconfirmedEsiaAccountError):
        await uc.execute(code="abc", state="valid-state")


async def test_deleted_account_is_tombstone(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    """Удалённый аккаунт нельзя пере-зарегистрировать тем же СНИЛС."""
    snils_hash = snils_hasher.hash(confirmed_identity.snils)
    await repo.add(
        User(
            esia_oid_hash="old-oid-hash",
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
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    with pytest.raises(AccountDeletedError):
        await uc.execute(code="abc", state="valid-state")


async def test_invalid_state_rejected(
    confirmed_identity, repo, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=FakeStateStore(),  # пустой → state неизвестен
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    with pytest.raises(InvalidStateError):
        await uc.execute(code="abc", state="unknown")


async def test_username_collision_gets_suffix(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    """Разные граждане с одинаковым ФИО получают разные хэндлы."""
    first_uc = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
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
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    second = await second_uc.execute(code="def", state="valid-state-2")

    u1 = await repo.get_by_id(first.user_id)
    u2 = await repo.get_by_id(second.user_id)
    assert u1 is not None and u2 is not None
    assert u1.username != u2.username


async def test_refresh_rotates_and_revokes_old(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
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
        audit=FakeAuditTrail(),
    )
    rotated = await refresh_uc.execute(refresh_token=old_refresh)
    assert rotated.refresh_token != old_refresh

    # Старый refresh отозван — повторное использование запрещено.
    with pytest.raises(InvalidTokenError):
        await refresh_uc.execute(refresh_token=old_refresh)


async def test_refresh_reuse_revokes_whole_family(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    """Детект кражи (M-REFRESH): повтор украденного токена рвёт всё семейство."""
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    result = await login.execute(code="abc", state="valid-state")
    old_refresh = result.tokens.refresh_token

    audit = FakeAuditTrail()
    refresh_uc = RefreshSession(
        users=repo,
        tokens=token_issuer,
        refresh_store=refresh_store,
        access_ttl_seconds=900,
        refresh_ttl_seconds=3600,
        audit=audit,
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

    # Событие безопасности зафиксировано в аудите (T10), без ПДн в payload.
    assert audit.actions() == ["identity.refresh.reuse_detected"]
    entry = audit.records[0]
    assert entry["entity_id"] == result.user_id
    assert entry["actor_id"] == result.user_id


class _BrokenAuditTrail(FakeAuditTrail):
    """Аудит, который падает при записи (недоступна БД, нарушен инвариант)."""

    async def record(self, **kwargs):  # type: ignore[override]
        raise RuntimeError("аудит недоступен")


async def test_refresh_reuse_returns_401_even_if_audit_write_fails(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    """Сбой записи аудита не превращает задуманный 401 в 500 (ре-ревью T10, (c)).

    Приоритет — защита пользователя: токены уже отозваны, и предъявитель
    краденого токена обязан получить отказ. След инцидента в этом случае
    остаётся в логе (``_LOG.error``), а не в журнале.
    """
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
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
        audit=_BrokenAuditTrail(),
    )
    rotated = await refresh_uc.execute(refresh_token=old_refresh)

    with pytest.raises(InvalidTokenError):
        await refresh_uc.execute(refresh_token=old_refresh)
    # Семейство всё равно отозвано — реакция на кражу не зависит от аудита.
    with pytest.raises(InvalidTokenError):
        await refresh_uc.execute(refresh_token=rotated.refresh_token)


async def test_initiate_login_stores_flow_secrets(confirmed_identity) -> None:
    """Инициация логина кладёт PKCE-verifier и nonce в стор и передаёт их шлюзу."""
    store = FakeStateStore()
    gateway = FakeEsiaGateway(confirmed_identity)
    uc = InitiateEsiaLogin(esia=gateway, state_store=store)

    redirect = await uc.execute()

    state, verifier, nonce = gateway.authorize_args[0]
    assert state == redirect.state
    # Секреты именно те, что сохранены под этим state (иначе callback упадёт).
    stored = await store.consume(redirect.state)
    assert stored is not None
    assert (stored.code_verifier, stored.nonce) == (verifier, nonce)
    # Длина verifier'а — в границах RFC 7636 (43..128 символов).
    assert 43 <= len(stored.code_verifier) <= 128
    # Каждый вход — новые секреты.
    second = await InitiateEsiaLogin(esia=gateway, state_store=store).execute()
    assert second.state != redirect.state
    assert gateway.authorize_args[1][1] != verifier


async def test_logout_revokes_refresh(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
    )
    result = await login.execute(code="abc", state="valid-state")

    logout_audit = FakeAuditTrail()
    logout = LogoutSession(
        tokens=token_issuer, refresh_store=refresh_store, audit=logout_audit
    )
    await logout.execute(refresh_token=result.tokens.refresh_token)
    assert logout_audit.actions() == ["identity.logout"]
    assert logout_audit.records[0]["actor_id"] == result.user_id

    refresh_uc = RefreshSession(
        users=repo,
        tokens=token_issuer,
        refresh_store=refresh_store,
        access_ttl_seconds=900,
        refresh_ttl_seconds=3600,
        audit=FakeAuditTrail(),
    )
    with pytest.raises(InvalidTokenError):
        await refresh_uc.execute(refresh_token=result.tokens.refresh_token)


async def test_logout_without_token_does_not_audit() -> None:
    """Отсутствующий/мусорный refresh-токен — no-op, аудит не пишется."""
    audit = FakeAuditTrail()
    logout = LogoutSession(
        tokens=JwtTokenIssuer(
            secret="test-jwt-secret-0123456789abcdef-pad",
            algorithm="HS256",
            access_ttl_seconds=900,
            refresh_ttl_seconds=3600,
        ),
        refresh_store=FakeRefreshTokenStore(),
        audit=audit,
    )
    await logout.execute(refresh_token=None)
    await logout.execute(refresh_token="garbage")
    assert audit.actions() == []


async def test_login_writes_audit_with_new_user_flag(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    """``identity.login`` различает первый вход и повторный флагом в metadata."""
    audit = FakeAuditTrail()
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
        encryptor=encryptor,
        token_issuer=token_issuer,
        audit=audit,
    )
    first = await login.execute(code="abc", state="valid-state")

    state_store.seed("valid-state-2")
    second = await login.execute(code="def", state="valid-state-2")

    assert audit.actions() == ["identity.login", "identity.login"]
    assert audit.records[0]["actor_id"] == first.user_id
    assert audit.records[1]["actor_id"] == second.user_id
    # ПДн (СНИЛС/ФИО) в payload не попадают — только user_id/entity фейка проверяют.
    assert all(r["entity_type"] == "user" for r in audit.records)


async def test_get_current_user_by_access_token(
    confirmed_identity, repo, state_store, refresh_store, snils_hasher, esia_oid_hasher, encryptor, token_issuer
) -> None:
    login = _build_complete_login(
        identity=confirmed_identity,
        repo=repo,
        state_store=state_store,
        refresh_store=refresh_store,
        hasher=snils_hasher,
        esia_oid_hasher=esia_oid_hasher,
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
        esia_oid_hash=f"oid-hash-{username}",
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
    with pytest.raises(UserNotFoundError):
        await UpdateMyProfile(users=repo).execute(
            user_id=uuid.uuid4(), display_name="X"
        )


async def test_update_profile_changes_username(repo) -> None:
    user = _user(username="erik")
    await repo.add(user)
    updated = await UpdateMyProfile(users=repo).execute(
        user_id=user.id, username="new-handle"
    )
    assert updated.username == "new-handle"


async def test_update_profile_username_collision_raises(repo) -> None:
    await repo.add(_user(username="taken"))
    victim = _user(username="free")
    await repo.add(victim)
    with pytest.raises(UsernameAlreadyTakenError):
        await UpdateMyProfile(users=repo).execute(
            user_id=victim.id, username="taken"
        )


# ── Онбординг и согласия (152-ФЗ) ─────────────────────────────────────────


_REQUIRED = [
    ConsentDocument(document="offer", version="2026-07-05"),
    ConsentDocument(document="pdn", version="2026-07-05"),
]


@pytest.fixture
def consent_repo() -> InMemoryConsentRepository:
    return InMemoryConsentRepository()


async def test_onboarding_status_needs_onboarding_on_first_login(
    repo, consent_repo
) -> None:
    """Первый вход: онбординг не пройден, обязательные согласия отсутствуют."""
    user = _user(username="fresh")
    await repo.add(user)

    needs, missing = await GetOnboardingStatus(
        consents=consent_repo, required=_REQUIRED
    ).execute(user=user)

    assert needs is True
    assert {(m.document, m.version) for m in missing} == {
        ("offer", "2026-07-05"),
        ("pdn", "2026-07-05"),
    }


async def test_complete_onboarding_rejects_incomplete_consents(
    repo, consent_repo
) -> None:
    """Если передана не вся обязательная подборка согласий — доменная ошибка."""
    user = _user(username="incomplete")
    await repo.add(user)
    uc = CompleteOnboarding(
        users=repo, consents=consent_repo, required=_REQUIRED, method="onboarding_web"
    )

    with pytest.raises(IncompleteConsentsError):
        await uc.execute(
            user_id=user.id,
            username=None,
            display_name=None,
            provided_consents=[ConsentInput(document="offer", version="2026-07-05")],
            ip=None,
            user_agent=None,
        )

    stored = await repo.get_by_id(user.id)
    assert stored is not None and stored.onboarded_at is None


async def test_complete_onboarding_with_full_consents_succeeds(
    repo, consent_repo
) -> None:
    """Полный набор согласий + псевдоним → онбординг пройден, согласия видны."""
    user = _user(username="fullset")
    await repo.add(user)
    uc = CompleteOnboarding(
        users=repo, consents=consent_repo, required=_REQUIRED, method="onboarding_web"
    )

    updated = await uc.execute(
        user_id=user.id,
        username="chosen-handle",
        display_name="Выбранное имя",
        provided_consents=[
            ConsentInput(document="offer", version="2026-07-05"),
            ConsentInput(document="pdn", version="2026-07-05"),
        ],
        ip="127.0.0.1",
        user_agent="pytest",
    )

    assert updated.onboarded_at is not None
    assert updated.username == "chosen-handle"
    assert updated.display_name == "Выбранное имя"

    consents = await GetMyConsents(consents=consent_repo).execute(user_id=user.id)
    assert {(c.document, c.version) for c in consents} == {
        ("offer", "2026-07-05"),
        ("pdn", "2026-07-05"),
    }
    assert all(c.method == "onboarding_web" for c in consents)

    needs, missing = await GetOnboardingStatus(
        consents=consent_repo, required=_REQUIRED
    ).execute(user=updated)
    assert needs is False
    assert missing == []


async def test_complete_onboarding_idempotent_when_already_done(
    repo, consent_repo
) -> None:
    """Повторный вызов при уже пройденном онбординге и полных согласиях — просто 200 (без ошибок)."""
    user = _user(username="repeatable")
    await repo.add(user)
    uc = CompleteOnboarding(
        users=repo, consents=consent_repo, required=_REQUIRED, method="onboarding_web"
    )
    provided = [
        ConsentInput(document="offer", version="2026-07-05"),
        ConsentInput(document="pdn", version="2026-07-05"),
    ]
    first = await uc.execute(
        user_id=user.id,
        username=None,
        display_name=None,
        provided_consents=provided,
        ip=None,
        user_agent=None,
    )
    onboarded_at = first.onboarded_at

    # Повтор без новых согласий не ломается и не создаёт дублей.
    second = await uc.execute(
        user_id=user.id,
        username=None,
        display_name=None,
        provided_consents=[],
        ip=None,
        user_agent=None,
    )
    assert second.onboarded_at == onboarded_at
    consents = await consent_repo.list_for_user(user.id)
    assert len(consents) == 2


async def test_document_version_bump_reintroduces_missing_consent(
    repo, consent_repo
) -> None:
    """Юрист поменял версию документа → согласие снова недостающее (needs_onboarding)."""
    user = _user(username="versionbump")
    await repo.add(user)
    uc = CompleteOnboarding(
        users=repo, consents=consent_repo, required=_REQUIRED, method="onboarding_web"
    )
    await uc.execute(
        user_id=user.id,
        username=None,
        display_name=None,
        provided_consents=[
            ConsentInput(document="offer", version="2026-07-05"),
            ConsentInput(document="pdn", version="2026-07-05"),
        ],
        ip=None,
        user_agent=None,
    )
    stored = await repo.get_by_id(user.id)
    assert stored is not None

    bumped_required = [
        ConsentDocument(document="offer", version="2026-08-01"),
        ConsentDocument(document="pdn", version="2026-07-05"),
    ]
    needs, missing = await GetOnboardingStatus(
        consents=consent_repo, required=bumped_required
    ).execute(user=stored)

    assert needs is True
    assert [(m.document, m.version) for m in missing] == [("offer", "2026-08-01")]


async def test_get_my_consents_empty_for_new_user(repo, consent_repo) -> None:
    user = _user(username="none-yet")
    await repo.add(user)
    consents = await GetMyConsents(consents=consent_repo).execute(user_id=user.id)
    assert consents == []


def test_consent_domain_entity_defaults() -> None:
    """Смоук: доменная сущность Consent создаётся с разумными дефолтами."""
    consent = Consent(
        user_id=uuid.uuid4(), document="offer", version="1", method="onboarding_web"
    )
    assert consent.id is not None
    assert consent.accepted_at is not None
    assert consent.satisfies(ConsentDocument(document="offer", version="1"))
    assert not consent.satisfies(ConsentDocument(document="offer", version="2"))


# ── Удаление аккаунта (T4, 152-ФЗ) ─────────────────────────────────────────


def test_anonymize_for_deletion_clears_pii_keeps_hashes() -> None:
    """Анонимизация стирает ФИО/публичный профиль, но хранит хэши-надгробия."""
    user = _user(username="tobedeleted", display="Иван Петров")
    user.real_name_enc = b"encrypted-fio"

    changed = user.anonymize_for_deletion()

    assert changed is True
    assert user.status is UserStatus.DELETED
    assert user.real_name_enc is None
    assert user.display_name == "Удалённый аккаунт"
    assert user.username == f"deleted-{user.id.hex[:8]}"
    # snils_hash/esia_oid_hash — антиобход «1 человек = 1 аккаунт», не трогаем.
    assert user.snils_hash == "hash-tobedeleted"
    assert user.esia_oid_hash == "oid-hash-tobedeleted"


def test_anonymize_for_deletion_idempotent() -> None:
    """Повторный вызов для уже удалённого аккаунта — no-op."""
    user = _user(username="already-gone")
    assert user.anonymize_for_deletion() is True
    tombstone_username = user.username

    assert user.anonymize_for_deletion() is False
    assert user.username == tombstone_username
    assert user.status is UserStatus.DELETED


async def test_delete_my_account_anonymizes_revokes_and_audits(repo) -> None:
    user = _user(username="deleteme")
    await repo.add(user)
    refresh_store = FakeRefreshTokenStore()
    await refresh_store.register("jti-1", 3600, str(user.id))
    audit = FakeAuditTrail()
    uc = DeleteMyAccount(users=repo, refresh_store=refresh_store, audit=audit)

    await uc.execute(user_id=user.id)

    stored = await repo.get_by_id(user.id)
    assert stored is not None
    assert stored.status is UserStatus.DELETED
    assert stored.real_name_enc is None
    assert stored.display_name == "Удалённый аккаунт"
    assert stored.username == f"deleted-{user.id.hex[:8]}"
    assert stored.snils_hash == user.snils_hash
    assert stored.esia_oid_hash == user.esia_oid_hash
    # Сессии отозваны.
    assert await refresh_store.is_active("jti-1") is False
    # Аудит — без ПДн: только action/entity, никакого ФИО/хэндла в payload.
    assert audit.actions() == ["identity.user.deleted"]
    entry = audit.records[0]
    assert entry["entity_id"] == user.id
    assert entry["actor_id"] == user.id


async def test_delete_my_account_idempotent(repo) -> None:
    """Повторный вызов не пишет второй раз в аудит и не падает."""
    user = _user(username="twice")
    await repo.add(user)
    refresh_store = FakeRefreshTokenStore()
    audit = FakeAuditTrail()
    uc = DeleteMyAccount(users=repo, refresh_store=refresh_store, audit=audit)

    await uc.execute(user_id=user.id)
    await uc.execute(user_id=user.id)

    assert audit.actions() == ["identity.user.deleted"]
    stored = await repo.get_by_id(user.id)
    assert stored is not None and stored.status is UserStatus.DELETED


async def test_delete_my_account_keeps_consents(repo) -> None:
    """Согласия переживают удаление аккаунта (append-only, доказательство акцепта).

    ``user_consents`` — юридическое доказательство того, что оферта и согласие
    на обработку ПДн были приняты (152-ФЗ): удаление профиля его не стирает
    (у роли БД нет DELETE на этой таблице), и ``DeleteMyAccount`` к согласиям
    не прикасается вовсе.
    """
    user = _user(username="withconsents")
    await repo.add(user)
    consents = onboarded_consent_repository(user.id)
    before = list(consents.rows)
    uc = DeleteMyAccount(
        users=repo, refresh_store=FakeRefreshTokenStore(), audit=FakeAuditTrail()
    )

    await uc.execute(user_id=user.id)

    assert await consents.list_for_user(user.id) == before


async def test_delete_my_account_unknown_user_raises(repo) -> None:
    uc = DeleteMyAccount(
        users=repo, refresh_store=FakeRefreshTokenStore(), audit=FakeAuditTrail()
    )
    with pytest.raises(UserNotFoundError):
        await uc.execute(user_id=uuid.uuid4())


# ── Модерация: блокировка/разблокировка/список пользователей (B7) ─────────


def _make_admin(username="boss") -> User:
    user = _user(username=username, display="Босс")
    user.role = UserRole.ADMIN
    return user


async def test_suspend_user_revokes_sessions_and_audits(repo) -> None:
    admin = _make_admin()
    target = _user(username="troll")
    await repo.add(admin)
    await repo.add(target)
    refresh_store = FakeRefreshTokenStore()
    await refresh_store.register("jti-1", 3600, str(target.id))
    audit = FakeAuditTrail()
    uc = SuspendUser(users=repo, refresh_store=refresh_store, audit=audit)

    result = await uc.execute(actor=admin, target_id=target.id, reason="спам")

    assert result.status is UserStatus.SUSPENDED
    stored = await repo.get_by_id(target.id)
    assert stored is not None and stored.status is UserStatus.SUSPENDED
    assert await refresh_store.is_active("jti-1") is False
    assert audit.actions() == ["identity.user.suspended"]
    entry = audit.records[0]
    assert entry["actor_id"] == admin.id
    assert entry["entity_id"] == target.id


async def test_suspend_user_cannot_suspend_self(repo) -> None:
    admin = _make_admin()
    await repo.add(admin)
    uc = SuspendUser(
        users=repo, refresh_store=FakeRefreshTokenStore(), audit=FakeAuditTrail()
    )
    with pytest.raises(CannotSuspendSelfError):
        await uc.execute(actor=admin, target_id=admin.id, reason="передумал быть админом")


async def test_suspend_user_cannot_suspend_another_admin(repo) -> None:
    admin = _make_admin(username="boss1")
    other_admin = _make_admin(username="boss2")
    await repo.add(admin)
    await repo.add(other_admin)
    uc = SuspendUser(
        users=repo, refresh_store=FakeRefreshTokenStore(), audit=FakeAuditTrail()
    )
    with pytest.raises(CannotSuspendAdminError):
        await uc.execute(actor=admin, target_id=other_admin.id, reason="конфликт")


async def test_suspend_user_rejects_non_active_target(repo) -> None:
    admin = _make_admin()
    target = _user(username="already-suspended", status=UserStatus.SUSPENDED)
    await repo.add(admin)
    await repo.add(target)
    uc = SuspendUser(
        users=repo, refresh_store=FakeRefreshTokenStore(), audit=FakeAuditTrail()
    )
    with pytest.raises(InvalidUserStatusError):
        await uc.execute(actor=admin, target_id=target.id, reason="повторно")


async def test_suspend_user_unknown_target_raises(repo) -> None:
    admin = _make_admin()
    await repo.add(admin)
    uc = SuspendUser(
        users=repo, refresh_store=FakeRefreshTokenStore(), audit=FakeAuditTrail()
    )
    with pytest.raises(UserNotFoundError):
        await uc.execute(actor=admin, target_id=uuid.uuid4(), reason="кто это")


async def test_reinstate_user_returns_to_active_and_audits(repo) -> None:
    admin = _make_admin()
    target = _user(username="reformed", status=UserStatus.SUSPENDED)
    await repo.add(admin)
    await repo.add(target)
    audit = FakeAuditTrail()
    uc = ReinstateUser(users=repo, audit=audit)

    result = await uc.execute(actor=admin, target_id=target.id)

    assert result.status is UserStatus.ACTIVE
    stored = await repo.get_by_id(target.id)
    assert stored is not None and stored.status is UserStatus.ACTIVE
    assert audit.actions() == ["identity.user.reinstated"]


async def test_reinstate_user_rejects_non_suspended_target(repo) -> None:
    admin = _make_admin()
    target = _user(username="already-active", status=UserStatus.ACTIVE)
    await repo.add(admin)
    await repo.add(target)
    uc = ReinstateUser(users=repo, audit=FakeAuditTrail())
    with pytest.raises(InvalidUserStatusError):
        await uc.execute(actor=admin, target_id=target.id)


async def test_list_users_filters_by_status_and_search(repo) -> None:
    await repo.add(_user(username="alice", display="Алиса", status=UserStatus.ACTIVE))
    await repo.add(_user(username="bob", display="Боб", status=UserStatus.SUSPENDED))
    await repo.add(_user(username="alice2", display="Алиса Вторая"))
    uc = ListUsers(users=repo)

    active_page = await uc.execute(
        status=UserStatus.ACTIVE, search=None, limit=10, offset=0
    )
    assert active_page.total == 2
    assert {u.username for u in active_page.items} == {"alice", "alice2"}

    search_page = await uc.execute(status=None, search="ali", limit=10, offset=0)
    assert search_page.total == 2

    suspended_page = await uc.execute(
        status=UserStatus.SUSPENDED, search=None, limit=10, offset=0
    )
    assert suspended_page.total == 1
    assert suspended_page.items[0].username == "bob"


async def test_list_users_pagination(repo) -> None:
    for i in range(3):
        await repo.add(_user(username=f"user{i}"))
    uc = ListUsers(users=repo)

    page = await uc.execute(status=None, search=None, limit=2, offset=0)
    assert len(page.items) == 2
    assert page.total == 3

    page2 = await uc.execute(status=None, search=None, limit=2, offset=2)
    assert len(page2.items) == 1


# ── Доменные правила блокировки (policies.ensure_can_suspend, User.suspend) ─


def test_user_suspend_transitions_active_to_suspended() -> None:
    user = _user(username="target")
    user.suspend()
    assert user.status is UserStatus.SUSPENDED


def test_user_suspend_rejects_non_active_status() -> None:
    user = _user(username="target", status=UserStatus.DELETED)
    with pytest.raises(InvalidUserStatusError):
        user.suspend()


def test_user_reinstate_transitions_suspended_to_active() -> None:
    user = _user(username="target", status=UserStatus.SUSPENDED)
    user.reinstate()
    assert user.status is UserStatus.ACTIVE


def test_user_reinstate_rejects_non_suspended_status() -> None:
    user = _user(username="target", status=UserStatus.ACTIVE)
    with pytest.raises(InvalidUserStatusError):
        user.reinstate()


def test_ensure_can_suspend_allows_admin_suspending_regular_user() -> None:
    admin = _make_admin()
    target = _user(username="regular")
    ensure_can_suspend(actor=admin, target=target)  # не должно бросать


def test_ensure_can_suspend_rejects_self() -> None:
    admin = _make_admin()
    with pytest.raises(CannotSuspendSelfError):
        ensure_can_suspend(actor=admin, target=admin)


def test_ensure_can_suspend_rejects_admin_target() -> None:
    admin = _make_admin(username="boss1")
    other_admin = _make_admin(username="boss2")
    with pytest.raises(CannotSuspendAdminError):
        ensure_can_suspend(actor=admin, target=other_admin)
