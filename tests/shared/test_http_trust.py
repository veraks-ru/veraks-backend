"""Доверие к корневым сертификатам НУЦ Минцифры.

Эквайринг ТБанка отдаёт сертификат, выпущенный «Russian Trusted Root CA».
Его нет ни в ``certifi``, ни в системном хранилище образа, поэтому платёж
падал на проверке сертификата ещё до обращения к API.
"""

from __future__ import annotations

import ssl

from app.shared.http import RUSSIAN_ROOTS, trusted_ssl_context


def test_certificates_are_shipped_with_the_code() -> None:
    """Корни лежат в репозитории: сборка не должна зависеть от чужого сайта."""
    for pem in RUSSIAN_ROOTS:
        assert pem.is_file(), pem
        assert pem.read_text().startswith("-----BEGIN CERTIFICATE-----")


def test_context_trusts_russian_roots_and_keeps_the_usual_ones() -> None:
    """Российские корни добавляются к обычным, а не заменяют их."""
    context = trusted_ssl_context()
    subjects = {
        entry.get("subject", ()) for entry in context.get_ca_certs()
    }
    flat = {
        value
        for subject in subjects
        for rdn in subject
        for _key, value in rdn
    }

    assert "Russian Trusted Root CA" in flat
    assert "Russian Trusted Sub CA" in flat
    # Обычные корни на месте — остальные интеграции ходят к ним.
    assert len(context.get_ca_certs()) > 100
    assert context.verify_mode is ssl.CERT_REQUIRED
