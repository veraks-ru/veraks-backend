"""Пуш уведомлений в реальном времени через goctopus (WS-релей).

Бэкенд POST-ит сообщение с ключом = user_id; goctopus доставляет его в
активные WebSocket-соединения этого пользователя. Ошибки пуша проглатываются:
основной запрос не должен падать из-за недоступности релея.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import RealtimeSettings
from app.shared.http import http_client

_LOG = logging.getLogger(__name__)


class GoctopusPusher:
    def __init__(self, settings: RealtimeSettings) -> None:
        self._settings = settings

    async def push(self, key: str, value: dict[str, Any]) -> None:
        if not self._settings.url:
            return
        try:
            async with http_client(timeout=3.0) as client:
                await client.post(
                    self._settings.url,
                    json={"key": key, "value": value},
                    auth=(self._settings.user, self._settings.password),
                )
        except Exception as exc:  # noqa: BLE001 — пуш best-effort, не критичен
            _LOG.warning("Не удалось отправить пуш в goctopus (best-effort): %s", exc)
