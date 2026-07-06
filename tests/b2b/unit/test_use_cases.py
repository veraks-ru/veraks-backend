"""Юнит-тесты use-cases b2b на in-memory фейках портов.

Покрывают: выдачу ключа с записью в аудит и БЕЗ проводки выручки (H-B2B,
M-AUDITGAP); идемпотентный отзыв с аудитом; fail-closed аутентификацию при
отказе квоты.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.b2b.application.use_cases import (
    AuthenticateApiKey,
    IssueApiKey,
    RevokeApiKey,
)
from app.modules.b2b.domain.errors import (
    ApiKeyNotFoundError,
    InvalidApiKeyError,
    QuotaExceededError,
)
from app.shared.audit.domain.entities import AuditActorType
from tests.b2b.fakes import (
    FakeApiKeyRepository,
    FakeAuditTrail,
    FakeKeyGenerator,
    FakeQuotaCounter,
)


def _issue_uc(repo, audit, *, default_quota: int = 1000) -> IssueApiKey:  # noqa: ANN001
    return IssueApiKey(
        keys=repo,
        generator=FakeKeyGenerator(),
        audit=audit,
        default_quota=default_quota,
    )


# ── IssueApiKey ──────────────────────────────────────────────────────────────


async def test_issue_records_audit_and_returns_secret() -> None:
    repo = FakeApiKeyRepository()
    audit = FakeAuditTrail()
    owner = uuid.uuid4()

    issued = await _issue_uc(repo, audit).execute(
        owner_user_id=owner, name="Аналитика"
    )

    assert issued.plaintext == "vk_testsecret000_abc"
    assert issued.key.key_prefix == "vk_testsecr"
    assert issued.key.daily_quota == 1000
    # Факт выдачи записан в аудит (M-AUDITGAP), выручка при этом НЕ проводится.
    assert audit.actions() == ["b2b.key.issued"]
    rec = audit.records[0]
    assert rec["actor_id"] == owner
    assert rec["actor_type"] is AuditActorType.ADMIN
    assert rec["entity_type"] == "api_key"
    assert rec["entity_id"] == issued.key.id
    assert rec["after"]["daily_quota"] == 1000


async def test_issue_honours_custom_quota() -> None:
    repo = FakeApiKeyRepository()
    issued = await _issue_uc(repo, FakeAuditTrail()).execute(
        owner_user_id=uuid.uuid4(), name="Лимит", daily_quota=42
    )
    assert issued.key.daily_quota == 42


async def test_issue_persists_key_retrievable_by_owner() -> None:
    repo = FakeApiKeyRepository()
    owner = uuid.uuid4()
    issued = await _issue_uc(repo, FakeAuditTrail()).execute(
        owner_user_id=owner, name="Ключ"
    )
    stored = await repo.list_for_owner(owner)
    assert [k.id for k in stored] == [issued.key.id]


# ── RevokeApiKey ─────────────────────────────────────────────────────────────


async def test_revoke_records_audit_and_is_idempotent() -> None:
    repo = FakeApiKeyRepository()
    owner = uuid.uuid4()
    issued = await _issue_uc(repo, FakeAuditTrail()).execute(
        owner_user_id=owner, name="Ключ"
    )

    audit = FakeAuditTrail()
    revoke = RevokeApiKey(keys=repo, audit=audit)

    await revoke.execute(owner_user_id=owner, key_id=issued.key.id)
    assert audit.actions() == ["b2b.key.revoked"]
    assert (await repo.get_by_id(issued.key.id)).is_active is False

    # Повторный отзыв ничего не меняет и НЕ пишет второй записи в аудит.
    await revoke.execute(owner_user_id=owner, key_id=issued.key.id)
    assert audit.actions() == ["b2b.key.revoked"]


async def test_revoke_foreign_key_raises_not_found() -> None:
    repo = FakeApiKeyRepository()
    issued = await _issue_uc(repo, FakeAuditTrail()).execute(
        owner_user_id=uuid.uuid4(), name="Ключ"
    )
    audit = FakeAuditTrail()
    revoke = RevokeApiKey(keys=repo, audit=audit)

    # Чужой владелец — ключ «не найден», аудит не пишется.
    with pytest.raises(ApiKeyNotFoundError):
        await revoke.execute(owner_user_id=uuid.uuid4(), key_id=issued.key.id)
    assert audit.actions() == []


# ── AuthenticateApiKey ───────────────────────────────────────────────────────


async def test_authenticate_valid_key_passes() -> None:
    repo = FakeApiKeyRepository()
    gen = FakeKeyGenerator()
    issued = await IssueApiKey(
        keys=repo, generator=gen, audit=FakeAuditTrail(), default_quota=5
    ).execute(owner_user_id=uuid.uuid4(), name="Ключ")

    auth = AuthenticateApiKey(
        keys=repo, generator=gen, quota=FakeQuotaCounter(allowed=True)
    )
    authed = await auth.execute(plaintext=issued.plaintext)
    assert authed.id == issued.key.id


async def test_authenticate_unknown_key_raises() -> None:
    repo = FakeApiKeyRepository()
    gen = FakeKeyGenerator()
    auth = AuthenticateApiKey(
        keys=repo, generator=gen, quota=FakeQuotaCounter(allowed=True)
    )
    with pytest.raises(InvalidApiKeyError):
        await auth.execute(plaintext="vk_unknown_secret")


async def test_authenticate_quota_denied_raises_fail_closed() -> None:
    # Квота вернула «нельзя» (в т.ч. fail-closed при сбое Redis) → 429-семантика.
    repo = FakeApiKeyRepository()
    gen = FakeKeyGenerator()
    issued = await IssueApiKey(
        keys=repo, generator=gen, audit=FakeAuditTrail(), default_quota=5
    ).execute(owner_user_id=uuid.uuid4(), name="Ключ")

    auth = AuthenticateApiKey(
        keys=repo, generator=gen, quota=FakeQuotaCounter(allowed=False)
    )
    with pytest.raises(QuotaExceededError):
        await auth.execute(plaintext=issued.plaintext)
