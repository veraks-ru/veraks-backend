"""Подпись Token ТБанк: сортировка скаляров, исключение вложенного, verify."""

import hashlib

from app.modules.billing.domain.tbank_signing import make_token, verify_token


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_make_token_sorts_scalars_and_appends_password():
    params = {
        "TerminalKey": "T",
        "Amount": 100000,
        "OrderId": "o-1",
        "Description": "Подписка",
        "Receipt": {"x": 1},
        "DATA": {"y": 2},
    }
    token = make_token(params, "pass")
    # sorted по ключу: Amount, Description, OrderId, Password, TerminalKey
    assert token == _sha("100000" + "Подписка" + "o-1" + "pass" + "T")


def test_make_token_excludes_token_receipt_data_and_nested():
    with_extra = make_token(
        {"TerminalKey": "T", "Amount": 1, "Token": "old", "Receipt": {"a": 1}}, "p"
    )
    plain = make_token({"TerminalKey": "T", "Amount": 1}, "p")
    assert with_extra == plain


def test_make_token_bool_lowercased():
    token = make_token({"TerminalKey": "T", "Recurrent": True}, "p")
    # sorted: Password, Recurrent, TerminalKey → "p" + "true" + "T"
    assert token == _sha("p" + "true" + "T")


def test_verify_token_roundtrip():
    payload: dict[str, object] = {
        "TerminalKey": "T",
        "OrderId": "o",
        "Success": True,
        "Status": "CONFIRMED",
        "PaymentId": "42",
        "Amount": 100000,
    }
    payload["Token"] = make_token(payload, "p")
    assert verify_token(payload, "p") is True
    payload["Amount"] = 999
    assert verify_token(payload, "p") is False


def test_verify_token_missing_token_is_false():
    assert verify_token({"TerminalKey": "T"}, "p") is False
