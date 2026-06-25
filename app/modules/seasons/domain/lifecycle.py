"""Чистые правила переходов жизненного цикла сезона.

Допустимы только ``upcoming → active`` и ``active → finished``. Повторный
переход в текущий статус — **идемпотентный no-op** (а не повторное действие):
это критично для надёжности воркера (см. дизайн §6.1) — повторная финализация
уже завершённого сезона не должна пересчитывать результат.

Модуль намеренно не импортирует :mod:`entities` на уровне загрузки (иначе —
цикл: ``entities`` тянет ``lifecycle`` в методах сущности). ``SeasonStatus``
подгружается лениво внутри проверки перехода.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.modules.seasons.domain.errors import InvalidSeasonTransitionError

if TYPE_CHECKING:  # pragma: no cover - только для подсказок типов
    from app.modules.seasons.domain.entities import SeasonStatus


def is_noop(current: SeasonStatus, target: SeasonStatus) -> bool:
    """Переход в тот же статус — идемпотентный no-op."""
    return current is target


def ensure_transition_allowed(current: SeasonStatus, target: SeasonStatus) -> None:
    """Проверяет допустимость перехода (для не-no-op случаев)."""
    from app.modules.seasons.domain.entities import SeasonStatus as _S

    allowed = {
        (_S.UPCOMING, _S.ACTIVE),
        (_S.ACTIVE, _S.FINISHED),
    }
    if (current, target) not in allowed:
        raise InvalidSeasonTransitionError(
            f"Недопустимый переход сезона: {current.value} → {target.value}"
        )
