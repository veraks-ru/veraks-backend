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


def test_junk_forwarded_falls_back_to_client_host() -> None:
    """Мусор в ``X-Forwarded-For`` отбрасывается — берём адрес сокета.

    Заголовок клиент может прислать сам; невалидное значение до колонки
    ``inet`` (``user_consents.ip``) и до ключей rate-limiter'а доходить не
    должно.
    """
    request = _make_request(
        client_host="203.0.113.7",
        headers=[(b"x-forwarded-for", b"not-an-ip; drop table")],
    )
    assert client_ip(request) == "203.0.113.7"


def test_junk_forwarded_and_junk_client_host_gives_none() -> None:
    """Если валидного адреса нет нигде — ``None`` (штатный ``NULL``, не 500)."""
    request = _make_request(
        client_host="unix-socket",
        headers=[(b"x-forwarded-for", b"junk")],
    )
    assert client_ip(request) is None


def test_accepts_ipv6_from_forwarded() -> None:
    """IPv6 — валидный адрес и проходит как есть (в нормализованном виде)."""
    request = _make_request(
        client_host="10.0.0.1",
        headers=[(b"x-forwarded-for", b"2001:db8::1, 10.0.0.1")],
    )
    assert client_ip(request) == "2001:db8::1"
