"""Юнит-тесты value-objects (СНИЛС, личность ЕСИА) — чистая логика."""

from __future__ import annotations

import pytest

from app.modules.identity.domain.errors import InvalidSnilsError
from app.modules.identity.domain.value_objects import EsiaIdentity, Snils


def test_parse_valid_snils_normalizes_separators() -> None:
    snils = Snils.parse("112-233-445 95")
    assert snils.digits == "11223344595"
    assert snils.formatted() == "112-233-445 95"


def test_parse_rejects_wrong_length() -> None:
    with pytest.raises(InvalidSnilsError):
        Snils.parse("123")


def test_parse_rejects_bad_checksum() -> None:
    # Корректная контрольная сумма для 112233445 — 95, не 00.
    with pytest.raises(InvalidSnilsError):
        Snils.parse("11223344500")


def test_parse_rejects_empty() -> None:
    with pytest.raises(InvalidSnilsError):
        Snils.parse("   ")


def test_low_numbers_skip_checksum() -> None:
    # Номера <= 001-001-998 не проверяются контрольным числом.
    assert Snils("00100199800").digits == "00100199800"


def test_full_name_assembles_parts() -> None:
    identity = EsiaIdentity(
        oid="o",
        snils=Snils.parse("11223344595"),
        first_name="Иван",
        last_name="Петров",
        middle_name="Сергеевич",
        trusted=True,
    )
    assert identity.full_name() == "Петров Иван Сергеевич"


def test_full_name_skips_missing_middle() -> None:
    identity = EsiaIdentity(
        oid="o",
        snils=Snils.parse("11223344595"),
        first_name="Иван",
        last_name="Петров",
        middle_name=None,
        trusted=True,
    )
    assert identity.full_name() == "Петров Иван"
