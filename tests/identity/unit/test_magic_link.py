"""Юнит-тесты входа по одноразовой ссылке (magic link).

Проверяют то, ради чего этот поток вообще сделан безопасным: токен наружу
уходит, а на сервере лежит только его хэш; ссылка одноразовая; чужой ящик
нельзя завалить письмами; сбой SMTP и несуществующий адрес неотличимы снаружи
(анти-энумерация).
"""

from __future__ import annotations

import pytest

from app.modules.identity.application.login import SessionIssuer
from app.modules.identity.application.use_cases import (
    CompleteEmailLogin,
    RequestEmailLogin,
)
from app.modules.identity.domain.entities import User, UserRole, UserStatus
from app.modules.identity.domain.errors import (
    AccountDeletedError,
    AccountSuspendedError,
    InvalidMagicLinkError,
)
from app.modules.identity.domain.magic_link import (
    MAGIC_LINK_TTL_SECONDS,
    MAX_LETTERS_PER_EMAIL,
    generate_magic_link_token,
    hash_magic_link_token,
)
from tests.identity.fakes import (
    FakeAuditTrail,
    FakeEmailSender,
    FakeMagicLinkStore,
    FakeRefreshTokenStore,
    InMemoryUserRepository,
)

LINK_BASE = "https://veraks.test"


@pytest.fixture
def links() -> FakeMagicLinkStore:
    return FakeMagicLinkStore()


@pytest.fixture
def sender() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
def repo() -> InMemoryUserRepository:
    return InMemoryUserRepository()


def _request_uc(links: FakeMagicLinkStore, sender: FakeEmailSender) -> RequestEmailLogin:
    return RequestEmailLogin(links=links, sender=sender, link_base_url=LINK_BASE)


def _complete_uc(
    links: FakeMagicLinkStore,
    repo: InMemoryUserRepository,
    token_issuer,
    *,
    audit: FakeAuditTrail | None = None,
) -> CompleteEmailLogin:
    return CompleteEmailLogin(
        links=links,
        users=repo,
        sessions=SessionIssuer(
            tokens=token_issuer,
            refresh_store=FakeRefreshTokenStore(),
            access_ttl_seconds=900,
            refresh_ttl_seconds=3600,
        ),
        audit=audit if audit is not None else FakeAuditTrail(),
    )


# ── Чистые функции токена ─────────────────────────────────────────────────


def test_token_is_unique_and_hash_is_deterministic() -> None:
    first, second = generate_magic_link_token(), generate_magic_link_token()
    assert first != second
    assert hash_magic_link_token(first) == hash_magic_link_token(first)
    assert hash_magic_link_token(first) != hash_magic_link_token(second)
    # sha256 в hex — 64 символа; сам токен из хэша не восстановить.
    assert len(hash_magic_link_token(first)) == 64
    assert first not in hash_magic_link_token(first)


# ── Запрос ссылки ─────────────────────────────────────────────────────────


async def test_request_stores_hash_not_token(links, sender) -> None:
    """В хранилище уходит ХЭШ токена; сам токен есть только в письме."""
    await _request_uc(links, sender).execute(email="Ivan@Example.RU")

    token = sender.last_token()
    stored_hash, stored_email, ttl = links.saved[-1]
    assert stored_hash == hash_magic_link_token(token)
    assert stored_hash != token
    # Адрес нормализован (trim + lower) — иначе один ящик дал бы два аккаунта.
    assert stored_email == "ivan@example.ru"
    assert ttl == MAGIC_LINK_TTL_SECONDS


async def test_request_letter_contains_link_and_expiry_notice(links, sender) -> None:
    await _request_uc(links, sender).execute(email=" user@example.com ")

    letter = sender.sent[-1]
    assert letter.to == "user@example.com"
    assert sender.last_link().startswith(f"{LINK_BASE}/auth/email/callback?token=")
    assert "15 минут" in letter.text_body
    assert "не запрашивали вход" in letter.text_body
    # Без внешних ресурсов: ни картинок, ни трекинг-пикселя, ни подгрузок —
    # единственная внешняя ссылка в письме это сама ссылка входа.
    assert "<img" not in letter.html_body
    assert "src=" not in letter.html_body
    assert letter.html_body.count("http") == letter.html_body.count(LINK_BASE)


async def test_request_stops_sending_after_limit_but_stays_silent(
    links, sender
) -> None:
    """Свыше N писем в час на адрес — письма нет, но и ошибки наружу нет."""
    uc = _request_uc(links, sender)
    for _ in range(MAX_LETTERS_PER_EMAIL):
        await uc.execute(email="victim@example.com")
    assert len(sender.sent) == MAX_LETTERS_PER_EMAIL

    await uc.execute(email="victim@example.com")  # (N+1)-е — не уходит

    assert len(sender.sent) == MAX_LETTERS_PER_EMAIL
    # Лишняя ссылка даже не выпускалась: нечего было бы перехватывать.
    assert len(links.saved) == MAX_LETTERS_PER_EMAIL


async def test_request_limit_counted_per_address(links, sender) -> None:
    """Лимит на один адрес не мешает писать на другой."""
    uc = _request_uc(links, sender)
    for _ in range(MAX_LETTERS_PER_EMAIL + 1):
        await uc.execute(email="one@example.com")

    await uc.execute(email="two@example.com")

    assert sender.sent[-1].to == "two@example.com"


async def test_request_survives_broken_smtp(links) -> None:
    """Сбой отправки не поднимается наверх: иначе 202/500 выдали бы адреса."""
    sender = FakeEmailSender(fail=True)

    await _request_uc(links, sender).execute(email="user@example.com")

    assert sender.sent == []
    assert links.saved  # ссылка выпущена — оператор достанет её из логов


async def test_request_does_not_create_user(links, sender, repo) -> None:
    """Аккаунт заводится переходом по ссылке, а не запросом письма."""
    await _request_uc(links, sender).execute(email="stranger@example.com")

    assert await repo.get_by_email("stranger@example.com") is None


# ── Вход по ссылке ────────────────────────────────────────────────────────


async def test_callback_creates_account_without_snils(
    links, sender, repo, token_issuer
) -> None:
    await _request_uc(links, sender).execute(email="new@example.com")

    result = await _complete_uc(links, repo, token_issuer).execute(
        token=sender.last_token()
    )

    assert result.is_new_user is True
    created = await repo.get_by_id(result.user_id)
    assert created is not None
    assert created.email == "new@example.com"
    assert created.snils_hash is None
    assert created.esia_oid_hash is None
    # Ссылка на почту личность не подтверждает (PRD §7).
    assert created.identity_verified is False
    # Согласия ещё не приняты — впереди онбординг.
    assert created.onboarded_at is None
    assert created.username.startswith("predictor-")
    assert created.display_name == created.username


async def test_link_works_exactly_once(links, sender, repo, token_issuer) -> None:
    await _request_uc(links, sender).execute(email="user@example.com")
    token = sender.last_token()
    uc = _complete_uc(links, repo, token_issuer)

    await uc.execute(token=token)

    with pytest.raises(InvalidMagicLinkError):
        await uc.execute(token=token)


async def test_expired_link_rejected(links, sender, repo, token_issuer) -> None:
    """Истёкшая запись (TTL в Redis) неотличима от неизвестного токена."""
    await _request_uc(links, sender).execute(email="user@example.com")
    token = sender.last_token()
    links.expire(hash_magic_link_token(token))

    with pytest.raises(InvalidMagicLinkError):
        await _complete_uc(links, repo, token_issuer).execute(token=token)


async def test_unknown_token_rejected(links, repo, token_issuer) -> None:
    with pytest.raises(InvalidMagicLinkError):
        await _complete_uc(links, repo, token_issuer).execute(token="never-issued")


async def test_second_login_same_email_reuses_account(
    links, sender, repo, token_issuer
) -> None:
    uc = _complete_uc(links, repo, token_issuer)
    request = _request_uc(links, sender)

    await request.execute(email="user@example.com")
    first = await uc.execute(token=sender.last_token())
    await request.execute(email="USER@example.com")  # другой регистр — тот же ящик
    second = await uc.execute(token=sender.last_token())

    assert second.is_new_user is False
    assert second.user_id == first.user_id


async def test_race_on_email_enters_the_winner(
    links, sender, repo, token_issuer
) -> None:
    """Параллельная регистрация того же адреса: входим в победителя гонки.

    Эмулируем гонку так же, как в ЕСИА-потоке: аккаунт появляется ПОСЛЕ
    проверки «есть ли такой email», уже во время вставки.
    """
    await _request_uc(links, sender).execute(email="race@example.com")
    token = sender.last_token()

    winner = User.register_with_email(email="race@example.com", username="winner")
    original_get = repo.get_by_email
    calls = {"n": 0}

    async def get_by_email_then_insert_winner(email: str):
        calls["n"] += 1
        if calls["n"] == 1:
            await repo.add(winner)  # «параллельная» регистрация
            return None
        return await original_get(email)

    repo.get_by_email = get_by_email_then_insert_winner  # type: ignore[method-assign]

    result = await _complete_uc(links, repo, token_issuer).execute(token=token)

    assert result.is_new_user is False
    assert result.user_id == winner.id


async def test_username_race_reallocates_handle(
    links, sender, repo, token_issuer
) -> None:
    """Занятый в момент вставки хэндл переаллоцируется, вход не падает."""
    await _request_uc(links, sender).execute(email="user@example.com")
    original_add = repo.add
    calls = {"n": 0}

    async def add_once_conflicting(user: User) -> User:
        calls["n"] += 1
        if calls["n"] == 1:
            from app.modules.identity.ports.repositories import UsernameTakenError

            raise UsernameTakenError(user.username)
        return await original_add(user)

    repo.add = add_once_conflicting  # type: ignore[method-assign]

    result = await _complete_uc(links, repo, token_issuer).execute(
        token=sender.last_token()
    )

    created = await repo.get_by_id(result.user_id)
    assert created is not None
    # display_name не должен «отстать» от переаллоцированного хэндла.
    assert created.display_name == created.username


async def test_deleted_account_cannot_login_by_email(
    links, sender, repo, token_issuer
) -> None:
    """Удаление — надгробие и для email: заново зарегистрироваться нельзя."""
    await _request_uc(links, sender).execute(email="user@example.com")
    result = await _complete_uc(links, repo, token_issuer).execute(
        token=sender.last_token()
    )
    stored = await repo.get_by_id(result.user_id)
    assert stored is not None
    stored.anonymize_for_deletion()
    await repo.update(stored)

    await _request_uc(links, sender).execute(email="user@example.com")
    with pytest.raises(AccountDeletedError):
        await _complete_uc(links, repo, token_issuer).execute(
            token=sender.last_token()
        )


async def test_suspended_account_cannot_login_by_email(
    links, sender, repo, token_issuer
) -> None:
    await repo.add(
        User(
            username="blocked",
            display_name="blocked",
            real_name_enc=None,
            email="blocked@example.com",
            status=UserStatus.SUSPENDED,
        )
    )
    await _request_uc(links, sender).execute(email="blocked@example.com")

    with pytest.raises(AccountSuspendedError):
        await _complete_uc(links, repo, token_issuer).execute(
            token=sender.last_token()
        )


async def test_login_audit_records_method_without_pii(
    links, sender, repo, token_issuer
) -> None:
    """В аудите — способ входа и флаг новизны, но никакого адреса."""
    audit = FakeAuditTrail()
    await _request_uc(links, sender).execute(email="user@example.com")

    await _complete_uc(links, repo, token_issuer, audit=audit).execute(
        token=sender.last_token()
    )

    assert audit.actions() == ["identity.login"]
    assert "user@example.com" not in str(audit.records)


# ── Фабрика доменной сущности ─────────────────────────────────────────────


def test_register_with_email_has_no_state_identity() -> None:
    user = User.register_with_email(email="a@example.com", username="predictor-abc")

    assert user.identity_verified is False
    assert user.snils_hash is None
    assert user.esia_oid_hash is None
    assert user.real_name_enc is None
    assert user.role is UserRole.USER
    assert user.onboarded_at is None
