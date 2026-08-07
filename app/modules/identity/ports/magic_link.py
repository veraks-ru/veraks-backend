"""Порт хранилища одноразовых ссылок входа и лимита писем на адрес."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MagicLinkStore(Protocol):
    """Короткоживущие записи «хэш токена → адрес» плюс счётчик писем.

    Хранилище обязано гасить запись АТОМАРНО (``GETDEL``): без этого два
    параллельных перехода по одной ссылке дали бы две сессии, и «один токен =
    один вход» перестал бы быть инвариантом. Тот же приём, что у
    ``StateStore`` в OIDC-потоке.
    """

    async def save(self, token_hash: str, email: str, ttl_seconds: int) -> None:
        """Сохраняет ХЭШ токена и связанный адрес с TTL."""
        ...

    async def consume(self, token_hash: str) -> str | None:
        """Атомарно гасит запись и возвращает адрес (или ``None``, если её нет)."""
        ...

    async def count_request(self, quota_key: str, window_seconds: int) -> int:
        """Учитывает запрос письма и возвращает их число в текущем окне.

        ``quota_key`` — псевдоним адреса (см.
        ``domain.magic_link.email_quota_key``), а не сам адрес: в счётчике не
        должно накапливаться «кто пытался войти за последний час».
        """
        ...
