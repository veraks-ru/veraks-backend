"""Юнит-тесты доменной части интеграции Jump.Finance.

Ядро: нормализация реквизитов СБП (телефон — единственный маршрут выплат),
маппинг статусов Jump в исход выплаты и конвертация копеек в рубли строкой
(float для денег запрещён инвариантом).
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.billing.domain.entities import PaymentProvider, PayoutRequisites
from app.modules.billing.domain.errors import InvalidRequisiteError
from app.modules.billing.domain.jump import map_jump_status, rubles_str


def _requisites(**overrides: object) -> PayoutRequisites:
    params: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "phone": "+79001234567",
        "sbp_bank_id": "100000000111",
        "last_name": "Иванов",
        "first_name": "Пётр",
    }
    params.update(overrides)
    return PayoutRequisites(**params)  # type: ignore[arg-type]


# ── PaymentProvider ────────────────────────────────────────────────────────


def test_payment_provider_has_jump() -> None:
    assert PaymentProvider("jump") is PaymentProvider.JUMP


# ── PayoutRequisites: нормализация и валидация ─────────────────────────────


def test_requisites_keep_normalized_phone() -> None:
    req = _requisites(phone="+79001234567")
    assert req.phone == "+79001234567"


@pytest.mark.parametrize(
    "raw",
    [
        "8 (900) 123-45-67",
        "89001234567",
        "79001234567",
        "+7 900 123 45 67",
    ],
)
def test_requisites_normalize_phone_to_plus7(raw: str) -> None:
    assert _requisites(phone=raw).phone == "+79001234567"


@pytest.mark.parametrize(
    "raw",
    ["", "12345", "+7900123456", "+790012345678", "не телефон", "+19001234567"],
)
def test_requisites_reject_invalid_phone(raw: str) -> None:
    with pytest.raises(InvalidRequisiteError):
        _requisites(phone=raw)


def test_requisites_strip_names_and_empty_middle_name_becomes_none() -> None:
    req = _requisites(last_name="  Иванов ", first_name=" Пётр", middle_name="  ")
    assert req.last_name == "Иванов"
    assert req.first_name == "Пётр"
    assert req.middle_name is None


@pytest.mark.parametrize(
    "field_name", ["last_name", "first_name", "sbp_bank_id"]
)
def test_requisites_reject_blank_required_fields(field_name: str) -> None:
    with pytest.raises(InvalidRequisiteError):
        _requisites(**{field_name: "   "})


def test_requisites_reject_non_numeric_sbp_bank_id() -> None:
    # У Jump id банка СБП — целое число (словарь /dictionaries).
    with pytest.raises(InvalidRequisiteError):
        _requisites(sbp_bank_id="тинькофф")


# ── Маппинг статусов Jump ──────────────────────────────────────────────────


def test_status_paid_maps_to_success() -> None:
    assert map_jump_status(1, is_final=True) is True


@pytest.mark.parametrize("status_id", [2, 5, 6])
def test_failed_statuses_map_to_failure(status_id: int) -> None:
    assert map_jump_status(status_id, is_final=True) is False


@pytest.mark.parametrize("status_id", [3, 4, 7, 8])
def test_pending_statuses_map_to_wait(status_id: int) -> None:
    assert map_jump_status(status_id, is_final=False) is None


def test_unknown_final_status_maps_to_failure() -> None:
    # Неизвестный, но финальный код — фиксируем неуспех, а не зависание.
    assert map_jump_status(99, is_final=True) is False


def test_unknown_non_final_status_maps_to_wait() -> None:
    assert map_jump_status(99, is_final=False) is None


# ── Конвертация копеек в рубли ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kopecks", "expected"),
    [
        (1, "0.01"),
        (100, "1.00"),
        (123_456, "1234.56"),
        (5_000_00, "5000.00"),
    ],
)
def test_rubles_str_converts_kopecks_exactly(kopecks: int, expected: str) -> None:
    assert rubles_str(kopecks) == expected
