"""Юнит-тест общего HTTP-хелпера ``client_ip`` (IP клиента за реверс-прокси)."""

from __future__ import annotations

from starlette.requests import Request

from app.http import client_ip


def _make_request(*, client_host: str | None, headers: list[tuple[bytes, bytes]]) -> Request:
    """Собирает ``Request`` напрямую по ASGI-scope, без реального сервера."""
    scope = {
        "type": "http",
        "headers": headers,
        "client": (client_host, 12345) if client_host else None,
    }
    return Request(scope)


def test_uses_first_address_from_x_forwarded_for() -> None:
    """За реверс-прокси реальный IP клиента — первый в ``X-Forwarded-For``."""
    request = _make_request(
        client_host="10.0.0.1",  # адрес прокси, не клиента
        headers=[(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
    )
    assert client_ip(request) == "203.0.113.7"


def test_falls_back_to_client_host_without_header() -> None:
    """Без заголовка (прямое подключение) — ``request.client.host``."""
    request = _make_request(client_host="203.0.113.7", headers=[])
    assert client_ip(request) == "203.0.113.7"


def test_returns_none_without_header_or_client() -> None:
    """Ни заголовка, ни клиента (нет сетевого соединения) — ``None``."""
    request = _make_request(client_host=None, headers=[])
    assert client_ip(request) is None
