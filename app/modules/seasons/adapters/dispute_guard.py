"""Заглушка проверки открытых споров — **fail-loud** (дизайн §6.4).

Домен resolutions/disputes ещё не построен. Молча возвращать «споров нет»
опасно: спроектированная защита от финализации поверх открытых споров была бы
выключена и невидимо. Поэтому заглушка названа явно и **громко** предупреждает
при каждом вызове.

TODO(resolutions): заменить на реальную проверку. До замены **запрещено**
включать автоматическую таймерную финализацию (``season_roll``) в проде с
реальными призовыми деньгами — сезон может закрыться поверх открытых споров.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


class AlwaysAllowsDisputeGuard:
    """Всегда разрешает финализацию (споров «нет»), но логирует предупреждение."""

    async def has_open_disputes(self, season_id: uuid.UUID) -> bool:
        """Возвращает ``False`` и пишет warning о том, что проверка — заглушка."""
        logger.warning(
            "DisputeGuard is a no-op stub — real resolutions check not wired "
            "(season=%s). Do NOT enable automatic finalization in production "
            "with real prize money until replaced. TODO(resolutions).",
            season_id,
        )
        return False
