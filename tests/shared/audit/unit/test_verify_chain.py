"""Юнит-тесты верификации хеш-цепочки аудита (use-case, с фейком порта чтения)."""

from __future__ import annotations

from app.shared.audit.application.verify_chain import VerifyAuditChain
from tests.shared.audit.fakes import FakeAuditLogReader, build_valid_chain


async def test_valid_chain_is_ok() -> None:
    entries = build_valid_chain(5)
    result = await VerifyAuditChain(reader=FakeAuditLogReader(entries)).execute()
    assert result.ok is True
    assert result.checked == 5
    assert result.first_broken_id is None


async def test_empty_chain_is_ok() -> None:
    result = await VerifyAuditChain(reader=FakeAuditLogReader([])).execute()
    assert result.ok is True
    assert result.checked == 0
    assert result.first_broken_id is None


async def test_tampered_payload_breaks_at_that_record() -> None:
    """Подмена ``after`` записи N (без пересчёта хеша) → ``first_broken_id == N``."""
    entries = build_valid_chain(5)
    tampered = entries[2]  # id == 3
    tampered.after = {"n": 999}  # содержимое поменяли, hash оставили старым

    result = await VerifyAuditChain(reader=FakeAuditLogReader(entries)).execute()

    assert result.ok is False
    assert result.first_broken_id == 3
    assert result.checked == 3  # дошли ровно до испорченной записи


async def test_forged_prev_hash_breaks_chain_continuity() -> None:
    """Подмена ``prev_hash`` записи (без пересчёта самой цепочки) детектится."""
    entries = build_valid_chain(4)
    entries[1].prev_hash = "forged"  # id == 2 теперь ссылается не туда

    result = await VerifyAuditChain(reader=FakeAuditLogReader(entries)).execute()

    assert result.ok is False
    assert result.first_broken_id == 2
