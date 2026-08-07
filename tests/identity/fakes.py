"""In-memory фейки портов identity для изолированного тестирования.

Фейки реализуют те же протоколы, что и продакшн-адаптеры, но без I/O —
это позволяет юнит-тестировать use-cases и интеграционно гонять эндпоинты
без Postgres, Redis и сети к ЕСИА.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.modules.identity.adapters.id_token import pkce_code_challenge
from app.modules.identity.application.dto import OidcFlowState
from app.modules.identity.domain.consent import Consent
from app.modules.identity.domain.entities import User, UserStatus
from app.modules.identity.domain.value_objects import EsiaIdentity, EsiaTokens
from app.modules.identity.ports.repositories import (
    EmailAlreadyExistsError,
    SnilsAlreadyExistsError,
    UsernameTakenError,
)
from app.shared.audit.domain.entities import AuditActorType, AuditEntry
from app.shared.mail.domain.message import EmailMessage


class InMemoryUserRepository:
    """Хранилище пользователей в памяти с эмуляцией UNIQUE-ограничений."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, User] = {}

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._clone(self._by_id.get(user_id))

    async def get_by_snils_hash(self, snils_hash: str) -> User | None:
        for user in self._by_id.values():
            if user.snils_hash == snils_hash:
                return self._clone(user)
        return None

    async def get_by_esia_oid_hash(self, esia_oid_hash: str) -> User | None:
        for user in self._by_id.values():
            if user.esia_oid_hash == esia_oid_hash:
                return self._clone(user)
        return None

    async def get_by_email(self, email: str) -> User | None:
        for user in self._by_id.values():
            if user.email is not None and user.email.lower() == email.lower():
                return self._clone(user)
        return None

    async def get_by_username(self, username: str) -> User | None:
        for user in self._by_id.values():
            if user.username.lower() == username.lower():
                return self._clone(user)
        return None

    async def username_exists(self, username: str) -> bool:
        return any(u.username.lower() == username.lower() for u in self._by_id.values())

    async def add(self, user: User) -> User:
        for existing in self._by_id.values():
            # Частичный UNIQUE: NULL-и не конфликтуют между собой.
            if user.snils_hash is not None and existing.snils_hash == user.snils_hash:
                raise SnilsAlreadyExistsError(user.snils_hash)
            if user.email is not None and _same_email(existing.email, user.email):
                raise EmailAlreadyExistsError(user.email)
            if existing.username.lower() == user.username.lower():
                raise UsernameTakenError(user.username)
        self._by_id[user.id] = self._clone(user)
        return self._clone(user)

    async def update(self, user: User) -> User:
        for existing in self._by_id.values():
            if existing.id == user.id:
                continue
            if existing.username.lower() == user.username.lower():
                raise UsernameTakenError(user.username)
            if user.email is not None and _same_email(existing.email, user.email):
                raise EmailAlreadyExistsError(user.email)
        self._by_id[user.id] = self._clone(user)
        return self._clone(user)

    async def list_page(
        self,
        *,
        status: UserStatus | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[User], int]:
        items = list(self._by_id.values())
        if status is not None:
            items = [u for u in items if u.status is status]
        if search:
            needle = search.lower()
            items = [
                u
                for u in items
                if needle in u.username.lower() or needle in u.display_name.lower()
            ]
        items.sort(key=lambda u: u.created_at, reverse=True)
        total = len(items)
        page = items[offset : offset + limit]
        cloned = [self._clone(u) for u in page]
        return [u for u in cloned if u is not None], total

    @staticmethod
    def _clone(user: User | None) -> User | None:
        """Возвращает копию, чтобы внешние мутации не текли в хранилище."""
        if user is None:
            return None
        return User(
            id=user.id,
            esia_oid_hash=user.esia_oid_hash,
            snils_hash=user.snils_hash,
            email=user.email,
            identity_verified=user.identity_verified,
            username=user.username,
            display_name=user.display_name,
            real_name_enc=user.real_name_enc,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
            onboarded_at=user.onboarded_at,
        )


def _same_email(left: str | None, right: str | None) -> bool:
    """Сравнение адресов как в БД (citext — регистронезависимо)."""
    if left is None or right is None:
        return False
    return left.lower() == right.lower()


class FakeMagicLinkStore:
    """Ссылки входа в памяти: одноразовость и счётчик писем на адрес.

    TTL не эмулируется по времени — тестам достаточно того, что запись можно
    выбросить вручную (``expire``); за реальное истечение отвечает Redis.
    """

    def __init__(self) -> None:
        self._links: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        # (token_hash, email, ttl) каждой выданной ссылки — для ассертов о TTL.
        self.saved: list[tuple[str, str, int]] = []

    async def save(self, token_hash: str, email: str, ttl_seconds: int) -> None:
        self._links[token_hash] = email
        self.saved.append((token_hash, email, ttl_seconds))

    async def consume(self, token_hash: str) -> str | None:
        return self._links.pop(token_hash, None)

    async def count_request(self, quota_key: str, window_seconds: int) -> int:
        self.counters[quota_key] = self.counters.get(quota_key, 0) + 1
        return self.counters[quota_key]

    def expire(self, token_hash: str) -> None:
        """Тестовый помощник: «протухание» ссылки до её использования."""
        self._links.pop(token_hash, None)


class FakeEmailSender:
    """Перехватывает письма вместо отправки; умеет притворяться сломанным SMTP."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[EmailMessage] = []
        self.fail = fail

    async def send(self, message: EmailMessage) -> None:
        if self.fail:
            raise RuntimeError("SMTP недоступен")
        self.sent.append(message)

    def last_link(self) -> str:
        """Ссылка входа из последнего письма (как её увидит пользователь)."""
        body = self.sent[-1].text_body
        for chunk in body.split():
            if "token=" in chunk:
                return chunk
        raise AssertionError("В письме нет ссылки со ссылочным токеном")

    def last_token(self) -> str:
        """Одноразовый токен из последнего письма."""
        return self.last_link().split("token=", 1)[1]


class InMemoryConsentRepository:
    """Хранилище согласий в памяти с эмуляцией ``ON CONFLICT DO NOTHING``."""

    def __init__(self) -> None:
        # Публичный список — тесты сверяют по нему записанные поля (ip и др.).
        self.rows: list[Consent] = []

    async def list_for_user(self, user_id: uuid.UUID) -> list[Consent]:
        return [c for c in self.rows if c.user_id == user_id]

    async def add_many(self, consents: list[Consent]) -> None:
        existing = {(c.user_id, c.document, c.version) for c in self.rows}
        for consent in consents:
            key = (consent.user_id, consent.document, consent.version)
            if key not in existing:
                self.rows.append(consent)
                existing.add(key)


def onboarded_consent_repository(user_id: uuid.UUID) -> InMemoryConsentRepository:
    """Согласия пользователя, покрывающие ВСЕ обязательные документы (текущие версии).

    Нужен интеграционным тестам соседних доменов: участие в конкурсе закрыто
    гардом ``require_onboarded_user`` (PRD §7), и «обычный участник» в фикстуре
    должен выглядеть как прошедший онбординг. Реестр берётся из тех же
    настроек, что и у продакшн-кода — тест не дублирует таблицу истины.
    """
    repo = InMemoryConsentRepository()
    repo.rows.extend(
        Consent(
            user_id=user_id,
            document=document,
            version=version,
            method="onboarding_web",
        )
        for document, version in get_settings().consents.required_documents.items()
    )
    return repo


class FakeEsiaGateway:
    """Шлюз ЕСИА, возвращающий заранее заданную личность.

    Запоминает PKCE/nonce-параметры обоих шагов, чтобы тест мог проверить, что
    ``code_verifier``/``nonce`` из стора реально доехали до обмена кода.
    """

    def __init__(self, identity: EsiaIdentity) -> None:
        self.identity = identity
        self.build_calls: list[str] = []
        # (state, code_verifier, nonce) шага авторизации.
        self.authorize_args: list[tuple[str, str, str]] = []
        # (code, code_verifier, nonce) шага обмена кода.
        self.exchange_args: list[tuple[str, str, str]] = []

    def build_authorization_url(
        self, *, state: str, code_verifier: str, nonce: str
    ) -> str:
        self.build_calls.append(state)
        self.authorize_args.append((state, code_verifier, nonce))
        # Как и боевой адаптер, наружу отдаём только S256-производную секрета.
        return (
            f"https://esia.example/authorize?state={state}"
            f"&code_challenge={pkce_code_challenge(code_verifier)}"
            f"&code_challenge_method=S256&nonce={nonce}"
        )

    async def exchange_code(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> EsiaTokens:
        self.exchange_args.append((code, code_verifier, nonce))
        return EsiaTokens(access_token=f"access-for-{code}", id_token="id")

    async def fetch_identity(self, tokens: EsiaTokens) -> EsiaIdentity:
        return self.identity


class FakeStateStore:
    """Выпущенные одноразовые state вместе с секретами потока (PKCE/nonce)."""

    def __init__(self) -> None:
        self._states: dict[str, OidcFlowState] = {}

    async def save(self, state: str, flow: OidcFlowState, ttl_seconds: int) -> None:
        self._states[state] = flow

    async def consume(self, state: str) -> OidcFlowState | None:
        return self._states.pop(state, None)

    def seed(self, state: str, flow: OidcFlowState | None = None) -> None:
        """Тестовый помощник: заранее положить валидный state с секретами."""
        self._states[state] = flow or OidcFlowState(
            code_verifier="seeded-verifier", nonce="seeded-nonce"
        )


class FakeRefreshTokenStore:
    """Allow-list refresh-jti с детектом повторного использования (семейство = user_id)."""

    def __init__(self) -> None:
        self._active: dict[str, str] = {}  # jti -> user_id
        self._rotated: set[str] = set()
        self._family: dict[str, set[str]] = {}  # user_id -> {jti}

    async def register(self, jti: str, ttl_seconds: int, user_id: str) -> None:
        self._active[jti] = user_id
        self._family.setdefault(user_id, set()).add(jti)

    async def is_active(self, jti: str) -> bool:
        return jti in self._active

    async def revoke(self, jti: str) -> None:
        self._active.pop(jti, None)

    async def mark_rotated(self, jti: str, ttl_seconds: int) -> None:
        self._rotated.add(jti)

    async def was_rotated(self, jti: str) -> bool:
        return jti in self._rotated

    async def revoke_all_for_user(self, user_id: str) -> None:
        for jti in self._family.pop(user_id, set()):
            self._active.pop(jti, None)


class FakeAuditTrail:
    """Запоминает записи аудита (без реальной хеш-цепочки)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_type: AuditActorType,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        self.records.append(
            {
                "actor_id": actor_id,
                "actor_type": actor_type,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
            }
        )
        return AuditEntry(
            occurred_at=datetime(2026, 1, 1),  # noqa: DTZ001 — фейк, время не важно
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            hash="fake",
        )

    def actions(self) -> list[str]:
        """Список зафиксированных action'ов (для ассертов)."""
        return [r["action"] for r in self.records]
