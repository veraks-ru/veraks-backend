"""Юнит-тесты проверки подписи вебхуков (чистая доменная функция)."""

from __future__ import annotations

from app.modules.billing.domain.webhooks import compute_signature, verify_signature


def test_empty_secret_disables_verification() -> None:
    # Пустой секрет (dev/тест) — любая (в т.ч. отсутствующая) подпись проходит.
    assert verify_signature("", b"{}", None) is True
    assert verify_signature("", b"{}", "whatever") is True


def test_valid_signature_passes() -> None:
    secret = "topsecret"
    body = b'{"event":"payout.succeeded"}'
    sig = compute_signature(secret, body)
    assert verify_signature(secret, body, sig) is True


def test_wrong_signature_rejected() -> None:
    secret = "topsecret"
    body = b'{"amount":100}'
    assert verify_signature(secret, body, "deadbeef") is False


def test_missing_signature_with_secret_rejected() -> None:
    assert verify_signature("topsecret", b"{}", None) is False


def test_tampered_body_rejected() -> None:
    secret = "topsecret"
    sig = compute_signature(secret, b'{"amount":100}')
    # Подпись от другого тела не подходит — защита целостности.
    assert verify_signature(secret, b'{"amount":999}', sig) is False
